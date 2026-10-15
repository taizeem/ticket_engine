import json
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from tickets.waiting_room.queue_manager import VirtualWaitingRoom


class WaitingRoomConsumer(AsyncJsonWebsocketConsumer):
    """
    Sub-millisecond WebSocket connection streaming live queue rank and admission passes.
    Each connected client joins an isolated channel group keyed by event_id and user_id.
    """

    async def connect(self):
        self.event_id = int(self.scope["url_route"]["kwargs"]["event_id"])
        self.user_id = str(self.scope["url_route"]["kwargs"]["user_id"])
        self.group_name = f"waiting_room_{self.event_id}_{self.user_id}"

        # Join the private group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Query initial queue position and stream immediately to client
        waiting_room = VirtualWaitingRoom(event_id=self.event_id)
        position = waiting_room.get_position(self.user_id)
        total_waiting = waiting_room.get_queue_size()

        await self.send_json(
            {
                "type": "QUEUE_STATUS",
                "event_id": self.event_id,
                "user_id": self.user_id,
                "queue_position": position,
                "total_waiting": total_waiting,
            }
        )

    async def disconnect(self, close_code):
        # Leave channel group to prevent memory leaks in Redis Channel Layer
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def admission_event(self, event: dict):
        """
        Handler invoked when Celery turnstile worker broadcasts an admission pass.
        Pushes payload directly over the WebSocket to trigger client checkout redirect.
        """
        await self.send_json(
            {
                "type": "ADMISSION_GRANTED",
                "event_id": event["event_id"],
                "user_id": event["user_id"],
                "admission_pass": event["admission_pass"],
                "expires_in_seconds": event["expires_in_seconds"],
            }
        )