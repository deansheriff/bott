from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import Auth, require_panel_or_web
from ..services import audit
from ..services.copilot import LANGUAGES, translate
from ..services.llm import LLMError
from ..services.spend import SpendCapReached

router = APIRouter(prefix="/copilot")


@router.post("/translate")
async def translate_endpoint(request: Request, auth: Auth = Depends(require_panel_or_web()), db: Session = Depends(get_db)):
    if "json" in request.headers.get("content-type", ""):
        body = await request.json()
    else:
        body = dict(await request.form())
    text = str(body.get("text") or "").strip()
    target = str(body.get("target") or "")
    conv_id = body.get("conversation_id")
    if not text or len(text) > 3000:
        return JSONResponse({"error": "text is required (max 3000 characters)"}, status_code=422)
    if target not in LANGUAGES:
        return JSONResponse({"error": f"target must be one of {sorted(LANGUAGES)}"}, status_code=422)
    try:
        result = translate(db, text, target, int(conv_id) if str(conv_id or "").isdigit() else None)
    except SpendCapReached:
        return JSONResponse({"error": "Daily LLM spend cap reached"}, status_code=429)
    except LLMError:
        return JSONResponse({"error": "Translation service unavailable"}, status_code=502)
    audit.record(db, auth.user, "copilot.translate", "conversation", conv_id, None, {"target": target, "chars": len(text)}, auth.ip)
    db.commit()
    return {"translation": result, "target": target}
