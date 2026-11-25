"""Admin site: login, logout, passwords, invites, panel pairing, profile (Chatwoot token)."""
from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import Auth, CSRFFailed, require, same_origin, web_session
from ..models import PanelLink, User, UserToken
from ..security.crypto import encrypt_secret
from ..security.middleware import client_ip
from ..security.passwords import hash_password, needs_rehash, password_problem, verify_password
from ..security.ratelimit import login_locked, record_login_attempt
from ..security.sessions import cookie_name, create_session, revoke_session, revoke_user_sessions, sign_token
from ..services import audit
from ..services.chatwoot import ChatwootError, agent_client
from ..templating import render
from ..utils import random_token, sha256_hex, utcnow

router = APIRouter(prefix="/admin")
LOGIN_CSRF_COOKIE = "rb_login_csrf"


def safe_next(value: str | None) -> str:
    """Only allow local /admin paths as post-login redirects."""
    if not value:
        return "/admin/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not parts.path.startswith("/admin") or value.startswith("//") or "\\" in value:
        return "/admin/"
    return value


def _set_session_cookie(response, raw_token: str) -> None:
    s = get_settings()
    response.set_cookie(cookie_name(s), sign_token(s, raw_token), httponly=True, secure=s.cookie_secure, samesite="strict", path="/", max_age=12 * 3600)


# ------------------------------------------------------------------ login
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/admin/", db: Session = Depends(get_db)):
    if web_session(request, db) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    token = random_token(24)
    resp = render(request, "admin/login.html", {"next": safe_next(next), "csrf_token": token, "error": ""})
    s = get_settings()
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True, secure=s.cookie_secure, samesite="strict", path="/admin", max_age=1800)
    return resp


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/admin/"), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not same_origin(request) or not cookie_token or not hmac.compare_digest(cookie_token.encode(), csrf_token.encode()):
        raise CSRFFailed()
    ip = client_ip(request)
    email = email.strip().lower()[:255]

    def fail(msg: str, status: int = 400):
        return render(request, "admin/login.html", {"next": safe_next(next), "csrf_token": cookie_token, "error": msg}, status_code=status)

    if login_locked(db, ip, email):
        return fail("Too many failed attempts. Try again in 15 minutes.", 429)
    user = db.query(User).filter(User.email == email).one_or_none()
    ok = verify_password(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        record_login_attempt(db, ip, email, False)
        return fail("Invalid email or password.", 401)
    record_login_attempt(db, ip, email, True)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    db.commit()
    raw, _ = create_session(db, user, "web", ip, request.headers.get("user-agent"))
    target = "/admin/password" if user.must_change_password else safe_next(next)
    resp = RedirectResponse(target, status_code=303)
    _set_session_cookie(resp, raw)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/admin")
    return resp


@router.post("/logout")
def logout(request: Request, auth: Auth = Depends(require(allow_password_change=True)), db: Session = Depends(get_db)):
    revoke_session(db, auth.session)
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(cookie_name(get_settings()), path="/")
    return resp


# ------------------------------------------------------------------ passwords
@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, auth: Auth = Depends(require(allow_password_change=True)), db: Session = Depends(get_db)):
    return render(request, "admin/password.html", {"error": "", "forced": auth.user.must_change_password}, auth=auth, db=db)


@router.post("/password", response_class=HTMLResponse)
def password_submit(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""), auth: Auth = Depends(require(allow_password_change=True)), db: Session = Depends(get_db)):
    user = auth.user

    def fail(msg: str):
        return render(request, "admin/password.html", {"error": msg, "forced": user.must_change_password}, auth=auth, db=db, status_code=400)

    if not verify_password(user.password_hash, current):
        return fail("Current password is incorrect.")
    if new != confirm:
        return fail("New passwords do not match.")
    if problem := password_problem(new, user.email):
        return fail(problem)
    if verify_password(user.password_hash, new):
        return fail("Choose a different password from the current one.")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    audit.record(db, user, "user.password_change", "user", user.id, None, {"password": "changed"}, auth.ip)
    db.commit()
    revoke_user_sessions(db, user.id, except_id=auth.session.id)
    return RedirectResponse("/admin/", status_code=303)


def _valid_user_token(db: Session, token: str) -> UserToken | None:
    row = db.query(UserToken).filter(UserToken.token_hash == sha256_hex(token)).one_or_none()
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        return None
    return row


@router.get("/set-password/{token}", response_class=HTMLResponse)
def set_password_form(request: Request, token: str, db: Session = Depends(get_db)):
    row = _valid_user_token(db, token)
    if row is None:
        return render(request, "admin/message.html", {"title": "Link expired", "message": "This link is invalid or has expired. Ask an admin for a new one."}, status_code=410)
    csrf = random_token(24)
    resp = render(request, "admin/set_password.html", {"token": token, "error": "", "csrf_token": csrf, "purpose": row.purpose})
    resp.set_cookie(LOGIN_CSRF_COOKIE, csrf, httponly=True, secure=get_settings().cookie_secure, samesite="strict", path="/admin", max_age=1800)
    return resp


@router.post("/set-password/{token}", response_class=HTMLResponse)
def set_password_submit(request: Request, token: str, new: str = Form(""), confirm: str = Form(""), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    if not same_origin(request) or not cookie_token or not hmac.compare_digest(cookie_token.encode(), csrf_token.encode()):
        raise CSRFFailed()
    row = _valid_user_token(db, token)
    if row is None:
        return render(request, "admin/message.html", {"title": "Link expired", "message": "This link is invalid or has expired."}, status_code=410)
    user = db.get(User, row.user_id)
    error = "Passwords do not match." if new != confirm else password_problem(new, user.email if user else "")
    if error or user is None:
        return render(request, "admin/set_password.html", {"token": token, "error": error or "Invalid link.", "csrf_token": cookie_token, "purpose": row.purpose}, status_code=400)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.is_active = True
    row.used_at = utcnow()
    audit.record(db, user, f"user.{row.purpose}_completed", "user", user.id, None, {"password": "set"}, client_ip(request))
    db.commit()
    revoke_user_sessions(db, user.id)
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------------------------------------------ panel pairing
@router.get("/panel-link", response_class=HTMLResponse)
def panel_link_form(request: Request, code: str = "", auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    link = db.query(PanelLink).filter(PanelLink.code == code.strip().upper()).one_or_none() if code else None
    valid = link is not None and link.expires_at > utcnow() and link.confirmed_at is None
    return render(request, "admin/panel_link.html", {"code": code.strip().upper()[:16], "valid": valid, "done": False}, auth=auth, db=db)


@router.post("/panel-link", response_class=HTMLResponse)
def panel_link_confirm(request: Request, code: str = Form(""), auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    link = db.query(PanelLink).filter(PanelLink.code == code.strip().upper()).one_or_none()
    if link is None or link.expires_at < utcnow() or link.confirmed_at is not None:
        return render(request, "admin/panel_link.html", {"code": code, "valid": False, "done": False}, auth=auth, db=db, status_code=410)
    link.user_id = auth.user.id
    link.confirmed_at = utcnow()
    audit.record(db, auth.user, "panel.link_confirmed", "panel_link", link.id, None, {"code": link.code}, auth.ip)
    db.commit()
    return render(request, "admin/panel_link.html", {"code": link.code, "valid": True, "done": True}, auth=auth, db=db)


# ------------------------------------------------------------------ profile
@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    return render(request, "admin/profile.html", {"error": "", "flash": ""}, auth=auth, db=db)


@router.post("/profile/chatwoot-token", response_class=HTMLResponse)
def connect_token(request: Request, token: str = Form(""), auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    token = token.strip()
    user = auth.user

    def page(error: str = "", flash: str = "", status: int = 200):
        return render(request, "admin/profile.html", {"error": error, "flash": flash}, auth=auth, db=db, status_code=status)

    if not token or len(token) > 200:
        return page("Paste your Chatwoot access token (Profile settings > Access Token).", status=400)
    try:
        prof = agent_client(token).get_profile()
    except ChatwootError:
        return page("Chatwoot rejected that token, or Chatwoot is unreachable.", status=400)
    account_id = get_settings().chatwoot_account_id
    accounts = prof.get("accounts") or []
    if accounts and not any(int(a.get("id", 0)) == account_id for a in accounts if isinstance(a, dict)):
        return page("That token's user is not a member of the configured Chatwoot account.", status=400)
    before = {"chatwoot_user_id": user.chatwoot_user_id, "chatwoot_token": bool(user.chatwoot_token_enc)}
    user.chatwoot_token_enc = encrypt_secret(token)
    user.chatwoot_user_id = prof.get("id")
    user.chatwoot_name = str(prof.get("name") or "")[:255]
    audit.record(db, user, "user.chatwoot_token_connect", "user", user.id, before, {"chatwoot_user_id": user.chatwoot_user_id, "chatwoot_token": True}, auth.ip)
    db.commit()
    return page(flash=f"Connected as {user.chatwoot_name or prof.get('email', 'Chatwoot user')}.")


@router.post("/profile/chatwoot-token/delete", response_class=HTMLResponse)
def disconnect_token(request: Request, auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    user = auth.user
    user.chatwoot_token_enc = None
    audit.record(db, user, "user.chatwoot_token_disconnect", "user", user.id, {"chatwoot_token": True}, {"chatwoot_token": False}, auth.ip)
    db.commit()
    return render(request, "admin/profile.html", {"error": "", "flash": "Chatwoot token removed."}, auth=auth, db=db)


@router.post("/profile/panel-sessions/revoke", response_class=HTMLResponse)
def revoke_panels(request: Request, auth: Auth = Depends(require("profile")), db: Session = Depends(get_db)):
    n = revoke_user_sessions(db, auth.user.id, kind="panel")
    audit.record(db, auth.user, "user.panel_sessions_revoked", "user", auth.user.id, None, {"revoked": n}, auth.ip)
    db.commit()
    return render(request, "admin/profile.html", {"error": "", "flash": f"Signed out {n} Dashboard App session(s)."}, auth=auth, db=db)
