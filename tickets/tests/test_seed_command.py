from io import StringIO
from django.core.management import call_command
from django.test import TransactionTestCase
from django.db import connections

from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room.inventory import InventoryManager


class SeedFlashSaleCommandTest(TransactionTestCase):

    def tearDown(self):
        InventoryManager(event_id=99).reset()
        connections.close_all()
        super().tearDown()

    def test_seed_command_provisions_db_and_redis_inventory(self):
        out = StringIO()
        call_command(
            "seed_flash_sale",
            event_id=99,
            seats=150,
            seats_per_row=25,
            stdout=out,
        )

        # Verify PostgreSQL state
        event = Event.objects.get(id=99)
        self.assertEqual(event.total_seats, 150)
        self.assertEqual(
            Seat.objects.filter(event=event, status=SeatStatus.AVAILABLE).count(),
            150,
        )

        # Verify Redis read-through cache state
        inv_mgr = InventoryManager(event_id=99)
        self.assertEqual(inv_mgr.get_available_count(), 150)
        self.assertFalse(inv_mgr.is_sold_out())
        self.assertIn("Seeded Event ID=99 with 150 seats", out.getvalue())