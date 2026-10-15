import os
import redis
from django.conf import settings

# Thread-safe connection pool reusing TCP sockets across requests
redis_pool = redis.ConnectionPool.from_url(
    getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0"),
    max_connections=int(os.environ.get("REDIS_MAX_CONNECTIONS", 100)),
    decode_responses=True,
    socket_timeout=2.0,
    socket_connect_timeout=2.0,
)


def get_redis_client() -> redis.Redis:
    """Returns a thread-safe Redis client instance backed by the shared pool."""
    return redis.Redis(connection_pool=redis_pool)