"""Deleting knowledge files, users, customer data and menu photos."""
from decimal import Decimal
from pathlib import Path

from app.models import AuditLog, AuthSession, BotTurn, ContactMemory, ConversationState, KnowledgeVersion, MenuItem, MessageLog, Role, User
from app.security.permissions import clean
from app.services.knowledge import get_kb

from .conftest import csrf_from, login, make_user


def admin(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    return csrf_from(client.get("/admin/knowledge").text)


# ------------------------------------------------------------ knowledge
def test_delete_restore_purge_knowledge(client, db):
    t = admin(client, db)
    kb = get_kb()
    assert kb.search("deliver lekki")
    r = client.post("/admin/knowledge/delivery.md/delete", data={"csrf_token": t})
    assert r.status_code == 200 and "Deleted files" in r.text
    assert not kb.exists("delivery.md")
    assert all(c.chunk.source != "delivery.md" for c in kb.search("deliver lekki"))  # bot stops using it
    assert db.query(AuditLog).filter_by(action="knowledge.delete").count() == 1

    # A new file with the same name is refused while the old one is restorable.
    assert "Restore it, or purge it" in client.post("/admin/knowledge/new", data={"csrf_token": t, "filename": "delivery"}).text

    r = client.post("/admin/knowledge/deleted/delivery.md/restore", data={"csrf_token": t})
    assert "restored" in r.text and kb.exists("delivery.md") and kb.search("deliver lekki")[0].chunk.source == "delivery.md"

    assert client.post("/admin/knowledge/deleted/delivery.md/purge", data={"csrf_token": t}).status_code == 400  # still live
    client.post("/admin/knowledge/delivery.md/delete", data={"csrf_token": t})
    r = client.post("/admin/knowledge/deleted/delivery.md/purge", data={"csrf_token": t})
    assert "permanently deleted" in r.text
    assert db.query(KnowledgeVersion).filter_by(filename="delivery.md").count() == 0
    assert "delivery.md" not in client.get("/admin/knowledge").text


def test_knowledge_delete_needs_permission(client, db):
    db.add(Role(name="editor", permissions=clean(["knowledge.edit"])))
    db.commit()
    make_user(db, "e@example.com", role="editor")
    login(client, "e@example.com")
    page = client.get("/admin/knowledge")
    assert page.status_code == 200 and "/delete" not in page.text
    r = client.post("/admin/knowledge/hours.md/delete", data={"csrf_token": csrf_from(page.text)})
    assert r.status_code == 403 and get_kb().exists("hours.md")


def test_knowledge_delete_rejects_bad_names(client, db):
    t = admin(client, db)
    assert client.post("/admin/knowledge/..%2F..%2Fetc.md/delete", data={"csrf_token": t}).status_code in (404, 405)
    assert client.post("/admin/knowledge/nope.md/delete", data={"csrf_token": t}).status_code == 404


# ------------------------------------------------------------ users
def test_delete_user_removes_access_and_keeps_audit(client, db):
    t = admin(client, db)
    staff = make_user(db, "s@example.com", role="staff")
    db.add(AuthSession(id="x" * 64, user_id=staff.id, kind="web", csrf_token="c"))
    db.commit()
    staff_id = staff.id
    r = client.post(f"/admin/users/{staff_id}/delete", data={"csrf_token": t})
    assert r.status_code == 200 and "s@example.com" not in r.text
    db.expunge_all()
    assert db.query(User).filter_by(id=staff_id).first() is None and db.query(AuthSession).filter_by(user_id=staff_id).count() == 0
    row = db.query(AuditLog).filter_by(action="user.delete").one()
    assert row.before["email"] == "s@example.com"


# ------------------------------------------------------------ customer data
def _customer(db):
    db.add(ContactMemory(contact_id=77, name="Ada", phone="+2348030000000", notes="VIP", last_orders=[], summaries=[]))
    db.add(ConversationState(conversation_id=42, contact_id=77, bot_turns=1))
    db.add(MessageLog(conversation_id=42, chatwoot_message_id=1, direction="incoming", sender_type="contact", content="my address is 1 Secret St"))
    db.add(BotTurn(conversation_id=42, question="my address is 1 Secret St"))
    db.add(ConversationState(conversation_id=99, contact_id=5, bot_turns=0))
    db.add(MessageLog(conversation_id=99, chatwoot_message_id=2, direction="incoming", sender_type="contact", content="someone else"))
    db.commit()


def test_contacts_pages(client, db):
    _customer(db)
    admin(client, db)
    assert "Ada" in client.get("/admin/contacts?q=ada").text
    assert "Ada" in client.get("/admin/contacts?q=0803").text
    page = client.get("/admin/contacts/77")
    assert page.status_code == 200 and "1 stored message" in page.text
    t = csrf_from(page.text)
    client.post("/admin/contacts/77", data={"csrf_token": t, "name": "Ada O.", "notes": "VIP, no pepper"})
    db.expire_all()
    assert db.get(ContactMemory, 77).name == "Ada O."


def test_delete_memory_only_keeps_transcripts(client, db):
    _customer(db)
    t = admin(client, db)
    r = client.post("/admin/contacts/77/delete", data={"csrf_token": t, "scope": "memory"}, follow_redirects=True)
    assert "Contact memory deleted" in r.text
    assert db.get(ContactMemory, 77) is None
    assert db.query(MessageLog).filter_by(conversation_id=42).count() == 1


def test_erase_all_customer_data(client, db):
    _customer(db)
    t = admin(client, db)
    client.post("/admin/contacts/77/delete", data={"csrf_token": t, "scope": "all"})
    assert db.get(ContactMemory, 77) is None
    assert db.query(MessageLog).filter_by(conversation_id=42).count() == 0
    assert db.query(BotTurn).filter_by(conversation_id=42).count() == 0
    assert db.query(ConversationState).filter_by(contact_id=77).count() == 0
    assert db.query(MessageLog).filter_by(conversation_id=99).count() == 1  # other customers untouched
    row = db.query(AuditLog).filter_by(action="contact.erase_all").one()
    assert row.after["messages"] == 1 and "Ada" not in str(row.before) + str(row.after) and "Secret" not in str(row.after)


def test_contacts_need_permission(client, db):
    _customer(db)
    make_user(db, "s@example.com", role="staff")
    login(client, "s@example.com")
    assert client.get("/admin/contacts").status_code == 403
    t = csrf_from(client.get("/admin/menu").text)
    assert client.post("/admin/contacts/77/delete", data={"csrf_token": t, "scope": "all"}).status_code == 403
    assert db.get(ContactMemory, 77) is not None


def test_contact_flash_is_not_reflected(client, db):
    _customer(db)
    admin(client, db)
    assert "Call 0800-SCAM" not in client.get("/admin/contacts?done=Call 0800-SCAM").text


# ------------------------------------------------------------ menu photos
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_menu_photo_file_removed_with_item(client, db, settings):
    t = admin(client, db)
    client.post("/admin/menu/save", data={"csrf_token": t, "name": "Zobo", "price": "800", "available": "on"}, files={"photo": ("z.png", PNG, "image/png")})
    item = db.query(MenuItem).filter_by(name="Zobo").one()
    path = Path(settings.media_dir) / "photos" / item.photo_url.rsplit("/", 1)[-1]
    assert path.exists()
    client.post(f"/admin/menu/{item.id}/delete", data={"csrf_token": t})
    assert not path.exists()


def test_replaced_menu_photo_is_cleaned_up(client, db, settings):
    t = admin(client, db)
    client.post("/admin/menu/save", data={"csrf_token": t, "name": "Zobo", "price": "800"}, files={"photo": ("a.png", PNG, "image/png")})
    item = db.query(MenuItem).filter_by(name="Zobo").one()
    first = Path(settings.media_dir) / "photos" / item.photo_url.rsplit("/", 1)[-1]
    client.post("/admin/menu/save", data={"csrf_token": t, "item_id": str(item.id), "name": "Zobo", "price": "800"}, files={"photo": ("b.png", PNG, "image/png")})
    db.expire_all()
    assert not first.exists() and db.get(MenuItem, item.id).photo_url != ""
    db.add(MenuItem(name="Other", price=Decimal(1)))
    db.commit()
