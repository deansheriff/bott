from datetime import timedelta

from app.models import AuthSession, User
from app.routers.admin_auth import safe_next
from app.utils import utcnow

from .conftest import PASSWORD, csrf_from, login, make_user


# ------------------------------------------------------------ admin site
def test_admin_requires_login(client):
    for path in ["/admin/", "/admin/settings", "/admin/users", "/admin/menu", "/admin/audit"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")


def test_login_sets_strict_cookie(client, db):
    make_user(db, "a@example.com")
    r = login(client, "a@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/admin/"
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and "__Host-rb_session" in v][0].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/admin/").status_code == 200


def test_bad_password(client, db):
    make_user(db, "a@example.com")
    assert login(client, "a@example.com", "wrong-password-123").status_code == 401
    assert login(client, "nobody@example.com").status_code == 401


def test_bootstrap_admin_must_change_password(client, db):
    admin = db.query(User).filter(User.email == "owner@example.com").one()
    assert admin.role == "admin" and admin.must_change_password
    r = login(client, "owner@example.com", "Initial-Passw0rd-123")
    assert r.headers["location"] == "/admin/password"
    r = client.get("/admin/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/password"
    page = client.get("/admin/password")
    r = client.post("/admin/password", data={"csrf_token": csrf_from(page.text), "current": "Initial-Passw0rd-123", "new": "A-much-better-pass-42", "confirm": "A-much-better-pass-42"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/admin/settings").status_code == 200


def test_roles_enforced_server_side(client, db):
    make_user(db, "staff@example.com", role="staff")
    login(client, "staff@example.com")
    for path in ["/admin/menu", "/admin/knowledge", "/admin/hours", "/admin/analytics", "/admin/"]:
        assert client.get(path).status_code == 200, path
    for path in ["/admin/settings", "/admin/users", "/admin/audit", "/admin/zones", "/admin/playground"]:
        assert client.get(path).status_code == 403, path
    page = client.get("/admin/menu")
    r = client.post("/admin/bot-switch", data={"value": "OFF", "csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert r.status_code == 403


def test_login_lockout(client, db):
    make_user(db, "a@example.com")
    for _ in range(5):
        login(client, "a@example.com", "wrong-password-123")
    r = login(client, "a@example.com")
    assert r.status_code == 429


def test_idle_and_absolute_timeouts(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    sess = db.query(AuthSession).one()
    sess.last_seen_at = utcnow() - timedelta(minutes=31)
    db.commit()
    assert client.get("/admin/", follow_redirects=False).status_code == 303

    login(client, "a@example.com")
    db.expire_all()
    sess = db.query(AuthSession).one()
    sess.created_at = utcnow() - timedelta(hours=13)
    db.commit()
    assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_tampered_cookie_rejected(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    real = client.cookies.get("__Host-rb_session")
    client.cookies.clear()
    client.cookies.set("__Host-rb_session", real[:-3] + ("AAA" if not real.endswith("AAA") else "BBB"))
    assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_safe_next():
    assert safe_next("/admin/menu") == "/admin/menu"
    for bad in ["https://evil.com", "//evil.com", "/app-panel/", "/admin\\..\\x", "javascript:alert(1)"]:
        assert safe_next(bad) == "/admin/"


def test_frame_headers(client):
    r = client.get("/admin/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    p = client.get("/app-panel/")
    assert "x-frame-options" not in p.headers
    assert "frame-ancestors 'self' https://chat.example.com" in p.headers["content-security-policy"]


def test_health_is_minimal(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ------------------------------------------------------------ dashboard app (iframe)
def _link_panel(client, db, email="agent@example.com", role="staff"):
    user = make_user(db, email, role=role)
    start = client.post("/app-panel/api/link/start").json()
    assert client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]}).status_code == 202
    login(client, email, next_url=f"/admin/panel-link?code={start['code']}")
    page = client.get(f"/admin/panel-link?code={start['code']}")
    assert start["code"] in page.text
    r = client.post("/admin/panel-link", data={"code": start["code"], "csrf_token": csrf_from(page.text)})
    assert r.status_code == 200
    client.cookies.clear()  # the iframe has no cookies
    claim = client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]})
    assert claim.status_code == 200
    body = claim.json()
    # The nonce is single-use.
    assert client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]}).status_code == 410
    return user, {"Authorization": f"Bearer {body['token']}", "X-CSRF-Token": body["csrf"]}


def test_panel_requires_bearer(client):
    assert client.get("/app-panel/view?conversation_id=42").status_code == 401
    assert client.get("/app-panel/view?conversation_id=42", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_panel_rejects_admin_cookie(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    assert client.get("/app-panel/view?conversation_id=42").status_code == 401


def test_panel_link_flow_and_token_requirement(client, db, fake_cw):
    user, headers = _link_panel(client, db)
    r = client.get("/app-panel/view?conversation_id=42", headers=headers)
    assert r.status_code == 200 and "Connect your Chatwoot API token" in r.text

    from app.security.crypto import encrypt_secret

    db.expire_all()
    u = db.get(User, user.id)
    u.chatwoot_token_enc = encrypt_secret("agent-token")
    db.commit()
    r = client.get("/app-panel/view?conversation_id=42", headers=headers)
    assert r.status_code == 200 and "Conversation #42" in r.text


def test_panel_denies_conversation_agent_cannot_see(client, db, fake_cw):
    from app.security.crypto import encrypt_secret

    user, headers = _link_panel(client, db)
    db.expire_all()
    u = db.get(User, user.id)
    u.chatwoot_token_enc = encrypt_secret("limited-token")
    db.commit()
    fake_cw.denied_tokens.add("limited-token")
    r = client.get("/app-panel/view?conversation_id=99", headers=headers)
    assert "can't access this conversation" in r.text


def test_panel_send_uses_agent_token(client, db, fake_cw):
    from app.security.crypto import encrypt_secret

    user, headers = _link_panel(client, db)
    db.expire_all()
    u = db.get(User, user.id)
    u.chatwoot_token_enc = encrypt_secret("agent-token")
    db.commit()
    r = client.post("/app-panel/api/conversations/42/send", data={"content": "Hello from a human"}, headers=headers)
    assert r.status_code == 200
    sends = [c for k, c in fake_cw.calls if k == "send"]
    assert sends[-1]["token"] == "agent-token" and sends[-1]["content"] == "Hello from a human"


def test_panel_session_idle_timeout(client, db, fake_cw):
    _, headers = _link_panel(client, db)
    sess = db.query(AuthSession).filter(AuthSession.kind == "panel").one()
    sess.last_seen_at = utcnow() - timedelta(minutes=31)
    db.commit()
    assert client.get("/app-panel/view?conversation_id=42", headers=headers).status_code == 401


def test_unconfirmed_link_cannot_be_claimed(client):
    start = client.post("/app-panel/api/link/start").json()
    r = client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]})
    assert r.status_code == 202 and "token" not in r.json()
    assert client.post("/app-panel/api/link/claim", json={"nonce": "made-up"}).status_code == 410


def test_password_reused_hash_not_exposed(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    html = client.get("/admin/profile").text
    assert "argon2" not in html and PASSWORD not in html
