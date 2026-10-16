from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase, Client
from django.utils import timezone
from django.urls import reverse
from django.db import connections

from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room.inventory import InventoryManager
from tickets.waiting_room.pow import generate_pow_challenge, verify_pow_solution, solve_pow_challenge


class PrometheusMetricsTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Super Bowl 2026",
            slug="super-bowl-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=5,
            is_active=True,
        )

        self.seats = [
            Seat.objects.create(
                event=self.event,
                section="Field",
                row="1",
                seat_number=i + 1,
                price=Decimal("1500.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(5)
        ]

        self.inv_mgr = InventoryManager(event_id=self.event.id)
        self.inv_mgr.reset()
        self.inv_mgr.sync_from_db()

    def tearDown(self):
        self.inv_mgr.reset()
        connections.close_all()
        super().tearDown()

    def test_metrics_endpoint_exposes_prometheus_format(self):
        # 1. Trigger a valid PoW verification to record telemetry
        challenge = generate_pow_challenge(event_id=self.event.id, user_id="metric_user", difficulty=2)
        solution = solve_pow_challenge(challenge["nonce"], difficulty=2)
        verify_pow_solution(
            event_id=self.event.id,
            user_id="metric_user",
            nonce=challenge["nonce"],
            difficulty=2,
            timestamp=challenge["timestamp"],
            signature=challenge["signature"],
            solution=solution,
        )

        # 2. Scrape /metrics/
        response = self.client.get(reverse("prometheus_metrics"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")

        # 3. Assert custom metric identifiers are exported in Prometheus format
        self.assertIn("ticket_allocation_duration_seconds_bucket", content)
        self.assertIn("ticket_available_inventory", content)
        self.assertIn("ticket_waiting_room_queue_size", content)
        self.assertIn("ticket_circuit_breaker_tripped", content)
        self.assertIn('ticket_pow_verifications_total{status="valid"}', content)