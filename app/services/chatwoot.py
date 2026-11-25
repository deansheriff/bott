"""Minimal Chatwoot Application API client.

Endpoints (developers.chatwoot.com API reference):
  POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages
  POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/toggle_status
  GET  /api/v1/accounts/{account_id}/conversations/{conversation_id}
  GET  /api/v1/profile            (user tokens only)
The bot token is for bot replies and handoff notes only; agent actions use the agent's own token.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class ChatwootError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class ChatwootClient:
    def __init__(self, base_url: str, account_id: int, token: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"api_access_token": token, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            resp = self._client.request(method, path, json=json)
        except httpx.ConnectError:
            resp = self._client.request(method, path, json=json)  # one retry on connect failure only
        except httpx.HTTPError as exc:
            log.warning("chatwoot.http_error", extra={"method": method, "error": type(exc).__name__})
            raise ChatwootError(None, type(exc).__name__) from exc
        if resp.status_code >= 400:
            log.warning("chatwoot.bad_status", extra={"method": method, "status": resp.status_code})
            raise ChatwootError(resp.status_code, f"Chatwoot returned {resp.status_code}")
        return resp.json() if resp.content else {}

    def _conv(self, conversation_id: int) -> str:
        return f"/api/v1/accounts/{self.account_id}/conversations/{int(conversation_id)}"

    def send_message(self, conversation_id: int, content: str, private: bool = False) -> dict:
        body = {"content": content, "message_type": "outgoing", "private": private}
        return self._request("POST", f"{self._conv(conversation_id)}/messages", json=body)

    def toggle_status(self, conversation_id: int, status: str) -> dict:
        if status not in ("open", "resolved", "pending", "snoozed"):
            raise ValueError("invalid status")
        return self._request("POST", f"{self._conv(conversation_id)}/toggle_status", json={"status": status})

    def get_conversation(self, conversation_id: int) -> dict:
        return self._request("GET", self._conv(conversation_id))

    def get_profile(self) -> dict:
        return self._request("GET", "/api/v1/profile")

    def close(self) -> None:
        self._client.close()


def _default_factory(token: str) -> ChatwootClient:
    s = get_settings()
    return ChatwootClient(s.chatwoot_api_base, s.chatwoot_account_id, token)


client_factory: Callable[[str], ChatwootClient] = _default_factory


def bot_client() -> ChatwootClient:
    return client_factory(get_settings().chatwoot_bot_token)


def agent_client(token: str) -> ChatwootClient:
    return client_factory(token)


def connection_test() -> dict:
    """Check reachability (GET /api) and whether the bot token is accepted.

    The bot token cannot call /profile, so we request a conversation id that should not
    exist: 404 means the token was accepted, 401 means it was rejected.
    """
    s = get_settings()
    result: dict[str, Any] = {"base_url": s.chatwoot_api_base}
    try:
        r = httpx.get(f"{s.chatwoot_api_base}/api", timeout=8)
        result["reachable"] = r.status_code < 500
        result["reachable_status"] = r.status_code
        try:
            result["version"] = r.json().get("version")
        except Exception:
            result["version"] = None
    except httpx.HTTPError as exc:
        result.update(reachable=False, error=type(exc).__name__)
        return result
    try:
        r = httpx.get(f"{s.chatwoot_api_base}/api/v1/accounts/{s.chatwoot_account_id}/conversations/999999999", headers={"api_access_token": s.chatwoot_bot_token}, timeout=8)
        result["bot_token_status"] = r.status_code
        result["bot_token_ok"] = r.status_code in (200, 404)
    except httpx.HTTPError as exc:
        result.update(bot_token_ok=False, error=type(exc).__name__)
    return result
