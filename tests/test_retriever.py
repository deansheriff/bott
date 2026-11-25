import pytest

from app.services.knowledge import KnowledgeBase
from app.services.retriever import BM25Retriever, Chunk, chunk_markdown, tokenize

DOC = """# Restaurant

## Opening hours

We open every day from 10:00 to 22:00.

## Delivery

We deliver to Lekki, Ikoyi and Victoria Island.

### Pickup

Pick up at the front counter.

## Payment

We accept bank transfer and card.
"""


def test_chunking_follows_headings():
    chunks = chunk_markdown("info.md", DOC)
    headings = [c.heading for c in chunks]
    assert "Restaurant > Opening hours" in headings
    assert "Restaurant > Delivery > Pickup" in headings
    assert all(c.source == "info.md" for c in chunks)


def test_long_sections_are_split():
    text = "# Big\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    chunks = chunk_markdown("big.md", text, max_chars=500)
    assert len(chunks) > 3 and all(len(c.text) <= 800 for c in chunks)


def test_bm25_ranks_relevant_chunk_first():
    r = BM25Retriever()
    r.index(chunk_markdown("info.md", DOC))
    top = r.search("do you deliver to lekki?", k=2)
    assert top and "Delivery" in top[0].chunk.heading
    assert "Opening hours" in r.search("what time do you open", k=1)[0].chunk.heading
    assert "Payment" in r.search("can I pay by card", k=1)[0].chunk.heading


def test_no_match_returns_empty():
    r = BM25Retriever()
    r.index(chunk_markdown("info.md", DOC))
    assert r.search("helicopter licence", k=3) == []
    assert r.search("", k=3) == []


def test_stemming_and_stopwords():
    assert tokenize("Deliveries to the islands") == ["delivery", "island"]


def test_retriever_is_swappable(tmp_path):
    class FixedRetriever(BM25Retriever):
        def search(self, query, k=4):
            return []

    kb = KnowledgeBase(tmp_path, retriever=FixedRetriever())
    (tmp_path / "a.md").write_text(DOC, encoding="utf-8")
    kb.reindex()
    assert kb.search("delivery") == []


def test_reindex_picks_up_new_files(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.reindex()
    assert kb.search("jollof") == []
    kb.write("menu-notes.md", "# Menu\n\nOur jollof rice is smoky.\n")
    kb.reindex()
    assert kb.search("jollof")[0].chunk.source == "menu-notes.md"


def test_validation_and_path_safety(tmp_path):
    kb = KnowledgeBase(tmp_path)
    assert kb.validate("ok.md", "# hi", 1000) == []
    assert kb.validate("../evil.md", "# hi", 1000)
    assert kb.validate("UPPER.md", "# hi", 1000)
    assert kb.validate("ok.md", "", 1000)
    assert kb.validate("ok.md", "x" * 2000, 1000)
    with pytest.raises(ValueError):
        kb.write("../../etc/passwd.md", "x")
    with pytest.raises(ValueError):
        kb.read("..\\secret.md")
