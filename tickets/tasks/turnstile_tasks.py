from celery import shared_task
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from tickets.models import Seat, SeatStatus
from tickets.waiting_room.queue_manager import VirtualWaitingRoom


@shared_task(name="tickets.tasks.turnstile_tasks.admit_waiting_room_batch")
def admit_waiting_room_batch(event_id: int, batch_size: int = 50) -> int:
    """
    Pops the top 'batch_size' users from the event's Redis ZSET waiting room,
    generates admission passes, and pushes them directly over WebSocket channels.
    """
    available_seats = Seat.objects.filter(
        event_id=event_id,
        status=SeatStatus.AVAILABLE,
    ).count()

    if available_seats <= 0:
        return 0

    effective_batch_size = min(batch_size, available_seats)
    waiting_room = VirtualWaitingRoom(event_id=event_id)
    admitted_users = waiting_room.admit_next_batch(
        batch_size=effective_batch_size,
        pass_ttl_seconds=60,
    )

    if not admitted_users:
        return 0

    channel_layer = get_channel_layer()

    # Dispatch admission events to user WebSocket groups
    for user_id, admission_pass in admitted_users:
        group_name = f"waiting_room_{event_id}_{user_id}"
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                "type": "admission_event",
                "event_id": event_id,
                "user_id": user_id,
                "admission_pass": admission_pass,
                "expires_in_seconds": 60,
            },
        )

    return len(admitted_users)