import json
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.db import connections

from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room import get_redis_client
from tickets.waiting_room.admission import generate_admission_pass


class ApiFlowIntegrationTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.redis = get_redis_client()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Radiohead Live in Concert",
            slug="radiohead-live-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=10,
            is_active=True,
        )

        self.seats = [
            Seat(
                event=self.event,
                section="VIP",
                row="1",
                seat_number=i + 1,
                price=Decimal("250.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(10)
        ]
        Seat.objects.bulk_create(self.seats)

        # Clear Redis test keys
        self.redis.delete(f"tb:event:{self.event.id}")
        self.redis.delete(f"waiting_room:queue:{self.event.id}")

    def tearDown(self):
        self.redis.delete(f"tb:event:{self.event.id}")
        self.redis.delete(f"waiting_room:queue:{self.event.id}")
        connections.close_all()
        super().tearDown()

    def test_fast_path_admission_and_successful_allocation(self):
        # 1. Enter waiting room (fast path: tokens available)
        enter_url = reverse("enter_waiting_room", kwargs={"event_id": self.event.id})
        response = self.client.post(
            enter_url,
            data=json.dumps({"user_id": "auditor_01"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ADMITTED")
        admission_pass = data["admission_pass"]

        # 2. Allocate seats using the valid admission pass
        allocate_url = reverse("allocate_seats", kwargs={"event_id": self.event.id})
        alloc_response = self.client.post(
            allocate_url,
            data=json.dumps({"user_id": "auditor_01", "quantity": 2}),
            content_type="application/json",
            HTTP_X_ADMISSION_TOKEN=admission_pass,
        )
        self.assertEqual(alloc_response.status_code, 201)
        alloc_data = alloc_response.json()
        self.assertEqual(len(alloc_data["seats"]), 2)
        self.assertEqual(alloc_data["status"], "PENDING")

        # 3. Verify DB inventory reflection
        held_count = Seat.objects.filter(event=self.event, status=SeatStatus.HELD).count()
        self.assertEqual(held_count, 2)

    def test_allocation_rejected_without_valid_token(self):
        allocate_url = reverse("allocate_seats", kwargs={"event_id": self.event.id})

        # Missing token header
        res_missing = self.client.post(
            allocate_url,
            data=json.dumps({"user_id": "attacker", "quantity": 1}),
            content_type="application/json",
        )
        self.assertEqual(res_missing.status_code, 401)

        # Forged/tampered token header
        forged_token = "invalid.signature_hash_here"
        res_forged = self.client.post(
            allocate_url,
            data=json.dumps({"user_id": "attacker", "quantity": 1}),
            content_type="application/json",
            HTTP_X_ADMISSION_TOKEN=forged_token,
        )
        self.assertEqual(res_forged.status_code, 403)

    def test_token_bucket_exhaustion_routes_to_queued_status(self):
        import time

        enter_url = reverse("enter_waiting_room", kwargs={"event_id": self.event.id})

        # Set last_updated to the future so the Lua script calculates delta = 0,
        # preventing token replenishment during test client HTTP dispatch
        bucket_key = f"tb:event:{self.event.id}"
        self.redis.hset(
            bucket_key,
            mapping={
                "tokens": "0",
                "last_updated": str(time.time() + 60.0),
            },
        )

        # Request admission during total exhaustion
        response = self.client.post(
            enter_url,
            data=json.dumps({"user_id": "queued_user_99"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["status"], "QUEUED")
        self.assertEqual(data["queue_position"], 1)
        self.assertEqual(data["total_waiting"], 1)
        self.assertEqual(data["channel_id"], f"event_{self.event.id}_user_queued_user_99")

    def test_insufficient_seats_returns_conflict_status(self):
        # Generate valid admission token directly
        valid_pass = generate_admission_pass(event_id=self.event.id, user_id="user_overflow")
        allocate_url = reverse("allocate_seats", kwargs={"event_id": self.event.id})

        # Request 15 seats when only 10 exist
        response = self.client.post(
            allocate_url,
            data=json.dumps({"user_id": "user_overflow", "quantity": 15}),
            content_type="application/json",
            HTTP_X_ADMISSION_TOKEN=valid_pass,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Requested 15 seats, but only 10 available", response.json()["error"])