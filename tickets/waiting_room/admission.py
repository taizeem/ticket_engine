import hmac
import hashlib
import json
import base64
import time
from typing import Optional
from django.conf import settings


def generate_admission_pass(event_id: int, user_id: str, ttl_seconds: int = 60) -> str:
    """
    Issues a cryptographically signed HMAC-SHA256 admission token.
    The client passes this in the 'X-Admission-Token' HTTP header to call seat allocation.
    """
    payload = {
        "event_id": event_id,
        "user_id": user_id,
        "exp": int(time.time()) + ttl_seconds,
    }
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")

    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{encoded_payload}.{signature}"


def verify_admission_pass(token: str, expected_event_id: int, expected_user_id: str) -> bool:
    """
    Validates signature, payload integrity, and non-expired TTL.
    Uses constant-time comparison to prevent timing side-channel attacks.
    """
    try:
        encoded_payload, signature = token.strip().split(".")
    except (ValueError, AttributeError):
        return False

    # Recalculate signature over the payload
    expected_sig = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        return False

    try:
        raw_json = base64.urlsafe_b64decode(encoded_payload.encode("utf-8")).decode("utf-8")
        payload = json.loads(raw_json)
    except Exception:
        return False

    if payload.get("event_id") != expected_event_id:
        return False

    if payload.get("user_id") != expected_user_id:
        return False

    if payload.get("exp", 0) < time.time():
        return False

    return True