from channels.generic.websocket import AsyncJsonWebsocketConsumer
from asgiref.sync import sync_to_async
from tickets.waiting_room.queue_manager import VirtualWaitingRoom


class WaitingRoomConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        self.event_id = int(self.scope["url_route"]["kwargs"]["event_id"])
        self.user_id = str(self.scope["url_route"]["kwargs"]["user_id"])
        self.group_name = f"waiting_room_{self.event_id}_{self.user_id}"

        # Join the isolated channel group
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        # Non-blocking Redis queries via sync_to_async
        waiting_room = VirtualWaitingRoom(event_id=self.event_id)
        position = await sync_to_async(waiting_room.get_position)(self.user_id)
        total_waiting = await sync_to_async(waiting_room.get_queue_size)()

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
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def admission_event(self, event: dict):
        await self.send_json(
            {
                "type": "ADMISSION_GRANTED",
                "event_id": event["event_id"],
                "user_id": event["user_id"],
                "admission_pass": event["admission_pass"],
                "expires_in_seconds": event["expires_in_seconds"],
            }
        )