"""Security headers, admin IP allow-list, and request logging (no bodies, no query strings)."""
from __future__ import annotations

import ipaddress
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from ..config import Settings

log = logging.getLogger("app.request")

CDN_SCRIPTS = "https://unpkg.com https://cdn.tailwindcss.com"


def _csp(frame_ancestors: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {CDN_SCRIPTS}",
            # Tailwind Play CDN and htmx inject <style> tags.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' https: data:",
            "connect-src 'self'",
            "font-src 'self' data:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        ]
    )


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers (with FORWARDED_ALLOW_IPS) rewrites request.client from X-Forwarded-For.
    return request.client.host if request.client else ""


def ip_allowed(ip: str, networks: list) -> bool:
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def install(app: FastAPI, settings: Settings) -> None:
    panel_csp = _csp(f"'self' {settings.chatwoot_browser_origin}")
    default_csp = _csp("'none'")
    networks = settings.allowed_ip_networks

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        started = time.perf_counter()
        path = request.url.path

        if path.startswith("/admin") and not ip_allowed(client_ip(request), networks):
            log.warning("admin.ip_blocked")
            return PlainTextResponse("Forbidden", status_code=403)

        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if path.startswith("/app-panel") or path.startswith("/static"):
            # The Dashboard App must be frameable by Chatwoot. No X-Frame-Options here.
            response.headers["Content-Security-Policy"] = panel_csp
            if "x-frame-options" in response.headers:
                del response.headers["x-frame-options"]
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = default_csp
        if path.startswith("/admin") or path.startswith("/app-panel") or path.startswith("/copilot"):
            response.headers["Cache-Control"] = "no-store"
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        route = request.scope.get("route")
        log.info(
            "http.request",
            extra={
                "method": request.method,
                "route": getattr(route, "path", "unmatched"),
                "status": response.status_code,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return response
