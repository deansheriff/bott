"""Role-based access control: custom roles, live permission checks and anti-escalation rules."""
from app.models import AuditLog, Role, User
from app.security.crypto import encrypt_secret
from app.security.permissions import ALL_PERMISSIONS, clean
from app.services.rbac import role_permissions

from .conftest import csrf_from, login, make_user


def add_role(db, name, perms):
    db.add(Role(name=name, description="", permissions=clean(perms)))
    db.commit()


def token(client, path="/admin/profile"):
    return csrf_from(client.get(path).text)


def test_default_roles_seeded(db):
    names = {r.name for r in db.query(Role).all()}
    assert {"admin", "staff", "agent"} <= names
    assert role_permissions(db, "admin") == frozenset(ALL_PERMISSIONS)
    assert "settings.edit" not in role_permissions(db, "staff")
    assert role_permissions(db, "agent") == frozenset({"overview.view", "panel.view", "panel.act"})


def test_clean_implies_view():
    assert clean(["menu.delete", "panel.act", "bogus.perm"]) == ["menu.view", "menu.delete", "panel.view", "panel.act"]


def test_view_only_role_cannot_write(client, db):
    add_role(db, "menu-viewer", ["menu.view"])
    make_user(db, "v@example.com", role="menu-viewer")
    login(client, "v@example.com")
    page = client.get("/admin/menu")
    assert page.status_code == 200 and "Add item" not in page.text
    t = token(client)
    assert client.post("/admin/menu/save", data={"csrf_token": t, "name": "X", "price": "1"}).status_code == 403
    assert client.get("/admin/knowledge").status_code == 403
    nav = client.get("/admin/menu").text
    assert 'href="/admin/settings"' not in nav and 'href="/admin/menu"' in nav


def test_permission_change_applies_immediately(client, db):
    make_user(db, "s@example.com", role="staff")
    login(client, "s@example.com")
    assert client.get("/admin/menu").status_code == 200
    staff = db.query(Role).filter_by(name="staff").one()
    staff.permissions = clean(["overview.view"])
    db.commit()
    assert client.get("/admin/menu").status_code == 403  # no re-login needed


def test_admin_creates_and_renames_role(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    t = token(client, "/admin/roles")
    r = client.post("/admin/roles/save", data={"csrf_token": t, "name": "kitchen", "description": "Kitchen", "perms": ["menu.edit", "hours.view"]})
    assert r.status_code == 200 and "saved" in r.text
    role = db.query(Role).filter_by(name="kitchen").one()
    assert role.permissions == ["menu.view", "menu.edit", "hours.view"]
    make_user(db, "k@example.com", role="kitchen")
    r = client.post("/admin/roles/save", data={"csrf_token": t, "role_id": str(role.id), "name": "kitchen-team", "perms": ["menu.edit"]})
    db.expire_all()
    assert db.query(User).filter_by(email="k@example.com").one().role == "kitchen-team"
    assert db.query(AuditLog).filter(AuditLog.action.in_(["role.create", "role.update"])).count() == 2


def test_admin_role_is_locked_and_role_delete_rules(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    t = token(client, "/admin/roles")
    admin_role = db.query(Role).filter_by(name="admin").one()
    r = client.post("/admin/roles/save", data={"csrf_token": t, "role_id": str(admin_role.id), "name": "admin", "perms": ["menu.view"]})
    assert r.status_code == 400
    assert client.post(f"/admin/roles/{admin_role.id}/delete", data={"csrf_token": t}).status_code == 400
    add_role(db, "temp", ["menu.view"])
    make_user(db, "t@example.com", role="temp")
    temp = db.query(Role).filter_by(name="temp").one()
    r = client.post(f"/admin/roles/{temp.id}/delete", data={"csrf_token": t})
    assert r.status_code == 400 and "still have" in r.text
    db.query(User).filter_by(email="t@example.com").update({"role": "staff"})
    db.commit()
    assert client.post(f"/admin/roles/{temp.id}/delete", data={"csrf_token": t}).status_code == 200
    assert db.query(Role).filter_by(name="temp").count() == 0


def test_no_privilege_escalation(client, db):
    add_role(db, "manager", ["users.view", "users.edit", "roles.view", "roles.edit", "menu.view"])
    add_role(db, "menu-viewer", ["menu.view"])
    me = make_user(db, "m@example.com", role="manager")
    boss = make_user(db, "boss@example.com", role="admin")
    login(client, "m@example.com")
    t = token(client, "/admin/users")
    # Can't create a role with permissions the manager lacks.
    r = client.post("/admin/roles/save", data={"csrf_token": t, "name": "sneaky", "perms": ["settings.edit"]})
    assert r.status_code == 403 and db.query(Role).filter_by(name="sneaky").count() == 0
    # Can't assign roles with more access, or invite with them.
    assert client.post("/admin/users/invite", data={"csrf_token": t, "email": "x@example.com", "role": "admin"}).status_code == 403
    assert client.post("/admin/users/invite", data={"csrf_token": t, "email": "x@example.com", "role": "staff"}).status_code == 403
    assert client.post("/admin/users/invite", data={"csrf_token": t, "email": "y@example.com", "role": "menu-viewer"}).status_code == 200
    # Can't touch an admin (password reset would hand over the account) or themselves.
    assert client.post(f"/admin/users/{boss.id}/reset", data={"csrf_token": t}).status_code == 403
    assert client.post(f"/admin/users/{boss.id}/role", data={"csrf_token": t, "role": "menu-viewer"}).status_code == 403
    assert client.post(f"/admin/users/{me.id}/role", data={"csrf_token": t, "role": "admin"}).status_code == 400
    # Can't edit a role that has more access than they do.
    staff = db.query(Role).filter_by(name="staff").one()
    assert client.post("/admin/roles/save", data={"csrf_token": t, "role_id": str(staff.id), "name": "staff", "perms": ["menu.view"]}).status_code == 403
    db.expire_all()
    assert db.get(User, boss.id).role == "admin" and db.get(User, me.id).role == "manager"


def test_last_admin_and_self_protection(client, db):
    me = make_user(db, "a@example.com")
    login(client, "a@example.com")
    t = token(client, "/admin/users")
    assert client.post(f"/admin/users/{me.id}/delete", data={"csrf_token": t}).status_code == 400
    assert client.post(f"/admin/users/{me.id}/active", data={"csrf_token": t}).status_code == 400
    owner = db.query(User).filter_by(email="owner@example.com").one()
    owner_id = owner.id
    assert client.post(f"/admin/users/{owner_id}/delete", data={"csrf_token": t}).status_code == 200  # 2 admins -> ok
    db.expunge_all()
    assert db.query(User).filter_by(id=owner_id).first() is None


def test_panel_permissions(client, db, fake_cw):
    add_role(db, "watcher", ["panel.view"])
    user = make_user(db, "w@example.com", role="watcher")
    user.chatwoot_token_enc = encrypt_secret("agent-token")
    db.commit()
    start = client.post("/app-panel/api/link/start").json()
    login(client, "w@example.com")
    page = client.get(f"/admin/panel-link?code={start['code']}")
    client.post("/admin/panel-link", data={"code": start["code"], "csrf_token": csrf_from(page.text)})
    client.cookies.clear()
    body = client.post("/app-panel/api/link/claim", json={"nonce": start["nonce"]}).json()
    h = {"Authorization": f"Bearer {body['token']}", "X-CSRF-Token": body["csrf"]}
    view = client.get("/app-panel/view?conversation_id=42", headers=h)
    assert view.status_code == 200 and "Take over" not in view.text and "can view bot details but not act" in view.text
    assert client.post("/app-panel/api/conversations/42/takeover", headers=h).status_code == 403
    r = client.post("/app-panel/api/conversations/42/send", data={"content": "hi"}, headers={**h, "HX-Request": "true"})
    assert "doesn't allow this" in r.text
    assert not [c for k, c in fake_cw.calls if k in ("send", "toggle")]


def test_role_without_panel_view_cannot_use_panel(client, db):
    add_role(db, "office", ["menu.view"])
    make_user(db, "o@example.com", role="office")
    start = client.post("/app-panel/api/link/start").json()
    login(client, "o@example.com")
    assert client.get(f"/admin/panel-link?code={start['code']}").status_code == 403
