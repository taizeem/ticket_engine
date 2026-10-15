from decimal import Decimal
from datetime import timedelta
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from django.utils import timezone
from django.db import connections

from config.asgi import application
from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room.queue_manager import VirtualWaitingRoom
from tickets.tasks.turnstile_tasks import admit_waiting_room_batch


class WebSocketWaitingRoomTest(TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.event = Event.objects.create(
            name="Formula 1 Grand Prix 2026",
            slug="f1-gp-2026",
            sale_start=self.now - timedelta(hours=1),
            sale_end=self.now + timedelta(days=2),
            total_seats=5,
            is_active=True,
        )

        self.seats = [
            Seat(
                event=self.event,
                section="Grandstand",
                row="A",
                seat_number=i + 1,
                price=Decimal("450.00"),
                status=SeatStatus.AVAILABLE,
            )
            for i in range(5)
        ]
        Seat.objects.bulk_create(self.seats)

        self.waiting_room = VirtualWaitingRoom(event_id=self.event.id)
        self.waiting_room.clear()

    def tearDown(self):
        self.waiting_room.clear()
        connections.close_all()
        super().tearDown()

    async def test_websocket_queue_connection_and_turnstile_admission(self):
        user_id = "f1_fanatic_77"

        # 1. Enqueue user in Redis ZSET
        self.waiting_room.enqueue(user_id=user_id)

        # 2. Establish WebSocket connection
        communicator = WebsocketCommunicator(
            application=application,
            path=f"/ws/waiting-room/{self.event.id}/{user_id}/",
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # 3. Assert initial queue status message received
        initial_msg = await communicator.receive_json_from(timeout=2.0)
        self.assertEqual(initial_msg["type"], "QUEUE_STATUS")
        self.assertEqual(initial_msg["queue_position"], 1)
        self.assertEqual(initial_msg["total_waiting"], 1)

        # 4. Trigger turnstile admission worker
        admit_waiting_room_batch(event_id=self.event.id, batch_size=1)

        # 5. Assert WebSocket receives live admission token push
        admission_msg = await communicator.receive_json_from(timeout=2.0)
        self.assertEqual(admission_msg["type"], "ADMISSION_GRANTED")
        self.assertIn("admission_pass", admission_msg)
        self.assertEqual(admission_msg["user_id"], user_id)

        # 6. Clean disconnection
        await communicator.disconnect()