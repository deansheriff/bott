"""Permission catalogue for role-based access control.

A permission is "<area>.<action>". Roles hold a list of permissions; the built-in
"admin" role always has every permission (including ones added in future versions)
and cannot be edited or deleted. Any logged-in user can open their own profile.
"""
from __future__ import annotations

ACTION_LABELS = {
    "view": "View",
    "edit": "Create & edit",
    "delete": "Delete",
    "use": "Use",
    "act": "Act on conversations",
}

# (area, label, actions, help text)
AREAS: list[tuple[str, str, tuple[str, ...], str]] = [
    ("overview", "Overview", ("view",), "Dashboard numbers and webhook health"),
    ("bot_switch", "Bot on/off switch", ("edit",), "Turn the bot ON, PAUSED or OFF for everyone"),
    ("knowledge", "Knowledge", ("view", "edit", "delete"), "Knowledge files, imports, versions and rollback"),
    ("menu", "Menu", ("view", "edit", "delete"), "Menu items, prices, availability, 86'd"),
    ("zones", "Delivery zones", ("view", "edit", "delete"), "Delivery areas, fees and minimum orders"),
    ("hours", "Hours & closures", ("view", "edit", "delete"), "Weekly hours and date overrides"),
    ("contacts", "Customer data", ("view", "edit", "delete"), "Stored customer memory and transcripts"),
    ("playground", "Playground", ("use",), "Test the bot and simulate handoffs (uses AI credit)"),
    ("analytics", "Analytics", ("view",), "Reports and CSV exports (include customer questions)"),
    ("settings", "Settings", ("view", "edit"), "AI provider, spend cap, keywords, quick replies"),
    ("users", "Users", ("view", "edit", "delete"), "Invite, disable, reset passwords, assign roles"),
    ("roles", "Roles", ("view", "edit", "delete"), "Create roles and choose their permissions"),
    ("audit", "Audit log", ("view",), "Read-only history of every change"),
    ("panel", "Chatwoot panel", ("view", "act"), "View bot details in Chatwoot; take over, send, draft, edit memory"),
]

ALL_PERMISSIONS: tuple[str, ...] = tuple(f"{area}.{action}" for area, _, actions, _ in AREAS for action in actions)
_ALL = frozenset(ALL_PERMISSIONS)

ADMIN_ROLE = "admin"

DEFAULT_ROLES: dict[str, tuple[str, list[str]]] = {
    "staff": (
        "Manages knowledge, menu and hours; sees analytics; uses the Chatwoot panel.",
        [
            "overview.view",
            "knowledge.view", "knowledge.edit", "knowledge.delete",
            "menu.view", "menu.edit", "menu.delete",
            "hours.view", "hours.edit", "hours.delete",
            "analytics.view",
            "panel.view", "panel.act",
        ],
    ),
    "agent": (
        "Chatwoot agents: uses the Restaurant Bot tab only.",
        ["overview.view", "panel.view", "panel.act"],
    ),
}


def clean(perms) -> list[str]:
    """Keep only known permissions, in catalogue order. Edit/delete imply view."""
    wanted = {p for p in (perms or []) if p in _ALL}
    for p in list(wanted):
        area, action = p.split(".", 1)
        if action in ("edit", "delete") and f"{area}.view" in _ALL:
            wanted.add(f"{area}.view")
        if p == "panel.act":
            wanted.add("panel.view")
    return [p for p in ALL_PERMISSIONS if p in wanted]


def all_permissions() -> frozenset[str]:
    return _ALL
