"""Role-based access control: roles, permission lookup and anti-escalation rules."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import Role, User
from ..security.permissions import ADMIN_ROLE, DEFAULT_ROLES, all_permissions, clean

ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def ensure_default_roles(db: Session) -> None:
    """Create the built-in admin role and the starter roles if they don't exist yet."""
    if db.query(Role).filter(Role.name == ADMIN_ROLE).one_or_none() is None:
        db.add(Role(name=ADMIN_ROLE, description="Everything, including users, roles and settings. Built-in; cannot be changed.", permissions=[], is_system=True))
    for name, (description, perms) in DEFAULT_ROLES.items():
        if db.query(Role).filter(Role.name == name).one_or_none() is None:
            db.add(Role(name=name, description=description, permissions=clean(perms)))
    db.commit()


def role_permissions(db: Session, role_name: str | None) -> frozenset[str]:
    if role_name == ADMIN_ROLE:
        return all_permissions()
    role = db.query(Role).filter(Role.name == role_name).one_or_none() if role_name else None
    return frozenset(clean(role.permissions)) if role else frozenset()


def user_permissions(db: Session, user: User) -> frozenset[str]:
    return role_permissions(db, user.role) if user.is_active else frozenset()


def can_grant(actor_perms: frozenset[str], perms) -> bool:
    """You can only hand out permissions you hold yourself (no privilege escalation)."""
    return set(clean(perms)) <= set(actor_perms)


def active_admin_count(db: Session) -> int:
    return db.query(User).filter(User.role == ADMIN_ROLE, User.is_active.is_(True)).count()


def roles_with_counts(db: Session) -> list[tuple[Role, int]]:
    roles = db.query(Role).order_by(Role.is_system.desc(), Role.name).all()
    return [(r, db.query(User).filter(User.role == r.name).count()) for r in roles]


def effective(role: Role) -> list[str]:
    return list(all_permissions()) if role.name == ADMIN_ROLE else clean(role.permissions)
