from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db import session_scope
from ..models import WebhookEvent
from ..security.signature import verify_signature
from ..services import bot

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1_000_000


def _record_rejection(reason: str) -> None:
    with session_scope() as db:
        db.add(WebhookEvent(event="", outcome="rejected", reason=reason))


@router.post("/webhooks/chatwoot")
async def chatwoot_webhook(request: Request, background: BackgroundTasks):
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"ok": False}, status_code=413)
    raw = await request.body()  # raw bytes BEFORE any JSON parsing
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse({"ok": False}, status_code=413)

    ok, reason = verify_signature(
        get_settings().chatwoot_webhook_secret,
        request.headers.get("x-chatwoot-timestamp"),
        raw,
        request.headers.get("x-chatwoot-signature"),
    )
    if not ok:
        log.warning("webhook.rejected", extra={"reason": reason})
        await run_in_threadpool(_record_rejection, reason)
        return JSONResponse({"ok": False}, status_code=401)

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        await run_in_threadpool(_record_rejection, "bad_json")
        return JSONResponse({"ok": False}, status_code=400)

    action, reason, conv_id = await run_in_threadpool(bot.ingest_event, payload, request.headers.get("x-chatwoot-delivery"))
    if action == "process":
        background.add_task(bot.process_incoming, payload)
    elif action == "summarize" and conv_id:
        background.add_task(bot.summarize_task, conv_id)
    return {"ok": True}
