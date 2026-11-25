"""Chatwoot webhook signature verification.

Per Chatwoot docs: X-Chatwoot-Signature = "sha256=" + HMAC-SHA256(secret, "{timestamp}.{raw_body}")
and X-Chatwoot-Timestamp = unix seconds when signed. Always verify against the
*raw* request bytes; re-serialising JSON changes the signature.
"""
from __future__ import annotations

import hashlib
import hmac
import time

MAX_SKEW_SECONDS = 300


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + raw_body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(
    secret: str,
    timestamp: str | None,
    raw_body: bytes,
    signature: str | None,
    now: float | None = None,
    tolerance: int = MAX_SKEW_SECONDS,
) -> tuple[bool, str]:
    """Return (ok, reason). reason is a short machine-readable code, safe to log."""
    if not secret:
        return False, "secret_not_configured"
    if not timestamp or not signature:
        return False, "missing_headers"
    timestamp = timestamp.strip()
    if not timestamp.isdigit():
        return False, "bad_timestamp"
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > tolerance:
        return False, "stale_timestamp"
    signature = signature.strip()
    if not signature.startswith("sha256="):
        return False, "bad_signature_format"
    expected = compute_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected.encode("utf-8"), signature.lower().encode("utf-8")):
        return False, "signature_mismatch"
    return True, "ok"
