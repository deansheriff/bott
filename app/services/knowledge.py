"""Knowledge folder: file I/O, validation, versioning, Markdown preview and the search index."""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
from pathlib import Path

import markdown as md
import nh3
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import KnowledgeVersion, User
from ..utils import sha256_hex
from .retriever import BM25Retriever, Retriever, ScoredChunk, chunk_markdown

log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}\.md$")


class KnowledgeBase:
    def __init__(self, directory: str | Path, retriever: Retriever | None = None) -> None:
        self.dir = Path(directory)
        self.retriever = retriever or BM25Retriever()
        self._lock = threading.Lock()
        self.chunk_count = 0

    # ------------------------------------------------------------- files
    def _path(self, name: str) -> Path:
        if not FILENAME_RE.match(name):
            raise ValueError("invalid filename")
        path = (self.dir / name).resolve()
        if path.parent != self.dir.resolve():
            raise ValueError("invalid path")
        return path

    def list_files(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name for p in self.dir.glob("*.md") if FILENAME_RE.match(p.name))

    def exists(self, name: str) -> bool:
        try:
            return self._path(name).exists()
        except ValueError:
            return False

    def read(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> None:
        path = self._path(name)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    @staticmethod
    def validate(name: str, content: str, max_bytes: int) -> list[str]:
        errors = []
        if not FILENAME_RE.match(name or ""):
            errors.append("Filename must be lowercase letters, digits, '-' or '_' and end in .md")
        if not content.strip():
            errors.append("Content must not be empty.")
        if "\x00" in content:
            errors.append("Content contains invalid characters.")
        size = len(content.encode("utf-8"))
        if size > max_bytes:
            errors.append(f"File is {size:,} bytes; the limit is {max_bytes:,} bytes.")
        return errors

    # ------------------------------------------------------------- index
    def reindex(self) -> int:
        chunks = []
        for name in self.list_files():
            try:
                chunks.extend(chunk_markdown(name, self.read(name)))
            except (OSError, UnicodeDecodeError):
                log.warning("knowledge.read_failed", extra={"file": name})
        with self._lock:
            self.retriever.index(chunks)
            self.chunk_count = len(chunks)
        log.info("knowledge.reindexed", extra={"chunks": len(chunks)})
        return len(chunks)

    def search(self, query: str, k: int = 4) -> list[ScoredChunk]:
        with self._lock:
            return self.retriever.search(query, k)


_kb: KnowledgeBase | None = None


def init_kb(directory: str | Path) -> KnowledgeBase:
    global _kb
    _kb = KnowledgeBase(directory)
    _kb.dir.mkdir(parents=True, exist_ok=True)
    _kb.reindex()
    return _kb


def get_kb() -> KnowledgeBase:
    if _kb is None:
        raise RuntimeError("knowledge base not initialised")
    return _kb


# ------------------------------------------------------------ versioning
def latest_version_no(db: Session, name: str) -> int:
    return db.scalar(select(func.max(KnowledgeVersion.version)).where(KnowledgeVersion.filename == name)) or 0


def save_version(db: Session, kb: KnowledgeBase, name: str, content: str, user: User | None, note: str = "") -> KnowledgeVersion:
    content = content.replace("\r\n", "\n")
    version = KnowledgeVersion(
        filename=name,
        version=latest_version_no(db, name) + 1,
        content=content,
        sha256=sha256_hex(content),
        note=note[:255] or None,
        created_by=user.email if user else "system",
    )
    db.add(version)
    db.flush()
    kb.write(name, content)
    return version


def seed_versions(db: Session, kb: KnowledgeBase) -> None:
    """Create version 1 for files on disk that have no history (first boot / files dropped in the volume)."""
    for name in kb.list_files():
        if latest_version_no(db, name) == 0:
            content = kb.read(name)
            db.add(KnowledgeVersion(filename=name, version=1, content=content, sha256=sha256_hex(content), note="initial import", created_by="system"))
    db.commit()


ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "ul", "ol", "li",
    "blockquote", "code", "pre", "table", "thead", "tbody", "tr", "th", "td", "a",
}


def render_markdown_safe(text: str) -> str:
    html = md.markdown(text, extensions=["tables", "sane_lists"])
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes={"a": {"href"}}, url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")


def unified_diff(old: str, new: str, old_label: str, new_label: str) -> list[str]:
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), old_label, new_label, lineterm=""))
