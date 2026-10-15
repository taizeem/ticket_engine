import hmac
import hashlib
import time
import secrets
from typing import Tuple, Dict, Any
from django.conf import settings
from tickets.waiting_room import get_redis_client


def generate_pow_challenge(
    event_id: int,
    user_id: str,
    difficulty: int = 4,
    ttl_seconds: int = 60,
) -> Dict[str, Any]:
    """
    Issues an HMAC-signed challenge requiring the client to find an integer 'solution'
    such that sha256(f"{nonce}:{solution}").hexdigest() begins with '0' * difficulty.
    """
    nonce = secrets.token_hex(16)
    timestamp = int(time.time())

    payload = f"{event_id}:{user_id}:{nonce}:{difficulty}:{timestamp}"
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "event_id": event_id,
        "user_id": user_id,
        "nonce": nonce,
        "difficulty": difficulty,
        "timestamp": timestamp,
        "expires_in_seconds": ttl_seconds,
        "signature": signature,
    }


def verify_pow_solution(
    event_id: int,
    user_id: str,
    nonce: str,
    difficulty: int,
    timestamp: int,
    signature: str,
    solution: int,
    ttl_seconds: int = 60,
) -> Tuple[bool, str]:
    """
    Validates PoW challenge solution in O(1) time.
    Enforces HMAC authenticity, freshness, single-use anti-replay, and hash prefix.
    """
    now = int(time.time())

    # 1. Check TTL freshness
    if (now - timestamp) > ttl_seconds or timestamp > (now + 5):
        return False, "Challenge has expired."

    # 2. Verify HMAC signature authenticity
    expected_payload = f"{event_id}:{user_id}:{nonce}:{difficulty}:{timestamp}"
    expected_sig = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        return False, "Invalid challenge signature."

    # 3. Replay Protection: Ensure nonce has not been spent
    redis_client = get_redis_client()
    replay_key = f"pow:spent:{nonce}"
    # Atomically sets key with TTL; fails if already present
    is_fresh = bool(redis_client.set(replay_key, "1", nx=True, ex=ttl_seconds * 2))
    if not is_fresh:
        return False, "Challenge solution has already been spent."

    # 4. Verify Single Hash Computation
    candidate_hash = hashlib.sha256(f"{nonce}:{solution}".encode("utf-8")).hexdigest()
    required_prefix = "0" * int(difficulty)

    if not candidate_hash.startswith(required_prefix):
        return False, "Incorrect proof-of-work solution."

    return True, "Valid."


def solve_pow_challenge(nonce: str, difficulty: int) -> int:
    """
    Reference solver (used by automated tests and client SDKs).
    Finds the first integer solution matching the difficulty prefix.
    """
    required_prefix = "0" * difficulty
    solution = 0
    while True:
        candidate = hashlib.sha256(f"{nonce}:{solution}".encode("utf-8")).hexdigest()
        if candidate.startswith(required_prefix):
            return solution
        solution += 1