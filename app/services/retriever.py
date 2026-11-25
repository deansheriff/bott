"""Knowledge retrieval. BM25 today; implement `Retriever` to swap in embeddings."""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from have how i if in is it its me my
    of on or our please so that the their them then there these they this to us was we what
    when where which who why will with you your yours hi hello hey""".split()
)


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    heading: str
    text: str


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float

    def as_dict(self) -> dict:
        return {
            "id": self.chunk.id,
            "source": self.chunk.source,
            "heading": self.chunk.heading,
            "score": round(self.score, 3),
            "text": self.chunk.text,
        }


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3] in "sxz":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _WORD_RE.findall(text.lower()) if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


def chunk_markdown(source: str, text: str, max_chars: int = 900) -> list[Chunk]:
    """Split Markdown by headings, then by paragraphs, into chunks of ~max_chars."""
    chunks: list[Chunk] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        heading = " > ".join(h for _, h in path) or source
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        current = ""
        for p in paras:
            if current and len(current) + len(p) + 2 > max_chars:
                chunks.append(Chunk(f"{source}#{len(chunks)}", source, heading, current))
                current = p
            else:
                current = f"{current}\n\n{p}" if current else p
        if current:
            chunks.append(Chunk(f"{source}#{len(chunks)}", source, heading, current))

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path = [(lvl, h) for lvl, h in path if lvl < level]
            path.append((level, m.group(2)))
        else:
            buf.append(line)
    flush()
    return chunks


class Retriever(ABC):
    @abstractmethod
    def index(self, chunks: list[Chunk]) -> None: ...

    @abstractmethod
    def search(self, query: str, k: int = 4) -> list[ScoredChunk]: ...


@dataclass
class BM25Retriever(Retriever):
    k1: float = 1.5
    b: float = 0.75
    _chunks: list[Chunk] = field(default_factory=list)
    _tfs: list[Counter] = field(default_factory=list)
    _lens: list[int] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _avgdl: float = 0.0

    def index(self, chunks: list[Chunk]) -> None:
        self._chunks = list(chunks)
        self._tfs, self._lens, self._df = [], [], Counter()
        for c in self._chunks:
            # Headings are counted twice: they are strong topical signals.
            toks = tokenize(c.heading) * 2 + tokenize(c.text)
            tf = Counter(toks)
            self._tfs.append(tf)
            self._lens.append(len(toks))
            self._df.update(tf.keys())
        self._avgdl = (sum(self._lens) / len(self._lens)) if self._lens else 0.0

    def search(self, query: str, k: int = 4) -> list[ScoredChunk]:
        q = set(tokenize(query))
        if not q or not self._chunks:
            return []
        n = len(self._chunks)
        results: list[ScoredChunk] = []
        for i, tf in enumerate(self._tfs):
            score = 0.0
            dl = self._lens[i] or 1
            for term in q:
                f = tf.get(term)
                if not f:
                    continue
                df = self._df[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                score += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1)))
            if score > 0:
                results.append(ScoredChunk(self._chunks[i], score))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:k]

    @property
    def size(self) -> int:
        return len(self._chunks)
