from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import get_settings
from .deps import PERMISSIONS, Auth
from .services.spend import cap_reached
from .utils import format_money, to_local

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))  # autoescape on for .html


def _local(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    from .services.settings_store import defaults

    if dt is None:
        return ""
    local = to_local(dt, defaults()["timezone"] if _tz_cache is None else _tz_cache)
    return local.strftime(fmt) if local else ""


_tz_cache: str | None = None
templates.env.filters["local"] = _local
templates.env.filters["money"] = lambda v, sym="₦": format_money(v, sym)
templates.env.globals["restaurant_name"] = lambda: get_settings().restaurant_name


def set_display_timezone(tz: str) -> None:
    global _tz_cache
    _tz_cache = tz


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, *, auth: Auth | None = None, db: Session | None = None, status_code: int = 200, headers: dict | None = None):
    context: dict[str, Any] = dict(ctx or {})
    context.setdefault("auth", auth)
    context.setdefault("user", auth.user if auth else None)
    context.setdefault("csrf_token", auth.csrf if auth else context.get("csrf_token", ""))
    context.setdefault("can", (lambda p: auth.user.role in PERMISSIONS.get(p, set())) if auth else (lambda p: False))
    if db is not None and auth is not None:
        from .services.settings_store import get_setting

        set_display_timezone(get_setting(db, "timezone"))
        context.setdefault("spend_cap_reached", cap_reached(db))
        context.setdefault("bot_switch", get_setting(db, "bot_switch"))
        context.setdefault("currency", get_setting(db, "currency_symbol"))
    context.setdefault("currency", "₦")
    return templates.TemplateResponse(request, name, context, status_code=status_code, headers=headers)
