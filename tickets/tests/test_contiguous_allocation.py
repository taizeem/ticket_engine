import concurrent.futures
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase
from django.utils import timezone
from django.db import connections, connection

from tickets.models import Event, Seat, SeatStatus
from tickets.services import allocate_seats_contiguous
from tickets.exceptions import InsufficientSeatsAvailable
from tickets.waiting_room.inventory import InventoryManager


class ContiguousAllocationTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.now = timezone.now()

        self.event = Event.objects.create(
            name="Orchestra Hall Live",
            slug="orchestra-hall-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=10,
            is_active=True,
        )

        # Row A: Fragmented inventory (Seats 1, 2 are available, 3 is BOOKED, 4 is available)
        # Row B: Clean contiguous inventory (Seats 1, 2, 3, 4 are all AVAILABLE)
        seats = [
            Seat(event=self.event, section="Main", row="A", seat_number=1, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            Seat(event=self.event, section="Main", row="A", seat_number=2, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            Seat(event=self.event, section="Main", row="A", seat_number=3, price=Decimal("100.00"), status=SeatStatus.BOOKED),
            Seat(event=self.event, section="Main", row="A", seat_number=4, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            
            Seat(event=self.event, section="Main", row="B", seat_number=1, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            Seat(event=self.event, section="Main", row="B", seat_number=2, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            Seat(event=self.event, section="Main", row="B", seat_number=3, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
            Seat(event=self.event, section="Main", row="B", seat_number=4, price=Decimal("100.00"), status=SeatStatus.AVAILABLE),
        ]
        Seat.objects.bulk_create(seats)

        self.inv_mgr = InventoryManager(event_id=self.event.id)
        self.inv_mgr.reset()
        self.inv_mgr.sync_from_db()

    def tearDown(self):
        self.inv_mgr.reset()
        connections.close_all()
        super().tearDown()

    def test_contiguous_selection_bypasses_fragmented_row(self):
        # Request 3 contiguous seats. Row A only has 2 adjacent (1, 2). Row B has 4 adjacent (1, 2, 3, 4).
        # Must pick Row B seats 1, 2, 3 without touching Row A.
        reservation = allocate_seats_contiguous(
            event_id=self.event.id,
            user_id="family_of_three",
            quantity=3,
            allow_scattered_fallback=False,
        )

        seats = list(reservation.seats.order_by("seat_number"))
        self.assertEqual(len(seats), 3)
        self.assertEqual({s.row for s in seats}, {"B"})
        self.assertEqual([s.seat_number for s in seats], [1, 2, 3])

    def test_contiguous_strict_rejection_without_fallback(self):
        # Book seat B2 to break Row B's run
        Seat.objects.filter(event=self.event, row="B", seat_number=2).update(status=SeatStatus.BOOKED)

        # Neither Row A nor Row B has 3 contiguous seats now
        with self.assertRaises(InsufficientSeatsAvailable):
            allocate_seats_contiguous(
                event_id=self.event.id,
                user_id="strict_buyer",
                quantity=3,
                allow_scattered_fallback=False,
            )

    def test_contiguous_fallback_to_scattered(self):
        # Break Row B's run
        Seat.objects.filter(event=self.event, row="B", seat_number=2).update(status=SeatStatus.BOOKED)

        # With fallback enabled, must successfully allocate 3 scattered seats across rows
        reservation = allocate_seats_contiguous(
            event_id=self.event.id,
            user_id="flexible_buyer",
            quantity=3,
            allow_scattered_fallback=True,
        )
        self.assertEqual(reservation.seats.count(), 3)

    def test_concurrent_contiguous_allocations_no_overlap(self):
        # Isolate contention to Row B by marking Row A seats as BOOKED for this test
        Seat.objects.filter(event=self.event, row="A").update(status=SeatStatus.BOOKED)

        def worker(user_id: str):
            try:
                res = allocate_seats_contiguous(
                    event_id=self.event.id,
                    user_id=user_id,
                    quantity=2,
                    section="Main",
                    allow_scattered_fallback=False,
                )
                return [s.seat_number for s in res.seats.all()]
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(worker, "user_alpha")
            future2 = executor.submit(worker, "user_beta")
            seats1 = future1.result()
            seats2 = future2.result()

        # Both workers must succeed
        self.assertEqual(len(seats1), 2)
        self.assertEqual(len(seats2), 2)

        # Zero seat overlap in Row B
        self.assertEqual(len(set(seats1).intersection(set(seats2))), 0)
        # Together they must cover all 4 seats of Row B: {1, 2, 3, 4}
        self.assertEqual(set(seats1 + seats2), {1, 2, 3, 4})