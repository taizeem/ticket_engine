import json
import concurrent.futures
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.db import connections

from tickets.models import Event, Seat, SeatStatus, Reservation, ReservationStatus, PaymentRecord
from tickets.services import confirm_reservation_payment
from tickets.exceptions import ReservationExpiredError


class PaymentConfirmationIntegrationTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Champions League Final 2026",
            slug="ucl-final-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=2,
            is_active=True,
        )

        self.res = Reservation.objects.create(
            user_id="uefa_vip_user",
            event=self.event,
            status=ReservationStatus.PENDING,
            expires_at=self.now + timedelta(minutes=10),
        )

        self.seat1 = Seat.objects.create(
            event=self.event,
            reservation=self.res,
            section="Category1",
            row="A",
            seat_number=1,
            price=Decimal("500.00"),
            status=SeatStatus.HELD,
            held_until=self.res.expires_at,
        )
        self.seat2 = Seat.objects.create(
            event=self.event,
            reservation=self.res,
            section="Category1",
            row="A",
            seat_number=2,
            price=Decimal("500.00"),
            status=SeatStatus.HELD,
            held_until=self.res.expires_at,
        )

    def tearDown(self):
        connections.close_all()
        super().tearDown()

    def test_successful_booking_confirmation(self):
        url = reverse("confirm_reservation", kwargs={"reservation_id": self.res.id})
        response = self.client.post(
            url,
            data=json.dumps({"amount": "1000.00", "payment_reference": "ch_stripe_abc123"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idemp_key_unique_001",
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["status"], "CONFIRMED")
        self.assertTrue(data["is_newly_confirmed"])

        # Assert database updates
        self.res.refresh_from_db()
        self.assertEqual(self.res.status, ReservationStatus.CONFIRMED)

        for seat in [self.seat1, self.seat2]:
            seat.refresh_from_db()
            self.assertEqual(seat.status, SeatStatus.BOOKED)
            self.assertIsNone(seat.held_until)

        # Assert PaymentRecord creation
        payment = PaymentRecord.objects.get(idempotency_key="idemp_key_unique_001")
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertEqual(payment.payment_reference, "ch_stripe_abc123")

    def test_idempotent_duplicate_webhook_replay(self):
        url = reverse("confirm_reservation", kwargs={"reservation_id": self.res.id})
        payload = {"amount": "1000.00", "payment_reference": "ch_stripe_abc123"}

        # First request
        res1 = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idemp_key_replay_999",
        )
        self.assertEqual(res1.status_code, 201)

        # Second request with identical idempotency key
        res2 = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idemp_key_replay_999",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(res2.json()["is_newly_confirmed"])

        # Total payment records in database must still equal exactly 1
        self.assertEqual(PaymentRecord.objects.filter(reservation=self.res).count(), 1)

    def test_expired_reservation_confirmation_rejected_and_released(self):
        # Manually backdate reservation hold into the past
        self.res.expires_at = self.now - timedelta(seconds=30)
        self.res.save(update_fields=["expires_at"])

        url = reverse("confirm_reservation", kwargs={"reservation_id": self.res.id})
        response = self.client.post(
            url,
            data=json.dumps({"amount": "1000.00", "payment_reference": "ch_too_late"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="idemp_key_late_001",
        )
        self.assertEqual(response.status_code, 410)  # HTTP 410 Gone

        # Verify seats were safely recycled back to AVAILABLE
        for seat in [self.seat1, self.seat2]:
            seat.refresh_from_db()
            self.assertEqual(seat.status, SeatStatus.AVAILABLE)
            self.assertIsNone(seat.reservation)