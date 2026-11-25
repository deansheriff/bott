"""Admin site pages (all require a session; roles enforced via deps.require)."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import Auth, require
from ..models import (
    AuditLog,
    AuthSession,
    BotTurn,
    ConversationState,
    DateOverride,
    DeliveryZone,
    KnowledgeVersion,
    LLMUsage,
    MenuItem,
    MessageLog,
    PanelLink,
    Role,
    ToolCallLog,
    User,
    UserToken,
    WebhookEvent,
    WeeklyHours,
)
from ..security.permissions import ACTION_LABELS, ADMIN_ROLE, AREAS, clean
from ..security.sessions import revoke_user_sessions
from ..services import audit, rbac, settings_store
from ..services.answer import generate_answer
from ..services.chatwoot import connection_test
from ..services.handoff import post_answer_check, pre_answer_check
from ..services.hours import DAY_NAMES, opening_status, valid_hhmm
from ..services.importer import ImportErrorUser, convert_upload, suggest_filename
from ..services.rbac import can_grant
from ..services.simulate import TranscriptError, simulate
from ..services.knowledge import FILENAME_RE, get_kb, latest_version_no, render_markdown_safe, save_version, unified_diff
from ..services.llm import DEFAULT_MODELS, PROVIDERS, LLMError, key_configured, model_matches
from ..services.spend import SpendCapReached, local_day_bounds_utc, spend_today
from ..templating import render
from ..utils import csv_safe, is_valid_tz, random_token, safe_tz, sha256_hex, utcnow

router = APIRouter(prefix="/admin")
log = logging.getLogger(__name__)

MENU_FIELDS = ["name", "category", "price", "available", "eighty_sixed_on", "description", "photo_url"]
ZONE_FIELDS = ["name", "aliases", "fee", "min_order", "active"]
MAX_PRICE = Decimal("100000000")


def _today_local(db: Session) -> date:
    return datetime.now(timezone.utc).astimezone(safe_tz(settings_store.get_setting(db, "timezone"))).date()


def _money(value: str) -> Decimal | None:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if d < 0 or d > MAX_PRICE or d.as_tuple().exponent < -2:
        return None
    return d


# ================================================================== overview
@router.get("/", response_class=HTMLResponse)
def overview(request: Request, auth: Auth = Depends(require("overview.view")), db: Session = Depends(get_db)):
    start, end = local_day_bounds_utc(settings_store.get_setting(db, "timezone"))
    today_turns = db.query(BotTurn).filter(BotTurn.created_at >= start, BotTurn.dry_run.is_(False))
    handed = {t.conversation_id for t in today_turns.filter(BotTurn.handoff.is_(True)).all()}
    replied = {t.conversation_id for t in today_turns.filter(BotTurn.handoff.is_(False), BotTurn.reply != "").all()}
    avg_latency = db.query(func.avg(BotTurn.latency_ms)).filter(BotTurn.created_at >= start, BotTurn.dry_run.is_(False), BotTurn.handoff.is_(False)).scalar()
    since = utcnow() - timedelta(hours=24)
    wh = {o: c for o, c in db.query(WebhookEvent.outcome, func.count(WebhookEvent.id)).filter(WebhookEvent.received_at >= since).group_by(WebhookEvent.outcome).all()}
    last_event = db.query(WebhookEvent).filter(WebhookEvent.outcome != "rejected").order_by(WebhookEvent.id.desc()).first()
    last_reject = db.query(WebhookEvent).filter(WebhookEvent.outcome.in_(["rejected", "error"])).order_by(WebhookEvent.id.desc()).first()
    stats = {
        "open_conversations": db.query(ConversationState).filter(ConversationState.last_status == "open").count(),
        "bot_handled_today": len(replied - handed),
        "handoffs_today": len(handed),
        "avg_response_ms": int(avg_latency) if avg_latency else None,
        "spend_today": spend_today(db),
        "spend_cap": float(settings_store.get_setting(db, "daily_spend_cap_usd")),
        "webhooks": wh,
        "last_event": last_event,
        "last_reject": last_reject,
        "chunks": get_kb().chunk_count,
    }
    return render(request, "admin/overview.html", {"stats": stats}, auth=auth, db=db)


@router.post("/bot-switch")
def bot_switch(request: Request, value: str = Form(...), auth: Auth = Depends(require("bot_switch.edit")), db: Session = Depends(get_db)):
    value = value.upper()
    if value not in settings_store.BOT_SWITCH_VALUES:
        return RedirectResponse("/admin/", status_code=303)
    before, after = settings_store.set_setting(db, "bot_switch", value)
    audit.record(db, auth.user, "settings.bot_switch", "setting", "bot_switch", before, after, auth.ip)
    db.commit()
    return RedirectResponse("/admin/", status_code=303)


# ================================================================== knowledge
def _knowledge_list(request: Request, db: Session, auth: Auth, error: str = "", flash: str = "", status: int = 200):
    kb = get_kb()
    on_disk = kb.list_files()
    counts = dict(db.query(KnowledgeVersion.filename, func.count(KnowledgeVersion.id)).group_by(KnowledgeVersion.filename).all())
    files = [{"name": n, "size": len(kb.read(n).encode()), "versions": counts.get(n, 0)} for n in on_disk]
    deleted = []
    for name in sorted(set(counts) - set(on_disk)):
        last = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).order_by(KnowledgeVersion.version.desc()).first()
        deleted.append({"name": name, "versions": counts[name], "last": last})
    return render(request, "admin/knowledge_list.html", {"files": files, "deleted": deleted, "chunks": kb.chunk_count, "error": error, "flash": flash}, auth=auth, db=db, status_code=status)


@router.get("/knowledge", response_class=HTMLResponse)
def knowledge_list(request: Request, auth: Auth = Depends(require("knowledge.view")), db: Session = Depends(get_db)):
    return _knowledge_list(request, db, auth)


@router.post("/knowledge/{name}/delete", response_class=HTMLResponse)
def knowledge_delete(request: Request, name: str, auth: Auth = Depends(require("knowledge.delete")), db: Session = Depends(get_db)):
    """Remove a file from the bot. Its versions are kept so it can be restored (or purged)."""
    kb = get_kb()
    if not FILENAME_RE.match(name) or not kb.exists(name):
        return _knowledge_list(request, db, auth, error="No such knowledge file.", status=404)
    content = kb.read(name)
    if latest_version_no(db, name) == 0:  # make sure there's something to restore from
        save_version(db, kb, name, content, auth.user, "snapshot before delete")
    kb.delete(name)
    audit.record(db, auth.user, "knowledge.delete", "knowledge_file", name, {"sha256": sha256_hex(content), "bytes": len(content)}, None, auth.ip)
    db.commit()
    kb.reindex()
    return _knowledge_list(request, db, auth, flash=f"{name} deleted. The bot no longer uses it. You can restore it from “Deleted files” below.")


@router.post("/knowledge/deleted/{name}/restore", response_class=HTMLResponse)
def knowledge_restore(request: Request, name: str, auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    kb = get_kb()
    last = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).order_by(KnowledgeVersion.version.desc()).first() if FILENAME_RE.match(name) else None
    if last is None or kb.exists(name):
        return _knowledge_list(request, db, auth, error="Nothing to restore.", status=404)
    v = save_version(db, kb, name, last.content, auth.user, f"restored from v{last.version}")
    audit.record(db, auth.user, "knowledge.restore", "knowledge_file", name, None, {"version": v.version, "from_version": last.version}, auth.ip)
    db.commit()
    kb.reindex()
    return _knowledge_list(request, db, auth, flash=f"{name} restored.")


@router.post("/knowledge/deleted/{name}/purge", response_class=HTMLResponse)
def knowledge_purge(request: Request, name: str, auth: Auth = Depends(require("knowledge.delete")), db: Session = Depends(get_db)):
    """Permanently delete a deleted file's version history."""
    kb = get_kb()
    if not FILENAME_RE.match(name) or kb.exists(name):
        return _knowledge_list(request, db, auth, error="Delete the file first; only deleted files can be purged.", status=400)
    n = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).delete(synchronize_session=False)
    if not n:
        return _knowledge_list(request, db, auth, error="Nothing to purge.", status=404)
    audit.record(db, auth.user, "knowledge.purge", "knowledge_file", name, {"versions": n}, None, auth.ip)
    db.commit()
    return _knowledge_list(request, db, auth, flash=f"{name} and its {n} version(s) were permanently deleted.")


@router.post("/knowledge/new")
def knowledge_new(request: Request, filename: str = Form(...), auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    name = filename.strip().lower()
    if not name.endswith(".md"):
        name += ".md"
    kb = get_kb()
    if not FILENAME_RE.match(name) or kb.exists(name):
        return _knowledge_list(request, db, auth, error="Invalid or existing filename.", status=400)
    if latest_version_no(db, name):
        return _knowledge_list(request, db, auth, error=f"{name} is in “Deleted files”. Restore it, or purge it first.", status=400)
    v = save_version(db, kb, name, f"# {name[:-3].replace('-', ' ').title()}\n\nWrite here.\n", auth.user, "created")
    audit.record(db, auth.user, "knowledge.create", "knowledge_file", name, None, {"version": v.version}, auth.ip)
    db.commit()
    kb.reindex()
    return RedirectResponse(f"/admin/knowledge/{name}", status_code=303)


@router.post("/knowledge/reindex")
def knowledge_reindex(request: Request, auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    n = get_kb().reindex()
    audit.record(db, auth.user, "knowledge.reindex", "knowledge_index", None, None, {"chunks": n}, auth.ip)
    db.commit()
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/knowledge/upload", response_class=HTMLResponse)
async def knowledge_upload(request: Request, document: UploadFile | None = File(None), save_as: str = Form(""), auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    """Convert an uploaded PDF/DOCX/TXT/MD to Markdown and open it in the editor UNSAVED for review."""
    kb = get_kb()
    s = get_settings()

    def back(error: str, status: int = 400):
        return _knowledge_list(request, db, auth, error=error, status=status)

    if document is None or not document.filename:
        return back("Choose a file to upload.")
    data = await document.read(s.max_upload_bytes + 1)
    original = PurePath(document.filename).name[:120]
    name = (save_as.strip().lower() or suggest_filename(original))
    if not name.endswith(".md"):
        name += ".md"
    if not FILENAME_RE.match(name):
        return back("“Save as” must be lowercase letters, digits, '-' or '_' (e.g. faq.md).")
    try:
        result = await run_in_threadpool(convert_upload, data, original, s.max_upload_bytes)
    except ImportErrorUser as exc:
        return back(str(exc))
    except Exception:
        log.exception("knowledge.import_failed")
        return back("This file couldn't be converted.")
    log.info("knowledge.import_converted", extra={"kind": result.kind, "bytes_in": len(data), "chars_out": len(result.markdown)})
    exists = kb.exists(name)
    notice = (
        f"Imported from {original}. Nothing is live yet: review the text, then click “Save version”."
        + (f" Saving will replace the current content of {name} (as a new version you can roll back)." if exists else f" Saving creates the new file {name}.")
    )
    versions = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).order_by(KnowledgeVersion.version.desc()).limit(50).all()
    errors = kb.validate(name, result.markdown, s.max_knowledge_file_bytes)
    return render(
        request,
        "admin/knowledge_edit.html",
        {"name": name, "content": result.markdown, "preview": render_markdown_safe(result.markdown[: s.max_knowledge_file_bytes]), "versions": versions, "errors": errors, "warnings": result.warnings, "notice": notice, "note": f"imported from {original}", "max_bytes": s.max_knowledge_file_bytes, "is_new": not exists},
        auth=auth,
        db=db,
    )


@router.post("/knowledge/preview", response_class=HTMLResponse)
def knowledge_preview(request: Request, content: str = Form(""), auth: Auth = Depends(require("knowledge.edit"))):
    return HTMLResponse(render_markdown_safe(content[: get_settings().max_knowledge_file_bytes]))


def _file_or_404(name: str):
    kb = get_kb()
    if not FILENAME_RE.match(name) or not kb.exists(name):
        return None
    return kb


@router.get("/knowledge/{name}", response_class=HTMLResponse)
def knowledge_edit(request: Request, name: str, auth: Auth = Depends(require("knowledge.view")), db: Session = Depends(get_db)):
    kb = _file_or_404(name)
    if kb is None:
        return render(request, "admin/message.html", {"title": "Not found", "message": "No such knowledge file."}, auth=auth, db=db, status_code=404)
    content = kb.read(name)
    versions = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).order_by(KnowledgeVersion.version.desc()).limit(50).all()
    return render(request, "admin/knowledge_edit.html", {"name": name, "content": content, "preview": render_markdown_safe(content), "versions": versions, "errors": [], "max_bytes": get_settings().max_knowledge_file_bytes}, auth=auth, db=db)


@router.post("/knowledge/{name}", response_class=HTMLResponse)
def knowledge_save(request: Request, name: str, content: str = Form(""), note: str = Form(""), create: str = Form(""), auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    kb = get_kb()
    exists = kb.exists(name)
    # New files are only created from the import flow (create=1); otherwise the file must exist.
    if not FILENAME_RE.match(name) or (not exists and create != "1"):
        return render(request, "admin/message.html", {"title": "Not found", "message": "No such knowledge file."}, auth=auth, db=db, status_code=404)
    content = content.replace("\r\n", "\n")
    errors = kb.validate(name, content, get_settings().max_knowledge_file_bytes)
    if errors:
        versions = db.query(KnowledgeVersion).filter(KnowledgeVersion.filename == name).order_by(KnowledgeVersion.version.desc()).limit(50).all()
        return render(request, "admin/knowledge_edit.html", {"name": name, "content": content, "preview": render_markdown_safe(content[:200_000]), "versions": versions, "errors": errors, "note": note, "is_new": not exists, "max_bytes": get_settings().max_knowledge_file_bytes}, auth=auth, db=db, status_code=400)
    if not exists:
        v = save_version(db, kb, name, content, auth.user, note)
        audit.record(db, auth.user, "knowledge.import" if note.startswith("imported from") else "knowledge.create", "knowledge_file", name, None, {"version": v.version, "sha256": v.sha256, "bytes": len(content), "note": v.note}, auth.ip)
        db.commit()
        kb.reindex()
        return RedirectResponse(f"/admin/knowledge/{name}", status_code=303)
    old = kb.read(name)
    if old != content:
        v = save_version(db, kb, name, content, auth.user, note)
        audit.record(db, auth.user, "knowledge.update", "knowledge_file", name, {"sha256": sha256_hex(old), "bytes": len(old)}, {"version": v.version, "sha256": v.sha256, "bytes": len(content)}, auth.ip)
        db.commit()
        kb.reindex()
    return RedirectResponse(f"/admin/knowledge/{name}", status_code=303)


@router.get("/knowledge/{name}/versions/{version_id}", response_class=HTMLResponse)
def knowledge_version(request: Request, name: str, version_id: int, auth: Auth = Depends(require("knowledge.view")), db: Session = Depends(get_db)):
    kb = _file_or_404(name)
    v = db.get(KnowledgeVersion, version_id)
    if kb is None or v is None or v.filename != name:
        return render(request, "admin/message.html", {"title": "Not found", "message": "No such version."}, auth=auth, db=db, status_code=404)
    diff = unified_diff(kb.read(name), v.content, "current", f"version {v.version}")
    return render(request, "admin/knowledge_version.html", {"name": name, "v": v, "diff": diff}, auth=auth, db=db)


@router.post("/knowledge/{name}/versions/{version_id}/rollback")
def knowledge_rollback(request: Request, name: str, version_id: int, auth: Auth = Depends(require("knowledge.edit")), db: Session = Depends(get_db)):
    kb = _file_or_404(name)
    v = db.get(KnowledgeVersion, version_id)
    if kb is None or v is None or v.filename != name:
        return RedirectResponse("/admin/knowledge", status_code=303)
    old = kb.read(name)
    nv = save_version(db, kb, name, v.content, auth.user, f"rollback to v{v.version}")
    audit.record(db, auth.user, "knowledge.rollback", "knowledge_file", name, {"sha256": sha256_hex(old)}, {"version": nv.version, "from_version": v.version}, auth.ip)
    db.commit()
    kb.reindex()
    return RedirectResponse(f"/admin/knowledge/{name}", status_code=303)


# ================================================================== menu
@router.get("/menu", response_class=HTMLResponse)
def menu_list(request: Request, auth: Auth = Depends(require("menu.view")), db: Session = Depends(get_db)):
    items = db.query(MenuItem).order_by(MenuItem.category, MenuItem.name).all()
    return render(request, "admin/menu_list.html", {"items": items, "today": _today_local(db)}, auth=auth, db=db)


@router.get("/menu/new", response_class=HTMLResponse)
@router.get("/menu/{item_id}/edit", response_class=HTMLResponse)
def menu_form(request: Request, item_id: int | None = None, auth: Auth = Depends(require("menu.edit")), db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id) if item_id else None
    return render(request, "admin/menu_form.html", {"item": item, "errors": [], "form": {}}, auth=auth, db=db)


async def _save_photo(upload: UploadFile | None) -> tuple[str | None, str | None]:
    """Validate by magic bytes and size; store under MEDIA_DIR/photos with a random name."""
    if upload is None or not upload.filename:
        return None, None
    data = await upload.read(get_settings().max_photo_bytes + 1)
    if len(data) > get_settings().max_photo_bytes:
        return None, "Photo is larger than the size limit."
    if data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        return None, "Photo must be a JPEG, PNG or WebP image."
    folder = Path(get_settings().media_dir) / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(12)}.{ext}"
    (folder / name).write_bytes(data)
    return f"/media/photos/{name}", None


_LOCAL_PHOTO_RE = re.compile(r"^/media/photos/([0-9a-f]{24}\.(?:jpg|png|webp))$")


def _remove_local_photo(db: Session, url: str | None) -> None:
    """Delete an uploaded photo file once no menu item uses it any more."""
    m = _LOCAL_PHOTO_RE.match(url or "")
    if not m or db.query(MenuItem).filter(MenuItem.photo_url == url).count():
        return
    (Path(get_settings().media_dir) / "photos" / m.group(1)).unlink(missing_ok=True)


@router.post("/menu/save", response_class=HTMLResponse)
async def menu_save(
    request: Request,
    item_id: str = Form(""),
    name: str = Form(""),
    category: str = Form(""),
    price: str = Form(""),
    available: str = Form(""),
    eighty_sixed: str = Form(""),
    description: str = Form(""),
    photo_url: str = Form(""),
    photo: UploadFile | None = File(None),
    auth: Auth = Depends(require("menu.edit")),
    db: Session = Depends(get_db),
):
    errors = []
    item = db.get(MenuItem, int(item_id)) if item_id.isdigit() else None
    name, category, description, photo_url = name.strip()[:120], category.strip()[:80], description.strip()[:2000], photo_url.strip()[:500]
    p = _money(price)
    if not name:
        errors.append("Name is required.")
    if p is None:
        errors.append("Price must be a non-negative number with at most 2 decimals.")
    if photo_url and not (photo_url.startswith("https://") or photo_url.startswith("/media/photos/")):
        errors.append("Photo URL must start with https://")
    uploaded, photo_err = await _save_photo(photo)
    if photo_err:
        errors.append(photo_err)
    if errors:
        _remove_local_photo(db, uploaded)
        return render(request, "admin/menu_form.html", {"item": item, "errors": errors, "form": {"name": name, "category": category, "price": price, "description": description, "photo_url": photo_url}}, auth=auth, db=db, status_code=400)
    before = audit.model_snapshot(item, MENU_FIELDS) if item else None
    old_photo = item.photo_url if item else None
    if item is None:
        item = MenuItem(name=name, price=p)
        db.add(item)
    item.name, item.category, item.price, item.description = name, category, p, description
    item.photo_url = uploaded or photo_url
    item.available = available == "on"
    item.eighty_sixed_on = _today_local(db) if eighty_sixed == "on" else None
    db.flush()
    audit.record(db, auth.user, "menu.update" if before else "menu.create", "menu_item", item.id, before, audit.model_snapshot(item, MENU_FIELDS), auth.ip)
    db.commit()
    if old_photo and old_photo != item.photo_url:
        _remove_local_photo(db, old_photo)
    return RedirectResponse("/admin/menu", status_code=303)


@router.post("/menu/{item_id}/toggle/{field}", response_class=HTMLResponse)
def menu_toggle(request: Request, item_id: int, field: str, auth: Auth = Depends(require("menu.edit")), db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if item is None or field not in ("available", "eighty_six"):
        return HTMLResponse("Not found", status_code=404)
    before = audit.model_snapshot(item, MENU_FIELDS)
    today = _today_local(db)
    if field == "available":
        item.available = not item.available
    else:
        item.eighty_sixed_on = None if item.eighty_sixed_on == today else today
    audit.record(db, auth.user, f"menu.toggle_{field}", "menu_item", item.id, before, audit.model_snapshot(item, MENU_FIELDS), auth.ip)
    db.commit()
    return render(request, "admin/_menu_row.html", {"i": item, "today": today}, auth=auth, db=db)


@router.post("/menu/{item_id}/delete")
def menu_delete(request: Request, item_id: int, auth: Auth = Depends(require("menu.delete")), db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if item:
        photo = item.photo_url
        audit.record(db, auth.user, "menu.delete", "menu_item", item.id, audit.model_snapshot(item, MENU_FIELDS), None, auth.ip)
        db.delete(item)
        db.commit()
        _remove_local_photo(db, photo)
    return RedirectResponse("/admin/menu", status_code=303)


# ================================================================== delivery zones
@router.get("/zones", response_class=HTMLResponse)
def zones_list(request: Request, edit: int | None = None, auth: Auth = Depends(require("zones.view")), db: Session = Depends(get_db)):
    zones = db.query(DeliveryZone).order_by(DeliveryZone.name).all()
    return render(request, "admin/zones.html", {"zones": zones, "edit": db.get(DeliveryZone, edit) if edit else None, "errors": []}, auth=auth, db=db)


@router.post("/zones/save", response_class=HTMLResponse)
def zones_save(request: Request, zone_id: str = Form(""), name: str = Form(""), aliases: str = Form(""), fee: str = Form(""), min_order: str = Form("0"), active: str = Form(""), auth: Auth = Depends(require("zones.edit")), db: Session = Depends(get_db)):
    zone = db.get(DeliveryZone, int(zone_id)) if zone_id.isdigit() else None
    f, m = _money(fee), _money(min_order or "0")
    errors = [e for e, bad in (("Name is required.", not name.strip()), ("Fee must be a valid amount.", f is None), ("Minimum order must be a valid amount.", m is None)) if bad]
    if errors:
        return render(request, "admin/zones.html", {"zones": db.query(DeliveryZone).order_by(DeliveryZone.name).all(), "edit": zone, "errors": errors}, auth=auth, db=db, status_code=400)
    before = audit.model_snapshot(zone, ZONE_FIELDS) if zone else None
    if zone is None:
        zone = DeliveryZone(name=name, fee=f)
        db.add(zone)
    zone.name = name.strip()[:120]
    zone.aliases = ", ".join(a.strip()[:80] for a in aliases.split(",") if a.strip())[:2000]
    zone.fee, zone.min_order, zone.active = f, m, active == "on"
    db.flush()
    audit.record(db, auth.user, "zone.update" if before else "zone.create", "delivery_zone", zone.id, before, audit.model_snapshot(zone, ZONE_FIELDS), auth.ip)
    db.commit()
    return RedirectResponse("/admin/zones", status_code=303)


@router.post("/zones/{zone_id}/toggle")
def zones_toggle(request: Request, zone_id: int, auth: Auth = Depends(require("zones.edit")), db: Session = Depends(get_db)):
    zone = db.get(DeliveryZone, zone_id)
    if zone:
        before = audit.model_snapshot(zone, ZONE_FIELDS)
        zone.active = not zone.active
        audit.record(db, auth.user, "zone.toggle_active", "delivery_zone", zone.id, before, audit.model_snapshot(zone, ZONE_FIELDS), auth.ip)
        db.commit()
    return RedirectResponse("/admin/zones", status_code=303)


@router.post("/zones/{zone_id}/delete")
def zones_delete(request: Request, zone_id: int, auth: Auth = Depends(require("zones.delete")), db: Session = Depends(get_db)):
    zone = db.get(DeliveryZone, zone_id)
    if zone:
        audit.record(db, auth.user, "zone.delete", "delivery_zone", zone.id, audit.model_snapshot(zone, ZONE_FIELDS), None, auth.ip)
        db.delete(zone)
        db.commit()
    return RedirectResponse("/admin/zones", status_code=303)


# ================================================================== hours
@router.get("/hours", response_class=HTMLResponse)
def hours_page(request: Request, auth: Auth = Depends(require("hours.view")), db: Session = Depends(get_db)):
    rows = {r.weekday: r for r in db.query(WeeklyHours).all()}
    overrides = db.query(DateOverride).filter(DateOverride.day >= _today_local(db) - timedelta(days=7)).order_by(DateOverride.day).all()
    status = opening_status(db, settings_store.get_setting(db, "timezone"))
    return render(request, "admin/hours.html", {"rows": rows, "days": DAY_NAMES, "overrides": overrides, "status": status, "errors": []}, auth=auth, db=db)


@router.post("/hours/weekly")
async def hours_save(request: Request, auth: Auth = Depends(require("hours.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    before = {r.weekday: {"open": r.open_time, "close": r.close_time, "closed": r.closed} for r in db.query(WeeklyHours).all()}
    after = {}
    for d in range(7):
        closed = form.get(f"closed_{d}") == "on"
        o, c = str(form.get(f"open_{d}") or ""), str(form.get(f"close_{d}") or "")
        if not closed and not (valid_hhmm(o) and valid_hhmm(c)):
            closed, o, c = True, "", ""
        row = db.get(WeeklyHours, d) or WeeklyHours(weekday=d)
        row.open_time, row.close_time, row.closed = o, c, closed
        db.add(row)
        after[d] = {"open": o, "close": c, "closed": closed}
    audit.record(db, auth.user, "hours.update", "weekly_hours", None, before, after, auth.ip)
    db.commit()
    return RedirectResponse("/admin/hours", status_code=303)


@router.post("/hours/override")
def override_add(request: Request, day: str = Form(...), closed: str = Form(""), open_time: str = Form(""), close_time: str = Form(""), note: str = Form(""), auth: Auth = Depends(require("hours.edit")), db: Session = Depends(get_db)):
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return RedirectResponse("/admin/hours", status_code=303)
    is_closed = closed == "on" or not (valid_hhmm(open_time) and valid_hhmm(close_time))
    row = db.query(DateOverride).filter(DateOverride.day == d).one_or_none()
    before = audit.model_snapshot(row, ["day", "closed", "open_time", "close_time", "note"]) if row else None
    row = row or DateOverride(day=d)
    row.closed, row.open_time, row.close_time, row.note = is_closed, "" if is_closed else open_time, "" if is_closed else close_time, note.strip()[:255]
    db.add(row)
    db.flush()
    audit.record(db, auth.user, "hours.override_set", "date_override", d.isoformat(), before, audit.model_snapshot(row, ["day", "closed", "open_time", "close_time", "note"]), auth.ip)
    db.commit()
    return RedirectResponse("/admin/hours", status_code=303)


@router.post("/hours/override/{override_id}/delete")
def override_delete(request: Request, override_id: int, auth: Auth = Depends(require("hours.delete")), db: Session = Depends(get_db)):
    row = db.get(DateOverride, override_id)
    if row:
        audit.record(db, auth.user, "hours.override_delete", "date_override", row.day.isoformat(), audit.model_snapshot(row, ["day", "closed", "open_time", "close_time", "note"]), None, auth.ip)
        db.delete(row)
        db.commit()
    return RedirectResponse("/admin/hours", status_code=303)


# ================================================================== playground
@router.get("/playground", response_class=HTMLResponse)
def playground(request: Request, auth: Auth = Depends(require("playground.use")), db: Session = Depends(get_db)):
    return render(request, "admin/playground.html", {}, auth=auth, db=db)


@router.post("/playground/run", response_class=HTMLResponse)
def playground_run(request: Request, message: str = Form(""), phone: str = Form(""), auth: Auth = Depends(require("playground.use")), db: Session = Depends(get_db)):
    """Runs the full decision pipeline with no Chatwoot calls."""
    message = message.strip()[:2000]
    ctx: dict = {"message": message, "answer": None, "decision": None, "error": ""}
    if not message:
        ctx["error"] = "Type a message."
        return render(request, "admin/_playground_result.html", ctx, auth=auth)
    turn = BotTurn(question=message, dry_run=True)
    db.add(turn)
    db.flush()
    decision = pre_answer_check(message, 0, int(settings_store.get_setting(db, "max_bot_turns")), settings_store.get_setting(db, "handoff_keywords"), settings_store.get_setting(db, "human_request_phrases"))
    if decision is None:
        try:
            answer = generate_answer(db, message, [], {}, contact_phone=phone.strip() or None, bot_turn_id=turn.id, purpose="playground")
            ctx["answer"] = answer
            decision = post_answer_check(answer.confidence, answer.needs_human, answer.answered_from_knowledge, answer.unverified_amounts, float(settings_store.get_setting(db, "confidence_threshold")), answer.reason)
            turn.reply, turn.confidence, turn.chunks, turn.tool_calls = answer.reply, answer.confidence, [c.as_dict() for c in answer.chunks], answer.tool_calls
        except SpendCapReached:
            ctx["error"] = "Daily spend cap reached."
        except LLMError as exc:
            ctx["error"] = f"LLM error: {exc}"
    ctx["decision"] = decision
    turn.handoff = decision is not None
    db.commit()
    return render(request, "admin/_playground_result.html", ctx, auth=auth)


@router.post("/playground/simulate", response_class=HTMLResponse)
def playground_simulate(
    request: Request,
    transcript: str = Form(""),
    phone: str = Form(""),
    contact_name: str = Form(""),
    mode: str = Form("auto"),
    auth: Auth = Depends(require("playground.use")),
    db: Session = Depends(get_db),
):
    """Show the exact customer message and private notes a handoff would produce. Nothing is sent to Chatwoot."""
    try:
        result = simulate(db, transcript, phone=phone.strip()[:32] or None, contact_name=contact_name.strip()[:100], force_handoff=mode == "force")
    except TranscriptError as exc:
        return render(request, "admin/_handoff_result.html", {"r": None, "error": str(exc)}, auth=auth)
    return render(request, "admin/_handoff_result.html", {"r": result, "error": ""}, auth=auth)


# ================================================================== analytics
def _analytics(db: Session, days: int = 30) -> dict:
    tz = settings_store.get_setting(db, "timezone")
    start = utcnow() - timedelta(days=days)

    def local_day(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(safe_tz(tz)).date().isoformat()

    daily: dict[str, dict] = {}
    for (created,) in db.query(MessageLog.created_at).filter(MessageLog.created_at >= start, MessageLog.direction == "incoming").all():
        daily.setdefault(local_day(created), {"messages": 0, "turns": 0, "handoffs": 0, "cost": 0.0})["messages"] += 1
    for created, handoff in db.query(BotTurn.created_at, BotTurn.handoff).filter(BotTurn.created_at >= start, BotTurn.dry_run.is_(False)).all():
        d = daily.setdefault(local_day(created), {"messages": 0, "turns": 0, "handoffs": 0, "cost": 0.0})
        d["turns"] += 1
        d["handoffs"] += int(bool(handoff))
    for created, cost in db.query(LLMUsage.created_at, LLMUsage.cost_usd).filter(LLMUsage.created_at >= start).all():
        daily.setdefault(local_day(created), {"messages": 0, "turns": 0, "handoffs": 0, "cost": 0.0})["cost"] += cost
    rows = [{"day": k, **v, "handoff_rate": (v["handoffs"] / v["turns"]) if v["turns"] else 0.0} for k, v in sorted(daily.items(), reverse=True)]

    unanswered: dict[str, int] = {}
    for (q,) in db.query(BotTurn.question).filter(BotTurn.created_at >= start, BotTurn.unanswered.is_(True), BotTurn.dry_run.is_(False)).all():
        key = re.sub(r"\s+", " ", (q or "").strip().lower())[:160]
        if key:
            unanswered[key] = unanswered.get(key, 0) + 1
    menu_q: dict[str, int] = {}
    for (inputs,) in db.query(ToolCallLog.inputs).filter(ToolCallLog.created_at >= start, ToolCallLog.tool == "menu_lookup").all():
        key = str((inputs or {}).get("query") or (inputs or {}).get("category") or "").strip().lower()[:80]
        if key:
            menu_q[key] = menu_q.get(key, 0) + 1
    total_turns = sum(r["turns"] for r in rows)
    return {
        "daily": rows,
        "unanswered": sorted(unanswered.items(), key=lambda x: -x[1])[:25],
        "menu": sorted(menu_q.items(), key=lambda x: -x[1])[:25],
        "totals": {"messages": sum(r["messages"] for r in rows), "turns": total_turns, "handoffs": sum(r["handoffs"] for r in rows), "cost": sum(r["cost"] for r in rows), "handoff_rate": (sum(r["handoffs"] for r in rows) / total_turns) if total_turns else 0.0},
        "days": days,
    }


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request, days: int = 30, auth: Auth = Depends(require("analytics.view")), db: Session = Depends(get_db)):
    return render(request, "admin/analytics.html", {"a": _analytics(db, max(1, min(days, 365)))}, auth=auth, db=db)


@router.get("/analytics/export.csv")
def analytics_export(report: str = "daily", days: int = 30, auth: Auth = Depends(require("analytics.view")), db: Session = Depends(get_db)):
    a = _analytics(db, max(1, min(days, 365)))
    buf = io.StringIO()
    w = csv.writer(buf)
    if report == "unanswered":
        w.writerow(["question", "count"])
        for q, n in a["unanswered"]:
            w.writerow([csv_safe(q), n])
    elif report == "menu":
        w.writerow(["menu_query", "count"])
        for q, n in a["menu"]:
            w.writerow([csv_safe(q), n])
    else:
        report = "daily"
        w.writerow(["day", "incoming_messages", "bot_turns", "handoffs", "handoff_rate", "llm_cost_usd"])
        for r in a["daily"]:
            w.writerow([r["day"], r["messages"], r["turns"], r["handoffs"], f"{r['handoff_rate']:.3f}", f"{r['cost']:.4f}"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="analytics-{report}.csv"'})


# ================================================================== settings
def _provider_info() -> list[dict]:
    names = [p for p in PROVIDERS if p != "fake" or not get_settings().is_production]
    labels = {"openai": "OpenAI", "gemini": "Google Gemini", "fake": "Fake (offline testing)"}
    keys = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "fake": ""}
    return [{"id": n, "label": labels[n], "default_model": DEFAULT_MODELS[n], "key_env": keys[n], "key_ok": key_configured(n)} for n in names]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, auth: Auth = Depends(require("settings.view")), db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", {"s": settings_store.all_settings(db), "errors": [], "flash": "", "providers": _provider_info()}, auth=auth, db=db)


def _parse_keywords(text: str) -> dict[str, list[str]] | None:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            return None
        cat, words = line.split(":", 1)
        cat = re.sub(r"[^a-z_]", "", cat.strip().lower().replace(" ", "_"))[:32]
        phrases = [w.strip().lower()[:60] for w in words.split(",") if w.strip()]
        if cat and phrases:
            out[cat] = phrases[:50]
    return out


@router.post("/settings", response_class=HTMLResponse)
def settings_save(
    request: Request,
    llm_provider: str = Form("openai"),
    llm_model: str = Form(""),
    daily_spend_cap_usd: str = Form(...),
    timezone_name: str = Form(...),
    greeting: str = Form(""),
    handoff_message: str = Form(""),
    handoff_keywords: str = Form(""),
    human_request_phrases: str = Form(""),
    max_bot_turns: str = Form("6"),
    confidence_threshold: str = Form("0.6"),
    currency_symbol: str = Form("₦"),
    qr_hours: str = Form(""),
    qr_delivery: str = Form(""),
    qr_payment: str = Form(""),
    llm_price_overrides: str = Form("{}"),
    auth: Auth = Depends(require("settings.edit")),
    db: Session = Depends(get_db),
):
    errors = []
    new: dict = {}
    allowed_providers = [p for p in PROVIDERS if p != "fake" or not get_settings().is_production]
    provider = llm_provider.strip().lower()
    model = llm_model.strip() or DEFAULT_MODELS.get(provider, "")
    if provider not in allowed_providers:
        errors.append("Choose OpenAI or Gemini as the AI provider.")
    elif not re.fullmatch(r"[A-Za-z0-9._:-]{2,64}", model):
        errors.append("Model name is invalid.")
    elif not model_matches(provider, model):
        errors.append(f"Model {model!r} doesn't belong to {provider}. Leave it empty to use {DEFAULT_MODELS.get(provider)}.")
    new["llm_provider"] = provider
    new["llm_model"] = model
    try:
        cap = float(daily_spend_cap_usd)
        assert 0 <= cap <= 10_000
        new["daily_spend_cap_usd"] = cap
    except (ValueError, AssertionError):
        errors.append("Spend cap must be between 0 and 10000.")
    if not is_valid_tz(timezone_name.strip()):
        errors.append("Unknown timezone.")
    new["timezone"] = timezone_name.strip()
    new["greeting"] = greeting.strip()[:300]
    new["handoff_message"] = handoff_message.strip()[:500] or settings_store.defaults()["handoff_message"]
    kw = _parse_keywords(handoff_keywords)
    if kw is None:
        errors.append("Handoff keywords: use one 'category: word, phrase' per line.")
    new["handoff_keywords"] = kw or {}
    new["human_request_phrases"] = [p.strip().lower()[:60] for p in human_request_phrases.split(",") if p.strip()][:50]
    try:
        new["max_bot_turns"] = max(1, min(50, int(max_bot_turns)))
        new["confidence_threshold"] = max(0.0, min(1.0, float(confidence_threshold)))
    except ValueError:
        errors.append("Turn limit and confidence threshold must be numbers.")
    new["currency_symbol"] = currency_symbol.strip()[:5] or "₦"
    new["quick_replies"] = {"hours": qr_hours.strip()[:1000], "delivery": qr_delivery.strip()[:1000], "payment": qr_payment.strip()[:1000]}
    try:
        po = json.loads(llm_price_overrides or "{}")
        assert isinstance(po, dict) and all(isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and x >= 0 for x in v) for v in po.values())
        new["llm_price_overrides"] = po
    except (ValueError, AssertionError):
        errors.append('Price overrides must be JSON like {"gpt-4o-mini": [0.15, 0.6]}.')
    if errors:
        return render(request, "admin/settings.html", {"s": {**settings_store.all_settings(db), **new}, "errors": errors, "flash": "", "providers": _provider_info()}, auth=auth, db=db, status_code=400)
    for key, value in new.items():
        before, after = settings_store.set_setting(db, key, value)
        if before != after:
            audit.record(db, auth.user, "settings.update", "setting", key, before, after, auth.ip)
    db.commit()
    return render(request, "admin/settings.html", {"s": settings_store.all_settings(db), "errors": [], "flash": "Settings saved.", "providers": _provider_info()}, auth=auth, db=db)


@router.post("/settings/test-connection", response_class=HTMLResponse)
def settings_test(request: Request, auth: Auth = Depends(require("settings.edit")), db: Session = Depends(get_db)):
    result = connection_test()
    audit.record(db, auth.user, "settings.connection_test", "chatwoot", None, None, {"reachable": result.get("reachable"), "bot_token_ok": result.get("bot_token_ok")}, auth.ip)
    db.commit()
    return render(request, "admin/_connection_result.html", {"r": result}, auth=auth)


# ================================================================== users
def _roles(db: Session) -> list[Role]:
    return db.query(Role).order_by(Role.is_system.desc(), Role.name).all()


def _grantable_roles(db: Session, auth: Auth) -> list[Role]:
    return [r for r in _roles(db) if can_grant(auth.perms, rbac.effective(r))]


def _can_manage(db: Session, auth: Auth, user: User) -> bool:
    """You may only manage users whose access does not exceed yours (prevents takeover of admins)."""
    return user.id != auth.user.id and can_grant(auth.perms, rbac.role_permissions(db, user.role))


def _users_page(request, db, auth, link=None, error="", status=200):
    users = db.query(User).order_by(User.email).all()
    manageable = {u.id for u in users if _can_manage(db, auth, u)}
    return render(
        request,
        "admin/users.html",
        {"users": users, "roles": _roles(db), "grantable": [r.name for r in _grantable_roles(db, auth)], "manageable": manageable, "link": link, "error": error},
        auth=auth,
        db=db,
        status_code=status,
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, auth: Auth = Depends(require("users.view")), db: Session = Depends(get_db)):
    return _users_page(request, db, auth)


def _issue_link(db: Session, request: Request, user: User, purpose: str) -> str:
    db.query(UserToken).filter(UserToken.user_id == user.id, UserToken.used_at.is_(None)).delete(synchronize_session=False)
    raw = random_token(32)
    db.add(UserToken(user_id=user.id, token_hash=sha256_hex(raw), purpose=purpose, expires_at=utcnow() + timedelta(hours=72 if purpose == "invite" else 24)))
    return str(request.base_url).rstrip("/") + f"/admin/set-password/{raw}"


def _managed_user(request, db, auth, user_id: int):
    """Return (user, None) or (None, error response)."""
    user = db.get(User, user_id)
    if user is None:
        return None, _users_page(request, db, auth, error="No such user.", status=404)
    if user.id == auth.user.id:
        return None, _users_page(request, db, auth, error="You can't change your own account here. Use My profile.", status=400)
    if not _can_manage(db, auth, user):
        return None, _users_page(request, db, auth, error="That user has access you don't have, so you can't manage them.", status=403)
    return user, None


@router.post("/users/invite", response_class=HTMLResponse)
def users_invite(request: Request, email: str = Form(...), role: str = Form("staff"), auth: Auth = Depends(require("users.edit")), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}", email):
        return _users_page(request, db, auth, error="Enter a valid email.", status=400)
    if role not in [r.name for r in _grantable_roles(db, auth)]:
        return _users_page(request, db, auth, error="You can't assign that role.", status=403)
    if db.query(User).filter(User.email == email).first():
        return _users_page(request, db, auth, error="A user with that email already exists.", status=400)
    user = User(email=email, role=role, is_active=True, must_change_password=False)
    db.add(user)
    db.flush()
    link = _issue_link(db, request, user, "invite")
    audit.record(db, auth.user, "user.invite", "user", user.id, None, user.public_dict(), auth.ip)
    db.commit()
    return _users_page(request, db, auth, link={"email": email, "url": link, "purpose": "invite"})


@router.post("/users/{user_id}/reset", response_class=HTMLResponse)
def users_reset(request: Request, user_id: int, auth: Auth = Depends(require("users.edit")), db: Session = Depends(get_db)):
    user, err = _managed_user(request, db, auth, user_id)
    if err:
        return err
    link = _issue_link(db, request, user, "reset")
    revoke_user_sessions(db, user.id)
    audit.record(db, auth.user, "user.password_reset_issued", "user", user.id, None, {"sessions_revoked": True}, auth.ip)
    db.commit()
    return _users_page(request, db, auth, link={"email": user.email, "url": link, "purpose": "reset"})


@router.post("/users/{user_id}/role", response_class=HTMLResponse)
def users_role(request: Request, user_id: int, role: str = Form(...), auth: Auth = Depends(require("users.edit")), db: Session = Depends(get_db)):
    user, err = _managed_user(request, db, auth, user_id)
    if err:
        return err
    if role not in [r.name for r in _grantable_roles(db, auth)]:
        return _users_page(request, db, auth, error="You can't assign that role.", status=403)
    if user.role == ADMIN_ROLE and role != ADMIN_ROLE and user.is_active and rbac.active_admin_count(db) <= 1:
        return _users_page(request, db, auth, error="Cannot demote the last active admin.", status=400)
    before = user.public_dict()
    user.role = role
    audit.record(db, auth.user, "user.role_change", "user", user.id, before, user.public_dict(), auth.ip)
    db.commit()
    return _users_page(request, db, auth)


@router.post("/users/{user_id}/active", response_class=HTMLResponse)
def users_active(request: Request, user_id: int, auth: Auth = Depends(require("users.edit")), db: Session = Depends(get_db)):
    user, err = _managed_user(request, db, auth, user_id)
    if err:
        return err
    if user.is_active and user.role == ADMIN_ROLE and rbac.active_admin_count(db) <= 1:
        return _users_page(request, db, auth, error="Cannot disable the last active admin.", status=400)
    before = user.public_dict()
    user.is_active = not user.is_active
    if not user.is_active:
        revoke_user_sessions(db, user.id)
    audit.record(db, auth.user, "user.enable" if user.is_active else "user.disable", "user", user.id, before, user.public_dict(), auth.ip)
    db.commit()
    return _users_page(request, db, auth)


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
def users_delete(request: Request, user_id: int, auth: Auth = Depends(require("users.delete")), db: Session = Depends(get_db)):
    user, err = _managed_user(request, db, auth, user_id)
    if err:
        return err
    if user.role == ADMIN_ROLE and user.is_active and rbac.active_admin_count(db) <= 1:
        return _users_page(request, db, auth, error="Cannot delete the last active admin.", status=400)
    before = user.public_dict()
    # SQLite doesn't enforce ON DELETE CASCADE by default, so remove dependants explicitly.
    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
    db.query(UserToken).filter(UserToken.user_id == user.id).delete(synchronize_session=False)
    db.query(PanelLink).filter(PanelLink.user_id == user.id).delete(synchronize_session=False)
    db.delete(user)
    audit.record(db, auth.user, "user.delete", "user", user_id, before, None, auth.ip)
    db.commit()
    return _users_page(request, db, auth)


# ================================================================== roles
def _roles_page(request, db, auth, edit: Role | None = None, form: dict | None = None, error: str = "", flash: str = "", status: int = 200):
    return render(
        request,
        "admin/roles.html",
        {"roles": rbac.roles_with_counts(db), "areas": AREAS, "action_labels": ACTION_LABELS, "edit": edit, "form": form, "error": error, "flash": flash, "effective": rbac.effective, "admin_role": ADMIN_ROLE},
        auth=auth,
        db=db,
        status_code=status,
    )


@router.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request, edit: int | None = None, auth: Auth = Depends(require("roles.view")), db: Session = Depends(get_db)):
    role = db.get(Role, edit) if edit else None
    if role is not None and role.is_system:
        role = None
    return _roles_page(request, db, auth, edit=role)


@router.post("/roles/save", response_class=HTMLResponse)
async def roles_save(request: Request, auth: Auth = Depends(require("roles.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    role_id = str(form.get("role_id") or "")
    name = str(form.get("name") or "").strip().lower()
    description = str(form.get("description") or "").strip()[:255]
    perms = clean(form.getlist("perms"))
    role = db.get(Role, int(role_id)) if role_id.isdigit() else None
    state = {"name": name, "description": description, "perms": perms}
    if role is not None and role.is_system:
        return _roles_page(request, db, auth, error="The built-in admin role can't be changed.", status=400)
    if not rbac.ROLE_NAME_RE.match(name):
        return _roles_page(request, db, auth, edit=role, form=state, error="Role name: 2-32 lowercase letters, digits, '-' or '_', starting with a letter.", status=400)
    clash = db.query(Role).filter(Role.name == name).one_or_none()
    if clash is not None and (role is None or clash.id != role.id):
        return _roles_page(request, db, auth, edit=role, form=state, error="A role with that name already exists.", status=400)
    if not can_grant(auth.perms, perms):
        return _roles_page(request, db, auth, edit=role, form=state, error="You can only give a role permissions you have yourself.", status=403)
    if role is not None and not can_grant(auth.perms, rbac.effective(role)):
        return _roles_page(request, db, auth, error="That role has access you don't have, so you can't edit it.", status=403)
    before = {"name": role.name, "description": role.description, "permissions": rbac.effective(role)} if role else None
    if role is None:
        role = Role(name=name, description=description, permissions=perms)
        db.add(role)
    else:
        if role.name != name:
            db.query(User).filter(User.role == role.name).update({User.role: name}, synchronize_session=False)
        role.name, role.description, role.permissions = name, description, perms
    db.flush()
    audit.record(db, auth.user, "role.update" if before else "role.create", "role", role.id, before, {"name": name, "description": description, "permissions": perms}, auth.ip)
    db.commit()
    return _roles_page(request, db, auth, flash=f"Role “{name}” saved. Changes apply on users' next click.")


@router.post("/roles/{role_id}/delete", response_class=HTMLResponse)
def roles_delete(request: Request, role_id: int, auth: Auth = Depends(require("roles.delete")), db: Session = Depends(get_db)):
    role = db.get(Role, role_id)
    if role is None:
        return _roles_page(request, db, auth, error="No such role.", status=404)
    if role.is_system:
        return _roles_page(request, db, auth, error="The built-in admin role can't be deleted.", status=400)
    if not can_grant(auth.perms, rbac.effective(role)):
        return _roles_page(request, db, auth, error="That role has access you don't have, so you can't delete it.", status=403)
    in_use = db.query(User).filter(User.role == role.name).count()
    if in_use:
        return _roles_page(request, db, auth, error=f"{in_use} user(s) still have the “{role.name}” role. Give them another role first.", status=400)
    audit.record(db, auth.user, "role.delete", "role", role.id, {"name": role.name, "permissions": rbac.effective(role)}, None, auth.ip)
    db.delete(role)
    db.commit()
    return _roles_page(request, db, auth, flash="Role deleted.")


# ================================================================== audit
@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, user: str = "", entity: str = "", action: str = "", since: str = "", until: str = "", page: int = 1, auth: Auth = Depends(require("audit.view")), db: Session = Depends(get_db)):
    q = db.query(AuditLog)
    if user:
        q = q.filter(AuditLog.user_email.ilike(f"%{user[:100]}%"))
    if entity:
        q = q.filter(AuditLog.entity == entity[:64])
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action[:64]}%"))
    for value, op in ((since, "ge"), (until, "lt")):
        try:
            d = datetime.fromisoformat(value) if value else None
        except ValueError:
            d = None
        if d is not None:
            q = q.filter(AuditLog.created_at >= d) if op == "ge" else q.filter(AuditLog.created_at < d + timedelta(days=1))
    page = max(1, page)
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset((page - 1) * 50).limit(50).all()
    entities = [e for (e,) in db.query(AuditLog.entity).distinct().order_by(AuditLog.entity).all()]
    return render(request, "admin/audit.html", {"rows": rows, "total": total, "page": page, "pages": max(1, (total + 49) // 50), "f": {"user": user, "entity": entity, "action": action, "since": since, "until": until}, "entities": entities, "dumps": lambda v: json.dumps(v, ensure_ascii=False, default=str)}, auth=auth, db=db)
