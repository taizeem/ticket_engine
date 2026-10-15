from typing import Optional
from tickets.waiting_room import get_redis_client
from tickets.models import Seat, SeatStatus

# Redis Lua Script: Atomically decrements available inventory.
# If inventory drops to <= 0, automatically trips the sold_out circuit breaker.
# KEYS[1]: inventory key (e.g., "inventory:available:1")
# KEYS[2]: circuit breaker key (e.g., "circuit_breaker:sold_out:1")
# ARGV[1]: quantity to decrement (integer)
# Returns: remaining inventory count
DECREMENT_INVENTORY_LUA = """
local inv_key = KEYS[1]
local cb_key = KEYS[2]
local qty = tonumber(ARGV[1])

local current = tonumber(redis.call('GET', inv_key) or 0)
local updated = current - qty

if updated <= 0 then
    redis.call('SET', inv_key, 0)
    redis.call('SET', cb_key, '1')
    return 0
else
    redis.call('SET', inv_key, updated)
    return updated
end
"""


class InventoryManager:
    """
    Manages atomic Redis inventory counts and edge-level circuit breaking.
    """

    def __init__(self, event_id: int):
        self.event_id = event_id
        self.inv_key = f"inventory:available:{event_id}"
        self.cb_key = f"circuit_breaker:sold_out:{event_id}"
        self.redis = get_redis_client()
        self._decr_script = self.redis.register_script(DECREMENT_INVENTORY_LUA)

    def is_sold_out(self) -> bool:
        """Evaluates whether the circuit breaker has tripped in under 0.2ms."""
        return self.redis.get(self.cb_key) == "1"

    def get_available_count(self) -> int:
        """Read-through cache: returns cached count or initializes from PostgreSQL."""
        val = self.redis.get(self.inv_key)
        if val is not None:
            return int(val)
        return self.sync_from_db()

    def sync_from_db(self) -> int:
        """Authoritative read from PostgreSQL to initialize or repair the Redis cache."""
        count = Seat.objects.filter(event_id=self.event_id, status=SeatStatus.AVAILABLE).count()
        self.redis.set(self.inv_key, count)
        if count <= 0:
            self.redis.set(self.cb_key, "1")
        else:
            self.redis.delete(self.cb_key)
        return count

    def decrement(self, quantity: int) -> int:
        """Atomically deducts seats and trips the breaker if remaining reaches 0."""
        return int(
            self._decr_script(
                keys=[self.inv_key, self.cb_key],
                args=[quantity],
            )
        )

    def increment(self, quantity: int) -> int:
        """Atomically restores inventory (used by hold sweeper) and resets circuit breaker."""
        new_val = self.redis.incrby(self.inv_key, quantity)
        if new_val > 0:
            self.redis.delete(self.cb_key)
        return new_val

    def trip(self) -> None:
        """Manually trips the circuit breaker."""
        self.redis.set(self.inv_key, 0)
        self.redis.set(self.cb_key, "1")

    def reset(self) -> None:
        """Clears circuit breaker and drops cache keys."""
        self.redis.delete(self.inv_key, self.cb_key)