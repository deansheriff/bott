from __future__ import annotations

import logging
from decimal import Decimal
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, User

log = logging.getLogger(__name__)

_SECRET_KEYS = ("password", "token", "secret", "api_key", "chatwoot_token_enc")


def scrub(value: Any) -> Any:
    """Make a value JSON-safe and mask anything that looks like a secret."""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in _SECRET_KEYS) and v else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def record(
    db: Session,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    ip: str | None = None,
) -> None:
    """Append an audit row. Caller is responsible for commit (same transaction as the change)."""
    db.add(
        AuditLog(
            user_id=actor.id if actor else None,
            user_email=actor.email if actor else "system",
            action=action,
            entity=entity,
            entity_id=None if entity_id is None else str(entity_id),
            before=scrub(before),
            after=scrub(after),
            ip=ip,
        )
    )
    log.info("audit", extra={"action": action, "entity": entity, "entity_id": str(entity_id), "actor_id": actor.id if actor else None})


def model_snapshot(obj: Any, fields: list[str]) -> dict[str, Any]:
    return {f: scrub(getattr(obj, f, None)) for f in fields}
