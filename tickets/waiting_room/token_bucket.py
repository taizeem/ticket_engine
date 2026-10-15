import time
from typing import Tuple
from tickets.waiting_room import get_redis_client

# Redis Lua Script for Atomic Token Bucket
# KEYS[1]: Bucket state key (e.g., "tb:event:1")
# ARGV[1]: Refill rate in tokens per second (float)
# ARGV[2]: Maximum burst capacity (integer)
# ARGV[3]: Current UNIX timestamp in seconds (float)
# ARGV[4]: Requested tokens (integer, usually 1)
# Returns: [allowed (0 or 1), remaining_tokens (float)]
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local refill_rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

-- Retrieve current bucket state: [tokens, last_updated]
local data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if tokens == nil then
    -- First access: initialize to maximum capacity
    tokens = capacity
    last_updated = now
else
    -- Calculate elapsed time and add replenished tokens
    local delta = math.max(0, now - last_updated)
    tokens = math.min(capacity, tokens + (delta * refill_rate))
    last_updated = now
end

local allowed = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
end

-- Save updated state with a 1-hour idle expiration
redis.call('HMSET', key, 'tokens', tokens, 'last_updated', last_updated)
redis.call('EXPIRE', key, 3600)

return {allowed, tokens}
"""


class RedisTokenBucket:
    """
    Sub-millisecond rate limiter that gates access directly to PostgreSQL.
    If tokens are depleted, the user is redirected to the Virtual Waiting Room.
    """

    def __init__(self, refill_rate: float, capacity: int):
        self.refill_rate = float(refill_rate)
        self.capacity = int(capacity)
        self.redis_client = get_redis_client()
        # Register script with Redis for cached SHA1 execution (EVALSHA)
        self._script = self.redis_client.register_script(TOKEN_BUCKET_LUA)

    def consume(self, event_id: int, tokens: int = 1) -> Tuple[bool, float]:
        """
        Attempts to consume tokens for a given event.
        Returns a tuple of (is_allowed, remaining_tokens).
        """
        key = f"tb:event:{event_id}"
        now = time.time()
        result = self._script(
            keys=[key],
            args=[self.refill_rate, self.capacity, now, tokens],
        )
        is_allowed = bool(result[0] == 1)
        remaining = float(result[1])
        return is_allowed, remaining