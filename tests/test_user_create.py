"""Creating logins directly (admin-set or generated passwords) and setting passwords."""
import re

from app.models import AuditLog, AuthSession, Role, User
from app.security.passwords import generate_password, password_problem
from app.security.permissions import clean

from .conftest import PASSWORD, csrf_from, login, make_user


def admin(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    return csrf_from(client.get("/admin/users").text)


def generated_from(html: str) -> str:
    m = re.search(r'Generated password.*?value="([^"]+)"', html, re.S)
    assert m, "no generated password shown"
    return m.group(1)


def test_generator_is_strong():
    pw = generate_password()
    assert len(pw) == 23 and password_problem(pw) is None and pw != generate_password()


def test_create_login_with_typed_password(client, db):
    t = admin(client, db)
    r = client.post("/admin/users/create", data={"csrf_token": t, "email": "Cook@Example.com", "role": "staff", "password": "Kitchen-Pass-2026"})
    assert r.status_code == 200 and "Login created for cook@example.com" in r.text
    assert "Kitchen-Pass-2026" not in r.text  # typed passwords are never echoed back
    u = db.query(User).filter_by(email="cook@example.com").one()
    assert u.role == "staff" and not u.must_change_password and u.password_hash
    client.cookies.clear()
    assert login(client, "cook@example.com", "Kitchen-Pass-2026").headers["location"] == "/admin/"


def test_create_login_with_generated_password_and_forced_change(client, db):
    t = admin(client, db)
    r = client.post("/admin/users/create", data={"csrf_token": t, "email": "agent1@example.com", "role": "agent", "password": "", "must_change": "on"})
    pw = generated_from(r.text)
    assert db.query(User).filter_by(email="agent1@example.com").one().must_change_password
    client.cookies.clear()
    assert login(client, "agent1@example.com", pw).headers["location"] == "/admin/password"


def test_create_rejects_weak_duplicate_and_bad_input(client, db):
    t = admin(client, db)
    assert "at least 12" in client.post("/admin/users/create", data={"csrf_token": t, "email": "w@example.com", "role": "staff", "password": "short"}).text
    assert client.post("/admin/users/create", data={"csrf_token": t, "email": "a@example.com", "role": "staff", "password": PASSWORD}).status_code == 400
    assert client.post("/admin/users/create", data={"csrf_token": t, "email": "not-an-email", "role": "staff"}).status_code == 400
    assert client.post("/admin/users/create", data={"email": "x@example.com", "role": "staff"}).status_code == 403  # no CSRF
    assert db.query(User).filter_by(email="w@example.com").count() == 0


def test_create_respects_role_limits(client, db):
    db.add(Role(name="manager", permissions=clean(["users.edit", "menu.view"])))
    db.add(Role(name="menu-viewer", permissions=clean(["menu.view"])))
    db.commit()
    make_user(db, "m@example.com", role="manager")
    login(client, "m@example.com")
    t = csrf_from(client.get("/admin/users").text)
    assert client.post("/admin/users/create", data={"csrf_token": t, "email": "x@example.com", "role": "admin", "password": PASSWORD}).status_code == 403
    assert client.post("/admin/users/create", data={"csrf_token": t, "email": "y@example.com", "role": "menu-viewer", "password": PASSWORD}).status_code == 200


def test_set_password_signs_user_out(client, db):
    t = admin(client, db)
    staff = make_user(db, "s@example.com", role="staff")
    db.add(AuthSession(id="s" * 64, user_id=staff.id, kind="web", csrf_token="c"))
    db.commit()
    r = client.post(f"/admin/users/{staff.id}/password", data={"csrf_token": t, "password": "", "must_change": "on"})
    pw = generated_from(r.text)
    assert db.query(AuthSession).filter_by(user_id=staff.id).count() == 0
    db.expire_all()
    assert db.get(User, staff.id).must_change_password
    client.cookies.clear()
    assert login(client, "s@example.com", PASSWORD).status_code == 401
    assert login(client, "s@example.com", pw).status_code == 303


def test_cannot_set_password_for_higher_access_or_self(client, db):
    db.add(Role(name="manager", permissions=clean(["users.edit", "menu.view"])))
    db.commit()
    me = make_user(db, "m@example.com", role="manager")
    boss = make_user(db, "boss@example.com", role="admin")
    login(client, "m@example.com")
    t = csrf_from(client.get("/admin/users").text)
    assert client.post(f"/admin/users/{boss.id}/password", data={"csrf_token": t, "password": "Takeover-Pass-123"}).status_code == 403
    assert client.post(f"/admin/users/{me.id}/password", data={"csrf_token": t, "password": "Takeover-Pass-123"}).status_code == 400
    client.cookies.clear()
    assert login(client, "boss@example.com", "Takeover-Pass-123").status_code == 401


def test_audit_never_stores_password(client, db):
    t = admin(client, db)
    client.post("/admin/users/create", data={"csrf_token": t, "email": "c@example.com", "role": "staff", "password": "Secret-Kitchen-99"})
    rows = db.query(AuditLog).filter(AuditLog.action.in_(["user.create", "user.password_set"])).all()
    assert rows and all("Secret-Kitchen-99" not in str(r.after) for r in rows)


def test_invite_link_still_available(client, db):
    t = admin(client, db)
    r = client.post("/admin/users/invite", data={"csrf_token": t, "email": "i@example.com", "role": "staff"})
    assert "/admin/set-password/" in r.text
