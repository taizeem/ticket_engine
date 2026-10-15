import json
from celery import shared_task
from django.utils import timezone
from tickets.models import Event, Seat, SeatStatus
from tickets.waiting_room import get_redis_client
from tickets.waiting_room.queue_manager import VirtualWaitingRoom


@shared_task(name="tickets.tasks.turnstile_tasks.admit_waiting_room_batch")
def admit_waiting_room_batch(event_id: int, batch_size: int = 50) -> int:
    """
    Pops the top 'batch_size' users from the event's Redis ZSET waiting room,
    generates admission passes, and publishes admission events to Redis Pub/Sub.
    Returns: count of admitted users.
    """
    # 1. Check if event is active and has available inventory
    available_seats = Seat.objects.filter(
        event_id=event_id,
        status=SeatStatus.AVAILABLE,
    ).count()

    if available_seats <= 0:
        return 0

    # Throttle admission batch size so it never exceeds available seats
    effective_batch_size = min(batch_size, available_seats)

    # 2. Atomically pop users and generate HMAC passes
    waiting_room = VirtualWaitingRoom(event_id=event_id)
    admitted_users = waiting_room.admit_next_batch(
        batch_size=effective_batch_size,
        pass_ttl_seconds=60,
    )

    if not admitted_users:
        return 0

    redis_client = get_redis_client()

    # 3. Publish admission notifications to Redis Pub/Sub
    for user_id, admission_pass in admitted_users:
        channel_name = f"waiting_room:notifications:{event_id}:{user_id}"
        payload = {
            "status": "ADMITTED",
            "event_id": event_id,
            "user_id": user_id,
            "admission_pass": admission_pass,
            "expires_in_seconds": 60,
        }
        redis_client.publish(channel_name, json.dumps(payload))

    return len(admitted_users)