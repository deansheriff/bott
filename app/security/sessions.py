"""Server-side sessions.

Web (admin) sessions: random token, signed with SESSION_SECRET (itsdangerous),
in a Secure + HttpOnly + SameSite=Strict cookie. Panel sessions: random bearer
token kept by the iframe in memory/sessionStorage (no cookies, because third-party
cookies are blocked in iframes). Both are stored hashed in auth_sessions and
share the same idle (30 min) and absolute (12 h) timeouts.
"""
from __future__ import annotations

from datetime import timedelta

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AuthSession, User
from ..utils import random_token, sha256_hex, utcnow

IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_TIMEOUT = timedelta(hours=12)
TOUCH_INTERVAL = timedelta(seconds=60)


def cookie_name(settings: Settings) -> str:
    # __Host- prefix forces Secure, Path=/ and no Domain attribute.
    return "__Host-rb_session" if settings.cookie_secure else "rb_session"


def _serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(settings.session_secret or "dev-insecure-secret", salt="rb-admin-session")


def sign_token(settings: Settings, raw: str) -> str:
    return _serializer(settings).dumps(raw)


def unsign_token(settings: Settings, signed: str | None) -> str | None:
    if not signed:
        return None
    try:
        value = _serializer(settings).loads(signed)
    except BadSignature:
        return None
    return value if isinstance(value, str) else None


def create_session(db: Session, user: User, kind: str, ip: str | None, user_agent: str | None) -> tuple[str, AuthSession]:
    raw = random_token(32)
    sess = AuthSession(
        id=sha256_hex(raw),
        user_id=user.id,
        kind=kind,
        csrf_token=random_token(24),
        ip=(ip or "")[:64],
        user_agent=(user_agent or "")[:255],
    )
    db.add(sess)
    db.commit()
    return raw, sess


def session_expired(sess: AuthSession) -> bool:
    now = utcnow()
    return now - sess.last_seen_at > IDLE_TIMEOUT or now - sess.created_at > ABSOLUTE_TIMEOUT


def load_session(db: Session, raw_token: str | None, kind: str) -> AuthSession | None:
    if not raw_token:
        return None
    sess = db.get(AuthSession, sha256_hex(raw_token))
    if sess is None or sess.kind != kind:
        return None
    if session_expired(sess) or not sess.user or not sess.user.is_active:
        db.delete(sess)
        db.commit()
        return None
    now = utcnow()
    if now - sess.last_seen_at > TOUCH_INTERVAL:
        sess.last_seen_at = now
        db.commit()
    return sess


def revoke_session(db: Session, sess: AuthSession) -> None:
    db.delete(sess)
    db.commit()


def revoke_user_sessions(db: Session, user_id: int, kind: str | None = None, except_id: str | None = None) -> int:
    q = db.query(AuthSession).filter(AuthSession.user_id == user_id)
    if kind:
        q = q.filter(AuthSession.kind == kind)
    if except_id:
        q = q.filter(AuthSession.id != except_id)
    n = q.delete(synchronize_session=False)
    db.commit()
    return n


def purge_expired(db: Session) -> None:
    now = utcnow()
    db.query(AuthSession).filter(
        (AuthSession.last_seen_at < now - IDLE_TIMEOUT) | (AuthSession.created_at < now - ABSOLUTE_TIMEOUT)
    ).delete(synchronize_session=False)
    db.commit()
