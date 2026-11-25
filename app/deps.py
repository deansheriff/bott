"""Auth dependencies for the admin site (cookie sessions) and the Dashboard App (bearer sessions)."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AuthSession, User
from .security.middleware import client_ip
from .security.sessions import cookie_name, load_session, unsign_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Server-side RBAC: every route names the permission it needs ("<area>.<action>", see
# security/permissions.py). Permissions come from the user's role on every request, so
# changing a role takes effect immediately.


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/admin/") -> None:
        self.next_url = next_url


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFFailed(Exception):
    pass


class PanelUnauthorized(Exception):
    def __init__(self, reason: str = "login_required") -> None:
        self.reason = reason


class PanelForbidden(Exception):
    """Logged in, but the user's role lacks the panel permission."""


@dataclass
class Auth:
    user: User
    session: AuthSession
    ip: str
    perms: frozenset = frozenset()

    @property
    def csrf(self) -> str:
        return self.session.csrf_token

    def can(self, perm: str) -> bool:
        return perm in self.perms


def _perms(db: Session, user: User) -> frozenset:
    from .services.rbac import user_permissions

    return user_permissions(db, user)


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    import hmac

    return bool(expected and provided) and hmac.compare_digest(str(expected).encode(), str(provided).encode())


def same_origin(request: Request) -> bool:
    """Reject cross-site state-changing requests when the browser sends Origin."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc == host


async def _provided_csrf(request: Request) -> str | None:
    token = request.headers.get("x-csrf-token")
    if token:
        return token
    ctype = request.headers.get("content-type", "")
    if "form" in ctype:
        form = await request.form()
        value = form.get("csrf_token")
        return value if isinstance(value, str) else None
    return None


def web_session(request: Request, db: Session) -> AuthSession | None:
    settings = get_settings()
    raw = unsign_token(settings, request.cookies.get(cookie_name(settings)))
    return load_session(db, raw, "web")


def require(perm: str | None = None, allow_password_change: bool = False):
    async def dependency(request: Request, db: Session = Depends(get_db)) -> Auth:
        sess = web_session(request, db)
        if sess is None:
            raise LoginRequired(request.url.path)
        user = sess.user
        if user.must_change_password and not allow_password_change:
            raise PasswordChangeRequired()
        perms = _perms(db, user)
        if perm and perm not in perms:
            raise Forbidden()
        if request.method not in SAFE_METHODS:
            if not same_origin(request) or not csrf_matches(sess.csrf_token, await _provided_csrf(request)):
                raise CSRFFailed()
        return Auth(user=user, session=sess, ip=client_ip(request), perms=perms)

    return dependency


def bearer_session(request: Request, db: Session) -> AuthSession | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return load_session(db, header[7:].strip(), "panel")


def require_panel(perm: str = "panel.view"):
    async def dependency(request: Request, db: Session = Depends(get_db)) -> Auth:
        sess = bearer_session(request, db)
        if sess is None:
            raise PanelUnauthorized()
        if sess.user.must_change_password:
            raise PanelUnauthorized("password_change_required")
        perms = _perms(db, sess.user)
        if perm not in perms:
            raise PanelForbidden()
        if request.method not in SAFE_METHODS and not csrf_matches(sess.csrf_token, request.headers.get("x-csrf-token")):
            raise CSRFFailed()
        return Auth(user=sess.user, session=sess, ip=client_ip(request), perms=perms)

    return dependency


def require_panel_or_web(perm: str = "panel.act"):
    """For /copilot/* which serves both the iframe (bearer) and the admin site (cookie)."""

    async def dependency(request: Request, db: Session = Depends(get_db)) -> Auth:
        sess = bearer_session(request, db)
        if sess is None:
            sess = web_session(request, db)
            if sess is not None and not same_origin(request):
                raise CSRFFailed()
        if sess is None:
            raise PanelUnauthorized()
        if sess.user.must_change_password:
            raise PanelUnauthorized("password_change_required")
        perms = _perms(db, sess.user)
        if perm not in perms:
            raise PanelForbidden()
        if request.method not in SAFE_METHODS and not csrf_matches(sess.csrf_token, await _provided_csrf(request)):
            raise CSRFFailed()
        return Auth(user=sess.user, session=sess, ip=client_ip(request), perms=perms)

    return dependency
