"""Convert uploaded documents (PDF, DOCX, TXT, MD) into knowledge Markdown.

Uploads are untrusted: type is decided by magic bytes, not the filename; DOCX
archives are checked for zip bombs before parsing; PDFs are page-limited. The
original file is never stored, only the converted text, and nothing goes live
until a person reviews it and saves a version.

FAQ-friendly: short lines/paragraphs ending in "?" become "##" headings, so each
question and its answer becomes its own search chunk.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePath

MAX_PDF_PAGES = 300
MAX_DOCX_UNCOMPRESSED = 50_000_000
MAX_ZIP_ENTRIES = 2_000
MAX_QUESTION_LEN = 200
_BULLET_RE = re.compile(r"^\s*(?:[•●▪◦‣\-\*–]|\(?\d{1,3}[\.\)])\s+")


class ImportErrorUser(Exception):
    """An error whose message is safe and useful to show to staff."""


@dataclass
class ImportResult:
    markdown: str
    kind: str
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers
def suggest_filename(original: str) -> str:
    stem = PurePath(original or "document").stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:56] or "document"
    if not slug[0].isalnum():
        slug = "doc-" + slug
    return f"{slug}.md"


def _title_from(original: str) -> str:
    stem = PurePath(original or "Document").stem
    return re.sub(r"[_\-]+", " ", stem).strip().title() or "Document"


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\x00", "")
    text = re.sub(r"[\u200b\u00ad\ufeff]", "", text)  # zero-width, soft hyphen, BOM
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def _is_question(text: str) -> bool:
    return text.endswith("?") and len(text) <= MAX_QUESTION_LEN and "\n" not in text


def _escape_heading_text(text: str) -> str:
    return text.lstrip("#").strip()


def detect_kind(data: bytes, filename: str) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "docx"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise ImportErrorUser("Old Word .doc files aren't supported. Open it in Word and save as .docx, or export to PDF.")
    ext = PurePath(filename or "").suffix.lower()
    if ext in (".txt", ".md", ".markdown"):
        return "md" if ext != ".txt" else "txt"
    raise ImportErrorUser("Unsupported file type. Upload a PDF, Word (.docx), .txt or .md file.")


# ------------------------------------------------------------------ text/markdown
def text_to_markdown(text: str, title: str) -> str:
    """Re-flow hard-wrapped plain text (e.g. from PDFs) into paragraphs, lists and FAQ headings."""
    blocks: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(" ".join(para))
            para.clear()

    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            flush()
            continue
        if _BULLET_RE.match(line):
            flush()
            blocks.append("- " + _BULLET_RE.sub("", line, count=1))
            continue
        if _is_question(line) and not para:
            flush()
            blocks.append("## " + _escape_heading_text(line))
            continue
        para.append(line)
        # A question wrapped over several lines ("Do you deliver to / Lekki?").
        if len(para) > 1 and _is_question(" ".join(para)):
            q = " ".join(para)
            para.clear()
            blocks.append("## " + _escape_heading_text(q))
    flush()

    # Join hyphenated breaks that slipped through ("deli- very" -> "delivery").
    out = [re.sub(r"(\w)- (\w)", r"\1\2", b) if not b.startswith("#") else b for b in blocks]
    body = "\n\n".join(out)
    return f"# {title}\n\n{body}\n" if body else ""


def convert_txt(data: bytes, title: str) -> ImportResult:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportErrorUser("The text file isn't valid UTF-8. Save it as UTF-8 and try again.") from exc
    return ImportResult(text_to_markdown(text, title), "txt")


def convert_md(data: bytes, title: str) -> ImportResult:
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\x00", "")
    except UnicodeDecodeError as exc:
        raise ImportErrorUser("The Markdown file isn't valid UTF-8.") from exc
    if not text.lstrip().startswith("#"):
        text = f"# {title}\n\n{text}"
    return ImportResult(text.strip() + "\n", "md")


# ------------------------------------------------------------------ pdf
def convert_pdf(data: bytes, title: str) -> ImportResult:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ImportErrorUser("This PDF is password-protected. Remove the password and upload it again.")
            except ImportErrorUser:
                raise
            except Exception as exc:
                raise ImportErrorUser("This PDF is encrypted in a way that can't be read. Remove the protection and try again.") from exc
        pages = reader.pages
        n = len(pages)
        if n > MAX_PDF_PAGES:
            warnings.append(f"Only the first {MAX_PDF_PAGES} of {n} pages were imported.")
        texts = []
        for i, page in enumerate(pages):
            if i >= MAX_PDF_PAGES:
                break
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                warnings.append(f"Page {i + 1} could not be read and was skipped.")
    except ImportErrorUser:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ImportErrorUser("This PDF couldn't be read. It may be damaged; try exporting it again.") from exc

    joined = "\n\n".join(texts)
    if len(_clean(joined)) < 20:
        raise ImportErrorUser("No text was found in this PDF. It's probably a scanned image; OCR isn't supported. Upload the original Word file or a text-based PDF.")
    warnings.append("PDF layout (columns, tables) can't be fully recovered. Check headings and lists before saving.")
    return ImportResult(text_to_markdown(joined, title), "pdf", warnings)


# ------------------------------------------------------------------ docx
def _check_zip(data: bytes) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportErrorUser("This file isn't a valid .docx document.") from exc
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
        raise ImportErrorUser("This document is too large once unpacked.")
    if "word/document.xml" not in zf.namelist():
        raise ImportErrorUser("This file isn't a Word .docx document. (Other Office formats aren't supported.)")


def _heading_level(style_name: str) -> int | None:
    name = (style_name or "").lower()
    if name == "title":
        return 1
    m = re.match(r"heading\s*(\d)", name)
    if m:
        return min(int(m.group(1)) + 1, 6)  # document title is "#", so Heading 1 -> "##"
    return None


def _table_to_md(table) -> str:
    rows = []
    for row in table.rows:
        cells = [_clean(c.text).replace("|", "\\|").replace("\n", " ") for c in row.cells]
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def convert_docx(data: bytes, title: str) -> ImportResult:
    _check_zip(data)
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:
        raise ImportErrorUser("This Word document couldn't be read. Try saving it again as .docx.") from exc

    blocks: list[str] = []
    has_title = False
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            text = _clean(p.text)
            if not text:
                continue
            level = _heading_level(p.style.name if p.style is not None else "")
            style = (p.style.name if p.style is not None else "").lower()
            if level == 1:
                if not has_title:
                    blocks.append("# " + _escape_heading_text(text))
                    has_title = True
                else:
                    blocks.append("## " + _escape_heading_text(text))
            elif level:
                blocks.append("#" * level + " " + _escape_heading_text(text))
            elif "list" in style or _BULLET_RE.match(text):
                blocks.append("- " + _BULLET_RE.sub("", text, count=1))
            elif _is_question(text):
                blocks.append("### " + _escape_heading_text(text))
            else:
                blocks.append(text)
        elif tag == "tbl":
            md = _table_to_md(Table(el, doc))
            if md:
                blocks.append(md)

    # Keep consecutive list items together.
    out: list[str] = []
    for b in blocks:
        if out and b.startswith("- ") and out[-1].startswith("- "):
            out[-1] += "\n" + b
        else:
            out.append(b)
    if not out:
        raise ImportErrorUser("No text was found in this Word document.")
    if not has_title:
        out.insert(0, f"# {title}")
    return ImportResult("\n\n".join(out) + "\n", "docx")


# ------------------------------------------------------------------ entry point
def convert_upload(data: bytes, filename: str, max_bytes: int) -> ImportResult:
    if not data:
        raise ImportErrorUser("The file is empty.")
    if len(data) > max_bytes:
        raise ImportErrorUser(f"The file is larger than the {max_bytes // 1_000_000} MB upload limit.")
    kind = detect_kind(data, filename)
    title = _title_from(filename)
    result = {"pdf": convert_pdf, "docx": convert_docx, "txt": convert_txt, "md": convert_md}[kind](data, title)
    if not result.markdown.strip():
        raise ImportErrorUser("No text was found in this file.")
    return result
