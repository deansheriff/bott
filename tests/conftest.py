from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import SessionLocal
from app.models import User
from app.security.passwords import hash_password
from app.security.ratelimit import limiter
from app.services import chatwoot
from app.services.chatwoot import ChatwootError
from app.services.llm import LLMResponse, set_provider
from app.services.llm.fake import FakeProvider

WEBHOOK_SECRET = "test-webhook-secret"
PASSWORD = "Correct-Horse-Battery-9"


class FakeChatwoot:
    """Records every Chatwoot API call made through the client factory."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.conversation_status = "pending"
        self.next_id = 10_000
        self.denied_tokens: set[str] = set()
        self.token = ""

    def __call__(self, token: str) -> "FakeChatwoot":
        self.token = token
        return self

    def send_message(self, conversation_id: int, content: str, private: bool = False) -> dict:
        self.next_id += 1
        self.calls.append(("send", {"conversation_id": conversation_id, "content": content, "private": private, "token": self.token}))
        return {"id": self.next_id}

    def toggle_status(self, conversation_id: int, status: str) -> dict:
        self.calls.append(("toggle", {"conversation_id": conversation_id, "status": status, "token": self.token}))
        return {"current_status": status}

    def get_conversation(self, conversation_id: int) -> dict:
        if self.token in self.denied_tokens:
            raise ChatwootError(404, "not found")
        self.calls.append(("get", {"conversation_id": conversation_id}))
        return {"id": conversation_id, "status": self.conversation_status, "meta": {"sender": {"id": 77, "name": "Ada", "phone_number": "+2348030000000"}}}

    def get_profile(self) -> dict:
        return {"id": 5, "name": "Agent Smith", "email": "agent@example.com", "accounts": [{"id": 1}]}

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def answer_responder(reply: str = "We open at 10:00.", confidence: float = 0.9, needs_human: bool = False, from_knowledge: bool = True):
    def responder(messages, opts):
        body = {"reply": reply, "confidence": confidence, "answered_from_knowledge": from_knowledge, "needs_human": needs_human, "reason": "test", "language": "en"}
        return LLMResponse(content=json.dumps(body), input_tokens=50, output_tokens=20, model=opts["model"])

    return responder


@pytest.fixture
def fake_cw(monkeypatch):
    fake = FakeChatwoot()
    monkeypatch.setattr(chatwoot, "client_factory", fake)
    return fake


@pytest.fixture
def fake_llm():
    provider = FakeProvider(answer_responder())
    set_provider(provider)
    yield provider
    set_provider(None)


@pytest.fixture
def settings(tmp_path) -> Settings:
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "hours.md").write_text("# Opening hours\n\n## Regular hours\n\nWe open every day from 10:00 to 22:00.\n", encoding="utf-8")
    (kdir / "delivery.md").write_text("# Delivery\n\n## Delivery areas\n\nWe deliver to Lekki and Victoria Island. Send your address to confirm the fee.\n\n## Pickup\n\nYou can pick up at the restaurant.\n", encoding="utf-8")
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        knowledge_dir=str(kdir),
        media_dir=str(tmp_path / "media"),
        session_secret="s" * 48,
        token_encryption_key=Fernet.generate_key().decode(),
        chatwoot_base_url="https://chat.example.com",
        chatwoot_account_id=1,
        chatwoot_bot_token="bot-token",
        chatwoot_webhook_secret=WEBHOOK_SECRET,
        cookie_secure=True,
        dashboard_admin_email="owner@example.com",
        dashboard_admin_password="Initial-Passw0rd-123",
        llm_provider="fake",
        restaurant_name="Test Kitchen",
    )


@pytest.fixture
def app(settings, fake_cw, fake_llm):
    from app.main import create_app
    from app.routers import panel

    limiter.reset()
    panel._access_cache.clear()
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def db(app):
    session = SessionLocal()
    yield session
    session.close()


def make_user(db, email: str, role: str = "admin", password: str = PASSWORD, must_change: bool = False) -> User:
    user = User(email=email, password_hash=hash_password(password), role=role, is_active=True, must_change_password=must_change)
    db.add(user)
    db.commit()
    return user


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_from(html: str) -> str:
    m = CSRF_RE.search(html)
    assert m, "no csrf token in page"
    return m.group(1)


def login(client: TestClient, email: str, password: str = PASSWORD, next_url: str = "/admin/"):
    page = client.get("/admin/login")
    token = csrf_from(page.text)
    return client.post("/admin/login", data={"email": email, "password": password, "csrf_token": token, "next": next_url}, follow_redirects=False)


def signed_headers(body: bytes, secret: str = WEBHOOK_SECRET, ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Chatwoot-Timestamp": str(ts), "X-Chatwoot-Signature": f"sha256={sig}", "Content-Type": "application/json"}


def message_payload(content: str = "What time do you open?", *, msg_id: int = 1, conv_id: int = 42, status: str = "pending", message_type: str = "incoming", sender_type: str = "contact", private: bool = False) -> dict:
    return {
        "event": "message_created",
        "id": msg_id,
        "content": content,
        "message_type": message_type,
        "private": private,
        "sender": {"id": 77, "name": "Ada", "phone_number": "+2348030000000", "type": sender_type},
        "conversation": {"display_id": conv_id, "id": conv_id, "status": status, "inbox_id": 3},
        "account": {"id": 1},
    }


def post_webhook(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/chatwoot", content=body, headers=signed_headers(body))
