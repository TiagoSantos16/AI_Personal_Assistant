from pathlib import Path
from core import rag


def test_real_lancedb_generation_and_escaped_source(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "VECTOR_DIR", str(tmp_path / "vectors"))
    monkeypatch.setattr(rag, "NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setattr(rag, "_embed_passages", lambda texts: [[1.0] + [0.0] * 383 for _ in texts])
    monkeypatch.setattr(rag, "_embed_query", lambda _: [1.0] + [0.0] * 383)
    notes = Path(rag.NOTES_DIR)
    notes.mkdir()
    path = notes / "Chef's_recipe.md"
    path.write_text("# Receita\n2 ovos e 150 g farinha.\n\n**Generation Info:**\nPrivate diagnostics", encoding="utf-8")
    rag.index_note(str(path), None)
    hits = rag.retrieve("ovos", {path.name})
    assert hits and "Private diagnostics" not in hits[0]["text"]
    before = rag.active_table()
    rag.reindex_all_notes(None, [path])
    assert rag.active_table() != before
    assert rag.retrieve("ovos", {path.name})
    rag.remove_from_index(path.name, None)
    assert rag.retrieve("ovos", {path.name}) == []
