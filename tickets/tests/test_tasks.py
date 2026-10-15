from decimal import Decimal
from datetime import timedelta
from django.test import TransactionTestCase
from django.utils import timezone
from django.db import connections

from tickets.models import Event, Seat, SeatStatus, Reservation, ReservationStatus
from tickets.tasks.expiration_tasks import reclaim_expired_seat_holds
from tickets.tasks.turnstile_tasks import admit_waiting_room_batch
from tickets.waiting_room.queue_manager import VirtualWaitingRoom
from tickets.waiting_room import get_redis_client


class CeleryTasksIntegrationTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.redis = get_redis_client()

        self.event = Event.objects.create(
            name="Apex Legends World Finals",
            slug="apex-finals-2026",
            sale_start=self.now - timedelta(hours=2),
            sale_end=self.now + timedelta(days=1),
            total_seats=20,
            is_active=True,
        )

        # Seed 20 seats
        self.seats = [
            Seat(
                event=self.event,
                section="Arena",
                row="1",
                seat_number=i + 1,
                price=Decimal("100.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(20)
        ]
        Seat.objects.bulk_create(self.seats)

        self.waiting_room = VirtualWaitingRoom(event_id=self.event.id)
        self.waiting_room.clear()

    def tearDown(self):
        self.waiting_room.clear()
        connections.close_all()
        super().tearDown()

    def test_reclaim_expired_seat_holds_lifecycle(self):
        # 1. Create one EXPIRED reservation (held 10 mins ago, expired 5 mins ago)
        expired_res = Reservation.objects.create(
            user_id="abandoned_buyer",
            event=self.event,
            status=ReservationStatus.PENDING,
            expires_at=self.now - timedelta(minutes=5),
        )
        Seat.objects.filter(event=self.event, seat_number__in=[1, 2]).update(
            status=SeatStatus.HELD,
            held_until=self.now - timedelta(minutes=5),
            reservation=expired_res,
        )

        # 2. Create one ACTIVE reservation (expires in 10 mins)
        active_res = Reservation.objects.create(
            user_id="active_buyer",
            event=self.event,
            status=ReservationStatus.PENDING,
            expires_at=self.now + timedelta(minutes=10),
        )
        Seat.objects.filter(event=self.event, seat_number__in=[3, 4]).update(
            status=SeatStatus.HELD,
            held_until=self.now + timedelta(minutes=10),
            reservation=active_res,
        )

        # 3. Execute sweeper
        reclaimed_count = reclaim_expired_seat_holds(batch_size=100)
        self.assertEqual(reclaimed_count, 2)

        # 4. Assert expired reservation is EXPIRED and its seats are AVAILABLE
        expired_res.refresh_from_db()
        self.assertEqual(expired_res.status, ReservationStatus.EXPIRED)

        reclaimed_seats = Seat.objects.filter(seat_number__in=[1, 2])
        for seat in reclaimed_seats:
            self.assertEqual(seat.status, SeatStatus.AVAILABLE)
            self.assertIsNone(seat.held_until)
            self.assertIsNone(seat.reservation)

        # 5. Assert active reservation remains completely untouched
        active_res.refresh_from_db()
        self.assertEqual(active_res.status, ReservationStatus.PENDING)
        active_seats = Seat.objects.filter(seat_number__in=[3, 4])
        for seat in active_seats:
            self.assertEqual(seat.status, SeatStatus.HELD)

    def test_turnstile_batch_admission_with_inventory_limits(self):
        # Enqueue 5 users in Redis ZSET
        for i in range(5):
            self.waiting_room.enqueue(f"gamer_{i}")

        self.assertEqual(self.waiting_room.get_queue_size(), 5)

        # Admit batch of 3 users
        admitted_count = admit_waiting_room_batch(event_id=self.event.id, batch_size=3)
        self.assertEqual(admitted_count, 3)

        # Assert queue shrunk to 2 users
        self.assertEqual(self.waiting_room.get_queue_size(), 2)

        # Assert remaining users are gamer_3 and gamer_4
        self.assertEqual(self.waiting_room.get_position("gamer_3"), 1)
        self.assertEqual(self.waiting_room.get_position("gamer_4"), 2)