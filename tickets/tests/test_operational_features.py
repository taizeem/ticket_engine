import json
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.db import connections

from tickets.models import Event, Seat, SeatStatus, Reservation, ReservationStatus
from tickets.waiting_room.inventory import InventoryManager


class OperationalFeaturesTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Euro 2026 Opening Match",
            slug="euro-2026-opening",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=2,
            is_active=True,
        )

        self.seats = [
            Seat.objects.create(
                event=self.event,
                section="Cat1",
                row="A",
                seat_number=i + 1,
                price=Decimal("180.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(2)
        ]

        self.inv_mgr = InventoryManager(event_id=self.event.id)
        self.inv_mgr.reset()
        self.inv_mgr.sync_from_db()

    def tearDown(self):
        self.inv_mgr.reset()
        connections.close_all()
        super().tearDown()

    def test_correlation_id_echoed_and_generated(self):
        # 1. Custom correlation ID passed in header
        custom_id = "trace-client-abc-12345"
        res = self.client.get(reverse("health_liveness"), HTTP_X_REQUEST_ID=custom_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Request-ID"), custom_id)
        self.assertEqual(res.json()["request_id"], custom_id)

        # 2. Generated correlation ID when header is omitted
        res_auto = self.client.get(reverse("health_liveness"))
        self.assertEqual(res_auto.status_code, 200)
        self.assertIsNotNone(res_auto.headers.get("X-Request-ID"))

    def test_health_readiness_probe_dependencies(self):
        res = self.client.get(reverse("health_readiness"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["checks"]["database"], "ok")
        self.assertEqual(data["checks"]["redis"], "ok")
        self.assertIn("latency_ms", data)

    def test_cancellation_releases_seats_and_replenishes_redis(self):
        # 1. Create a CONFIRMED reservation holding both seats
        res_obj = Reservation.objects.create(
            user_id="cancelling_user",
            event=self.event,
            status=ReservationStatus.CONFIRMED,
            expires_at=self.now + timedelta(days=1),
        )
        Seat.objects.filter(event=self.event).update(
            status=SeatStatus.BOOKED,
            reservation=res_obj,
        )
        self.inv_mgr.decrement(quantity=2)
        self.assertTrue(self.inv_mgr.is_sold_out())

        # 2. Execute cancellation request
        cancel_url = reverse("cancel_reservation", kwargs={"reservation_id": res_obj.id})
        response = self.client.post(
            cancel_url,
            data=json.dumps({"reason": "Cannot attend due to schedule conflict"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "CANCELLED")

        # 3. Verify PostgreSQL seats reverted to AVAILABLE
        for seat in Seat.objects.filter(event=self.event):
            self.assertEqual(seat.status, SeatStatus.AVAILABLE)
            self.assertIsNone(seat.reservation)

        # 4. Verify Redis inventory replenished and circuit breaker cleared
        self.assertEqual(self.inv_mgr.get_available_count(), 2)
        self.assertFalse(self.inv_mgr.is_sold_out())