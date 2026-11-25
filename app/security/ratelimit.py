"""Rate limiting.

- SlidingWindowLimiter: in-process, used for the webhook (per conversation) and
  panel-link creation (per IP). Single-instance deployments only; use Redis if
  you scale horizontally.
- Login lockout: stored in the DB so it survives restarts and multiple workers.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import LoginAttempt
from ..utils import utcnow

LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_ACCOUNT = 5
MAX_FAILURES_PER_IP = 20


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: float) -> bool:
        """Record a hit; return True if allowed, False if over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # crude memory bound
                for k in list(self._hits)[:10_000]:
                    if not self._hits[k]:
                        del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def login_locked(db: Session, ip: str, email: str) -> bool:
    since = utcnow() - LOGIN_WINDOW
    acct = db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.email == email, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since
        )
    )
    if (acct or 0) >= MAX_FAILURES_PER_ACCOUNT:
        return True
    by_ip = db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.ip == ip, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since
        )
    )
    return (by_ip or 0) >= MAX_FAILURES_PER_IP


def record_login_attempt(db: Session, ip: str, email: str, success: bool) -> None:
    db.add(LoginAttempt(ip=ip, email=email, success=success))
    if success:
        # A successful login clears the account's failure streak.
        db.query(LoginAttempt).filter(
            LoginAttempt.email == email, LoginAttempt.success.is_(False)
        ).delete(synchronize_session=False)
    db.commit()
