"""App factory. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings, set_settings
from .db import create_all, init_engine, session_scope
from .deps import CSRFFailed, Forbidden, LoginRequired, PanelUnauthorized, PasswordChangeRequired
from .logging_setup import setup_logging
from .models import DeliveryZone, MenuItem, User, WeeklyHours
from .routers import admin_auth, admin_pages, copilot, health, panel, webhooks
from .security import middleware
from .security.passwords import hash_password
from .security.sessions import purge_expired
from .services import audit
from .services.knowledge import init_kb, seed_versions

log = logging.getLogger("app")
BASE = Path(__file__).parent


def bootstrap_admin(settings: Settings) -> None:
    with session_scope() as db:
        if db.query(User).count() > 0:
            return
        if not (settings.dashboard_admin_email and settings.dashboard_admin_password):
            log.warning("bootstrap.no_admin", extra={"note": "set DASHBOARD_ADMIN_EMAIL and DASHBOARD_ADMIN_PASSWORD for first boot"})
            return
        user = User(email=settings.dashboard_admin_email.strip().lower(), password_hash=hash_password(settings.dashboard_admin_password), role="admin", is_active=True, must_change_password=True)
        db.add(user)
        db.flush()
        audit.record(db, None, "user.bootstrap_admin", "user", user.id, None, user.public_dict())
        log.info("bootstrap.admin_created", extra={"user_id": user.id})


def seed_demo(settings: Settings) -> None:
    """Demo data for local development ONLY (SEED_DEMO_DATA=true). Never enable in production."""
    with session_scope() as db:
        if db.query(MenuItem).count() == 0:
            for name, cat, price, desc in [
                ("Jollof Rice & Chicken", "Mains", 4500, "Smoky party jollof with grilled chicken."),
                ("Fried Rice & Turkey", "Mains", 5200, "Vegetable fried rice with peppered turkey."),
                ("Pounded Yam & Egusi", "Swallow", 5000, "Egusi soup with assorted meat."),
                ("Small Chops Platter", "Sides", 3500, "Puff-puff, samosa, spring rolls."),
                ("Zobo (50cl)", "Drinks", 800, "Chilled hibiscus drink."),
            ]:
                db.add(MenuItem(name=name, category=cat, price=Decimal(price), description=desc, available=True))
        if db.query(DeliveryZone).count() == 0:
            db.add(DeliveryZone(name="Lekki Phase 1", aliases="lekki 1, lekki phase one", fee=Decimal(1500), min_order=Decimal(5000)))
            db.add(DeliveryZone(name="Victoria Island", aliases="vi, v.i", fee=Decimal(2000), min_order=Decimal(5000)))
            db.add(DeliveryZone(name="Ikoyi", aliases="", fee=Decimal(2000), min_order=Decimal(5000)))
        if db.query(WeeklyHours).count() == 0:
            for d in range(7):
                db.add(WeeklyHours(weekday=d, open_time="10:00", close_time="22:00" if d < 5 else "23:00", closed=False))
        log.warning("bootstrap.demo_data_seeded")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    set_settings(settings)
    setup_logging(settings.log_level)
    if settings.is_production:
        problems = settings.production_problems()
        if problems:
            for p in problems:
                log.error("config.invalid", extra={"problem": p})
            raise RuntimeError("Invalid production configuration: " + "; ".join(problems))

    init_engine(settings.sqlalchemy_url)
    create_all()
    kb = init_kb(settings.knowledge_dir)
    with session_scope() as db:
        seed_versions(db, kb)
        purge_expired(db)
    bootstrap_admin(settings)
    if settings.seed_demo_data:
        seed_demo(settings)

    app = FastAPI(title="Restaurant Bot", docs_url=None, redoc_url=None, openapi_url=None)
    middleware.install(app, settings)

    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    media = Path(settings.media_dir)
    (media / "photos").mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media)), name="media")

    for r in (health.router, webhooks.router, copilot.router, panel.router, admin_auth.router, admin_pages.router):
        app.include_router(r)

    def _is_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = f"/admin/login?next={exc.next_url}" if exc.next_url.startswith("/admin") else "/admin/login"
        if _is_htmx(request):
            return HTMLResponse("", status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _pw_required(request: Request, exc: PasswordChangeRequired):
        if _is_htmx(request):
            return HTMLResponse("", status_code=403, headers={"HX-Redirect": "/admin/password"})
        return RedirectResponse("/admin/password", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("<h1>403 Forbidden</h1><p>Your role does not allow this page.</p><p><a href='/admin/'>Back</a></p>", status_code=403)

    @app.exception_handler(CSRFFailed)
    async def _csrf(request: Request, exc: CSRFFailed):
        log.warning("csrf.failed", extra={"path": request.url.path})
        if request.url.path.startswith(("/app-panel", "/copilot")):
            return JSONResponse({"error": "csrf"}, status_code=403)
        return HTMLResponse("<h1>403</h1><p>Your session form token is invalid or expired. Reload the page and try again.</p>", status_code=403)

    @app.exception_handler(PanelUnauthorized)
    async def _panel_unauth(request: Request, exc: PanelUnauthorized):
        return JSONResponse({"error": exc.reason}, status_code=401)

    return app
