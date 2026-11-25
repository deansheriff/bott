"""Environment configuration (pydantic-settings).

Values here are *boot-time* settings. Some of them (model, spend cap, timezone)
are only initial defaults: once an admin changes them on /admin/settings the
database value wins. See app/services/settings_store.py.
"""
from __future__ import annotations

import base64
import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"  # production | development | test
    log_level: str = "INFO"

    # Chatwoot
    chatwoot_base_url: str = "http://localhost:3000"  # used for server-to-server API calls
    chatwoot_public_url: str = ""  # browser-facing URL of Chatwoot; defaults to chatwoot_base_url
    chatwoot_account_id: int = 1
    chatwoot_bot_token: str = ""
    chatwoot_webhook_secret: str = ""

    # LLM
    llm_provider: str = "openai"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    daily_llm_spend_cap_usd: float = 5.0

    # Storage
    database_url: str = "sqlite:///./data/dev.db"
    knowledge_dir: str = "./knowledge"
    media_dir: str = "./data/media"

    # Security
    session_secret: str = ""
    token_encryption_key: str = ""
    cookie_secure: bool = True
    dashboard_admin_email: str = ""
    dashboard_admin_password: str = ""
    dashboard_allowed_ips: str = ""

    # Restaurant
    restaurant_name: str = "Our Restaurant"
    timezone: str = "Africa/Lagos"

    # Operations
    seed_demo_data: bool = False
    webhook_rate_limit_per_minute: int = 12
    max_knowledge_file_bytes: int = 200_000
    max_photo_bytes: int = 2_000_000
    max_upload_bytes: int = 10_000_000  # knowledge document uploads (PDF/DOCX/TXT/MD)

    # ------------------------------------------------------------------ helpers
    @property
    def chatwoot_api_base(self) -> str:
        return self.chatwoot_base_url.rstrip("/")

    @property
    def chatwoot_browser_origin(self) -> str:
        """Origin of the Chatwoot dashboard as the browser sees it (scheme://host[:port])."""
        url = (self.chatwoot_public_url or self.chatwoot_base_url).strip()
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def allowed_ip_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        nets = []
        for part in self.dashboard_allowed_ips.split(","):
            part = part.strip()
            if part:
                nets.append(ipaddress.ip_network(part, strict=False))
        return nets

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def production_problems(self) -> list[str]:
        """Return configuration problems that must block a production boot."""
        problems: list[str] = []
        if len(self.session_secret) < 32:
            problems.append("SESSION_SECRET must be at least 32 characters")
        if not self.chatwoot_webhook_secret:
            problems.append("CHATWOOT_WEBHOOK_SECRET is required")
        if not self.chatwoot_bot_token:
            problems.append("CHATWOOT_BOT_TOKEN is required")
        if not _valid_fernet_key(self.token_encryption_key):
            problems.append("TOKEN_ENCRYPTION_KEY must be a Fernet key (32 url-safe base64 bytes)")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production (HTTPS only)")
        if not self.chatwoot_browser_origin.startswith("https://"):
            problems.append("Chatwoot browser URL (CHATWOOT_PUBLIC_URL or CHATWOOT_BASE_URL) must be https")
        try:
            self.allowed_ip_networks
        except ValueError as exc:
            problems.append(f"DASHBOARD_ALLOWED_IPS is invalid: {exc}")
        return problems


def _valid_fernet_key(key: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(key.encode())) == 32
    except Exception:
        return False


_override: Settings | None = None


@lru_cache
def _load() -> Settings:
    return Settings()


def get_settings() -> Settings:
    return _override or _load()


def set_settings(settings: Settings | None) -> None:
    """Used by the app factory and tests to pin the active settings object."""
    global _override
    _override = settings
