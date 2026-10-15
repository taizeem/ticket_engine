import time
from typing import Optional, List, Tuple
from tickets.waiting_room import get_redis_client
from tickets.waiting_room.admission import generate_admission_pass


class VirtualWaitingRoom:
    """
    Manages client queue state and FIFO admissions using Redis Sorted Sets.
    """

    def __init__(self, event_id: int):
        self.event_id = event_id
        self.queue_key = f"waiting_room:queue:{event_id}"
        self.active_users_key = f"waiting_room:active:{event_id}"
        self.redis_client = get_redis_client()

    def enqueue(self, user_id: str) -> Tuple[int, bool]:
        """
        Enqueues user with microsecond timestamp as the sorting score.
        Uses nx=True to prevent re-enqueueing from resetting an existing queue spot.
        Returns: (queue_position, is_new_entry)
        """
        score = time.time()
        # nx=True: only set if user is not already queued
        is_new = bool(self.redis_client.zadd(self.queue_key, {user_id: score}, nx=True))
        position = self.get_position(user_id)
        return position, is_new

    def get_position(self, user_id: str) -> Optional[int]:
        """
        Returns 1-based queue rank, or None if the user is not in the queue.
        ZRANK is O(log(N)), negligible even at 500,000 waiting users.
        """
        rank = self.redis_client.zrank(self.queue_key, user_id)
        if rank is None:
            return None
        return rank + 1

    def get_queue_size(self) -> int:
        """Returns total count of waiting clients."""
        return self.redis_client.zcard(self.queue_key)

    def admit_next_batch(self, batch_size: int, pass_ttl_seconds: int = 60) -> List[Tuple[str, str]]:
        """
        Pops the top 'batch_size' users from the queue in strict FIFO order (lowest score first)
        and generates an HMAC admission pass for each.
        Returns: List of (user_id, admission_pass)
        """
        if batch_size <= 0:
            return []

        # Atomic FIFO pop from Redis 6.2+
        popped_entries = self.redis_client.zpopmin(self.queue_key, count=batch_size)
        admitted = []

        for user_id, _ in popped_entries:
            token = generate_admission_pass(
                event_id=self.event_id,
                user_id=user_id,
                ttl_seconds=pass_ttl_seconds,
            )
            # Store in active pass registry with TTL
            self.redis_client.setex(
                f"pass:{self.event_id}:{user_id}",
                pass_ttl_seconds,
                token,
            )
            admitted.append((user_id, token))

        return admitted

    def clear(self) -> None:
        """Drops queue state (used for test teardown and event conclusion)."""
        self.redis_client.delete(self.queue_key, self.active_users_key)