import concurrent.futures
from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase
from django.utils import timezone
from django.db import connections, connection

from tickets.models import Event, Seat, SeatStatus, ReservationStatus
from tickets.services import allocate_seats_orm, allocate_seats_raw_cte
from tickets.exceptions import InsufficientSeatsAvailable


class ConcurrencySeatAllocationTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.event = Event.objects.create(
            name="Coldplay World Tour",
            slug="coldplay-world-tour-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=5),
            total_seats=50,
            is_active=True,
        )

        self.seats = [
            Seat(
                event=self.event,
                section="A",
                row=str((i // 10) + 1),
                seat_number=(i % 10) + 1,
                price=Decimal("150.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(50)
        ]
        Seat.objects.bulk_create(self.seats)

    def tearDown(self):
        # Force release of all connection pool sockets before database teardown
        connections.close_all()
        super().tearDown()

    def test_single_allocation_success(self):
        reservation = allocate_seats_orm(
            event_id=self.event.id,
            user_id="user_123",
            quantity=2,
            hold_duration_seconds=300,
        )
        self.assertEqual(reservation.status, ReservationStatus.PENDING)
        self.assertEqual(reservation.seats.count(), 2)

        held_seats = Seat.objects.filter(reservation=reservation)
        self.assertEqual(held_seats.count(), 2)
        for seat in held_seats:
            self.assertEqual(seat.status, SeatStatus.HELD)
            self.assertIsNotNone(seat.held_until)

    def test_insufficient_seats_atomic_rollback(self):
        with self.assertRaises(InsufficientSeatsAvailable):
            allocate_seats_orm(
                event_id=self.event.id,
                user_id="whale_user",
                quantity=60,
            )

        available_count = Seat.objects.filter(
            event=self.event,
            status=SeatStatus.AVAILABLE,
        ).count()
        self.assertEqual(available_count, 50)

    def test_high_concurrency_race_condition_protection(self):
        thread_count = 25
        quantity_per_request = 4
        success_reservations = []
        failure_count = 0

        def worker_task(thread_id: int):
            try:
                res = allocate_seats_orm(
                    event_id=self.event.id,
                    user_id=f"user_{thread_id}",
                    quantity=quantity_per_request,
                    hold_duration_seconds=600,
                )
                return ("SUCCESS", res.id)
            except InsufficientSeatsAvailable:
                return ("FAILED_INSUFFICIENT", None)
            except Exception as e:
                return ("ERROR", str(e))
            finally:
                # Crucial: Close thread-local DB socket when the worker finishes
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker_task, i) for i in range(thread_count)]
            for future in concurrent.futures.as_completed(futures):
                status, res_id = future.result()
                if status == "SUCCESS":
                    success_reservations.append(res_id)
                elif status == "FAILED_INSUFFICIENT":
                    failure_count += 1
                else:
                    self.fail(f"Unexpected transaction error: {res_id}")

        self.assertEqual(len(success_reservations), 12)
        self.assertEqual(failure_count, 13)

        allocated_seats = Seat.objects.filter(
            event=self.event,
            status=SeatStatus.HELD,
        )
        self.assertEqual(allocated_seats.count(), 48)

        reservation_ids = allocated_seats.values_list("reservation_id", flat=True)
        self.assertEqual(len(reservation_ids), 48)
        self.assertEqual(len(set(reservation_ids)), 12)

    def test_raw_sql_cte_concurrency_equivalence(self):
        reservation = allocate_seats_raw_cte(
            event_id=self.event.id,
            user_id="user_fast_lane",
            quantity=5,
            hold_duration_seconds=300,
        )
        self.assertEqual(reservation.seats.count(), 5)
        self.assertEqual(
            Seat.objects.filter(reservation=reservation, status=SeatStatus.HELD).count(),
            5,
        )