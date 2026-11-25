from app.models import AuditLog
from app.security.crypto import encrypt_secret
from app.services import settings_store

from .conftest import csrf_from, login, make_user


def _admin(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    return csrf_from(client.get("/admin/").text)


def test_state_change_without_token_rejected(client, db):
    _admin(client, db)
    r = client.post("/admin/bot-switch", data={"value": "OFF"}, follow_redirects=False)
    assert r.status_code == 403
    assert settings_store.get_setting(db, "bot_switch") == "ON"


def test_wrong_token_rejected(client, db):
    _admin(client, db)
    r = client.post("/admin/bot-switch", data={"value": "OFF", "csrf_token": "not-the-token"}, follow_redirects=False)
    assert r.status_code == 403


def test_form_token_accepted_and_audited(client, db):
    token = _admin(client, db)
    r = client.post("/admin/bot-switch", data={"value": "PAUSED", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert settings_store.get_setting(db, "bot_switch") == "PAUSED"
    row = db.query(AuditLog).filter(AuditLog.action == "settings.bot_switch").one()
    assert row.before == "ON" and row.after == "PAUSED" and row.user_email == "a@example.com"


def test_htmx_header_token_accepted(client, db):
    token = _admin(client, db)
    r = client.post("/admin/knowledge/preview", data={"content": "# Hi <script>alert(1)</script>"}, headers={"X-CSRF-Token": token, "HX-Request": "true"})
    assert r.status_code == 200
    assert "<h1>" in r.text and "<script>" not in r.text  # preview is sanitised
    assert client.post("/admin/knowledge/preview", data={"content": "x"}, headers={"HX-Request": "true"}).status_code == 403


def test_cross_origin_rejected_even_with_token(client, db):
    token = _admin(client, db)
    r = client.post("/admin/bot-switch", data={"value": "OFF", "csrf_token": token}, headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403


def test_login_form_requires_csrf(client, db):
    make_user(db, "a@example.com")
    r = client.post("/admin/login", data={"email": "a@example.com", "password": "x", "csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403


def test_csrf_token_is_per_session(client, db):
    token1 = _admin(client, db)
    client.cookies.clear()
    login(client, "a@example.com")
    r = client.post("/admin/bot-switch", data={"value": "OFF", "csrf_token": token1}, follow_redirects=False)
    assert r.status_code == 403


def _panel_headers(client, db):
    from app.models import User

    user = make_user(db, "agent@example.com", role="staff")
    user.chatwoot_token_enc = encrypt_secret("agent-token")
    db.commit()
    start = client.post("/app-panel/api/link/start").json()
    login(client, "agent@example.com")
    page = client.get(f"/admin/panel-link?code={start['code']}")
    client.post("/admin/panel-link", data={"code": start["code"], "csrf_token": csrf_from(page.text)})
    client.cookies.clear()
    body = client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]}).json()
    return {"Authorization": f"Bearer {body['token']}"}, body["csrf"]


def test_panel_post_requires_csrf_header(client, db, fake_cw):
    auth, csrf = _panel_headers(client, db)
    assert client.post("/app-panel/api/conversations/42/takeover", headers=auth).status_code == 403
    assert client.post("/app-panel/api/conversations/42/takeover", headers={**auth, "X-CSRF-Token": "wrong"}).status_code == 403
    r = client.post("/app-panel/api/conversations/42/takeover", headers={**auth, "X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert ("toggle", {"conversation_id": 42, "status": "open", "token": "agent-token"}) in fake_cw.calls


def test_translate_requires_csrf(client, db, fake_llm):
    auth, csrf = _panel_headers(client, db)
    assert client.post("/copilot/translate", json={"text": "hello", "target": "yo"}, headers=auth).status_code == 403
    r = client.post("/copilot/translate", json={"text": "hello", "target": "yo"}, headers={**auth, "X-CSRF-Token": csrf})
    assert r.status_code == 200 and "translation" in r.json()
    assert client.post("/copilot/translate", json={"text": "hello", "target": "yo"}).status_code == 401
