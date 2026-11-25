"""Document import: PDF/DOCX/TXT/MD -> Markdown, safety checks, review-before-save flow."""
import io
import zipfile

import pytest
from docx import Document

from app.services.answer import unverified_amounts
from app.services.importer import ImportErrorUser, convert_upload, suggest_filename, text_to_markdown
from app.services.knowledge import get_kb

from .conftest import csrf_from, login, make_user

LIMIT = 10_000_000


def make_docx() -> bytes:
    doc = Document()
    doc.add_heading("Customer FAQ", level=0)  # Title style
    doc.add_heading("Delivery", level=1)
    doc.add_paragraph("Do you deliver to Lekki?")
    doc.add_paragraph("Yes, we deliver to Lekki Phase 1 every day.")
    doc.add_paragraph("Cash on delivery", style="List Bullet")
    doc.add_paragraph("Bank transfer", style="List Bullet")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Day", "Hours"
    table.cell(1, 0).text, table.cell(1, 1).text = "Monday", "10:00 | 22:00"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    """Build a minimal valid text PDF with correct xref offsets."""
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({l.replace('(', '').replace(')', '')}) Tj T*" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


# ------------------------------------------------------------ converters
def test_docx_headings_lists_tables_and_faq():
    md = convert_upload(make_docx(), "faq.docx", LIMIT).markdown
    assert md.startswith("# Customer FAQ")
    assert "## Delivery" in md
    assert "### Do you deliver to Lekki?" in md
    assert "- Cash on delivery\n- Bank transfer" in md
    assert "| Day | Hours |" in md and "| Monday | 10:00 \\| 22:00 |" in md


def test_pdf_text_extracted_and_questions_become_headings():
    pdf = make_pdf(["Do you deliver to Ikoyi?", "Yes, we deliver to Ikoyi daily."])
    res = convert_upload(pdf, "Delivery FAQ.pdf", LIMIT)
    assert res.kind == "pdf"
    assert res.markdown.startswith("# Delivery Faq")
    assert "## Do you deliver to Ikoyi?" in res.markdown
    assert "deliver to Ikoyi daily" in res.markdown
    assert res.warnings  # layout warning is always shown for PDFs


def test_scanned_pdf_without_text_rejected():
    with pytest.raises(ImportErrorUser, match="scanned"):
        convert_upload(make_pdf([]), "scan.pdf", LIMIT)


def test_text_reflow():
    md = text_to_markdown("Our hours are from ten in the morning\nuntil late at night.\n\n• Card\n• Cash\n\nCan I pay with\ntransfer?\nYes.", "Info")
    assert "Our hours are from ten in the morning until late at night." in md
    assert "- Card\n\n- Cash" in md
    assert "## Can I pay with transfer?" in md


def test_type_detected_by_magic_not_extension():
    with pytest.raises(ImportErrorUser, match="Unsupported"):
        convert_upload(b"MZ\x90\x00 not a document", "evil.pdf", LIMIT)
    with pytest.raises(ImportErrorUser, match=r"\.doc files"):
        convert_upload(b"\xd0\xcf\x11\xe0" + b"\x00" * 100, "old.doc", LIMIT)
    assert convert_upload(b"Hello there.\n", "notes.txt", LIMIT).kind == "txt"


def test_size_limit_and_empty():
    with pytest.raises(ImportErrorUser, match="limit"):
        convert_upload(b"x" * 101, "a.txt", 100)
    with pytest.raises(ImportErrorUser, match="empty"):
        convert_upload(b"", "a.txt", 100)


def test_zip_bomb_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\x00" * 60_000_000)
    with pytest.raises(ImportErrorUser, match="too large"):
        convert_upload(buf.getvalue(), "bomb.docx", LIMIT)


def test_non_word_zip_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<x/>")
    with pytest.raises(ImportErrorUser, match="Word"):
        convert_upload(buf.getvalue(), "sheet.xlsx", LIMIT)


def test_invalid_utf8_text_rejected():
    with pytest.raises(ImportErrorUser, match="UTF-8"):
        convert_upload(b"\xff\xfe\xfa bad", "a.txt", LIMIT)


def test_suggest_filename():
    assert suggest_filename("Customer FAQ (2024).pdf") == "customer-faq-2024.md"
    assert suggest_filename("../../etc/passwd") == "passwd.md"
    assert suggest_filename("Ọ̀rọ̀ Yorùbá.docx") == "oro-yoruba.md"


def test_prices_in_knowledge_are_not_trusted():
    from app.services.retriever import Chunk, ScoredChunk

    chunks = [ScoredChunk(Chunk("a#0", "old-menu.md", "Menu", "Jollof rice ₦3,000"), 1.0)]
    assert unverified_amounts("Jollof is ₦3,000", [], chunks) == ["₦3,000"]
    assert unverified_amounts("Jollof is ₦4,500", [{"output": {"price": 4500.0}}], chunks) == []


# ------------------------------------------------------------ endpoint flow
def _staff(client, db):
    make_user(db, "staff@example.com", role="staff")
    login(client, "staff@example.com")
    return csrf_from(client.get("/admin/knowledge").text)


def test_upload_opens_review_without_going_live(client, db):
    token = _staff(client, db)
    r = client.post("/admin/knowledge/upload", data={"csrf_token": token}, files={"document": ("Customer FAQ.docx", make_docx(), "application/octet-stream")})
    assert r.status_code == 200
    assert "Nothing is live yet" in r.text and "customer-faq.md" in r.text
    assert "### Do you deliver to Lekki?" in r.text
    assert not get_kb().exists("customer-faq.md")  # not saved yet


def test_save_after_review_creates_file_version_and_index(client, db):
    from app.models import AuditLog, KnowledgeVersion

    token = _staff(client, db)
    r = client.post("/admin/knowledge/customer-faq.md", data={"csrf_token": token, "content": "# FAQ\n\n## Do you deliver to Ajah?\n\nYes, every day.\n", "note": "imported from faq.docx", "create": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert get_kb().exists("customer-faq.md")
    assert db.query(KnowledgeVersion).filter_by(filename="customer-faq.md").count() == 1
    assert db.query(AuditLog).filter_by(action="knowledge.import").count() == 1
    assert get_kb().search("deliver to ajah")[0].chunk.source == "customer-faq.md"


def test_save_cannot_create_file_without_import_flag(client, db):
    token = _staff(client, db)
    r = client.post("/admin/knowledge/sneaky.md", data={"csrf_token": token, "content": "# x"}, follow_redirects=False)
    assert r.status_code == 404 and not get_kb().exists("sneaky.md")


def test_upload_requires_csrf_and_role(client, db):
    _staff(client, db)
    r = client.post("/admin/knowledge/upload", files={"document": ("a.txt", b"hello world text", "text/plain")})
    assert r.status_code == 403
    client.cookies.clear()
    r = client.post("/admin/knowledge/upload", files={"document": ("a.txt", b"hello", "text/plain")}, follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]


def test_upload_error_shown_to_user(client, db):
    token = _staff(client, db)
    r = client.post("/admin/knowledge/upload", data={"csrf_token": token}, files={"document": ("scan.pdf", make_pdf([]), "application/pdf")})
    assert r.status_code == 400 and "scanned" in r.text


def test_upload_bad_save_as_rejected(client, db):
    token = _staff(client, db)
    r = client.post("/admin/knowledge/upload", data={"csrf_token": token, "save_as": "../evil"}, files={"document": ("a.txt", b"hello world", "text/plain")})
    assert r.status_code == 400 and "Save as" in r.text
