import json
from types import SimpleNamespace
import pytest
from core import decisions, rag


def rows(count=3):
    return [{"id": f"S{i}", "source": f"{i}.md", "title": f"Receita {i}", "text": f"{i} ovos"} for i in range(1, count + 1)]


def test_jev_reorders_and_filters_once(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"answers": {
            "S1": {"noul": .7}, "S2": {"noul": .95}, "S3": {"noul": .2}}})
    monkeypatch.setattr(decisions.requests, "post", post)
    selected, degraded = decisions.rank_sources("quantos ovos?", rows() + [dict(rows()[0], text="150 g farinha")], float("inf"))
    assert [r["id"] for r in selected] == ["S2", "S1", "S1"] and not degraded
    assert len(calls) == 1 and calls[0][0].endswith("/systemone")
    assert calls[0][1]["json"]["questions"]["S1"]["type"] == "noul"


@pytest.mark.parametrize("score", [float("nan"), float("inf"), 1.1, -.1])
def test_bad_relevance_preserves_candidates(monkeypatch, score):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setattr(decisions.requests, "post", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"answers": {"S1": {"noul": score}}}))
    original = rows(1)
    assert decisions.rank_sources("eggs", original, float("inf")) == (original, True)


def test_no_six_note_cap():
    hits = [{"source": str(i), "text": "short evidence", "_distance": .1} for i in range(30)]
    assert len(rag.select_hits(hits, {str(i) for i in range(30)})) == 30


def test_answer_uses_history_titles_and_structure(monkeypatch):
    monkeypatch.setenv("RAG_JEV_ENABLED", "false")
    calls = []
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    def invoke(chain, messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps({"answer": "## Receita\n- 1 ovo [S1]", "used_sources": ["S1"]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    history = [{"role": "user", "content": "Quero a receita."}, {"role": "assistant", "content": "Receita encontrada."}]
    result = rag.answer("E os ingredientes?", rows(1), history)
    data = json.loads(calls[0][1]["content"])
    assert data["conversation_history"] == history
    assert result["source_titles"] == {"1.md": "Receita 1"}
    assert "headings" in calls[0][0]["content"]
    assert "receita" in rag.retrieval_question("E os ingredientes?", history)
    assert len(rag.retrieval_question("x" * 3990, history)) <= 4000


def test_compact_prompt_preserves_all_excerpts_and_saves_payload():
    evidence = [{"id": "S1", "source": "a-very-long-internal-filename.md", "title": "Receita de pão", "text": text}
                for text in ("2 ovos", "150 g farinha", "2 ovos")]
    compact = decisions.group_sources(evidence)
    assert compact == {"S1": {"title": "Receita de pão", "excerpts": ["2 ovos", "150 g farinha"]}}
    assert len(json.dumps(compact)) < len(json.dumps(evidence))


def test_citation_repair_receives_bad_answer(monkeypatch):
    monkeypatch.setenv("RAG_JEV_ENABLED", "false")
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    calls = []
    def invoke(chain, messages, **kwargs):
        calls.append(list(messages))
        source = "S99" if len(calls) == 1 else "S1"
        return SimpleNamespace(content=json.dumps({"answer": f"1 ovo [{source}]", "used_sources": [source]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    assert rag.answer("ovos?", rows(1))["sources"] == ["1.md"]
    assert len(calls) == 2 and "S99" in calls[1][-2]["content"]
    assert calls[1][-2]["role"] == "assistant"


def test_retrieval_pages_without_loading_embeddings(tmp_path, monkeypatch):
    import lancedb
    monkeypatch.setattr(rag, "VECTOR_DIR", str(tmp_path))
    monkeypatch.setattr(rag, "active_table", lambda: "notes")
    monkeypatch.setattr(rag, "_embed_query", lambda _: [1.0])
    monkeypatch.setenv("RAG_CANDIDATE_CHARS", "900")
    calls, position = [], {}
    query = SimpleNamespace()
    query.metric = lambda _: query
    query.select = lambda columns: calls.append(columns) or query
    query.offset = lambda offset: position.update(offset=offset) or query
    query.limit = lambda limit: calls.append(limit) or query
    query.to_list = lambda: [{"source": str(i), "text": "x" * 500, "_distance": .1} for i in range(64)]
    table = SimpleNamespace(count_rows=lambda: 100000, search=lambda _: query)
    connection = SimpleNamespace(table_names=lambda: ["notes"], open_table=lambda _: table)
    monkeypatch.setattr(lancedb, "connect", lambda _: connection)
    assert len(rag.retrieve("ovos", {str(i) for i in range(64)})) == 1
    assert calls == [["id", "source", "text"], 64]


def test_duplicate_retrieval_text_is_not_paid_twice():
    hits = [{"source": "a", "text": "Title: Recipe\n\n2 ovos", "_distance": .1}] * 2
    assert rag.select_hits(hits, {"a": "Recipe"}) == [{"source": "a", "id": "S1", "text": "2 ovos", "title": "Recipe"}]


def test_movie_followups_keep_original_topic_after_refusal(monkeypatch):
    topic = "give me a movie to watch now that has a plot twist and is not the prestige"
    history = [{"role": "user", "content": topic},
               {"role": "assistant", "content": "Prisoners (2013) [S1]"}]
    assert rag.retrieval_question("give me another one", history) == topic
    history += [{"role": "user", "content": "give me another one"},
                {"role": "assistant", "content": "I couldn't find that in your saved notes."}]
    assert rag.retrieval_question("give me another movie like that", history) == topic
    assert rag.retrieval_question("How do I make pancakes?", history) == "How do I make pancakes?"
    queries = []
    monkeypatch.setattr(decisions, "rank_sources", lambda query, evidence, deadline: (queries.append(query) or evidence, False))
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    def invoke(chain, messages, **kwargs):
        data = json.loads(messages[1]["content"])
        assert data["conversation_history"] == history
        assert "excluding items already suggested" in messages[0]["content"]
        return SimpleNamespace(content=json.dumps({"answer": "Shutter Island [S1]", "used_sources": ["S1"]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    evidence = [{"id": "S1", "source": "movies.md", "title": "Plot twist movies",
                 "text": "Prisoners, Shutter Island, The Prestige"}]
    assert rag.answer("give me another movie like that", evidence, history)["sources"] == ["movies.md"]
    assert queries == [topic]


def test_stored_topic_survives_many_short_followups():
    topic = "filmes com reviravoltas"
    history = [{"role": "user", "content": "outro filme", "retrieval_topic": topic},
               {"role": "assistant", "content": "Prisoners"}]
    assert rag.retrieval_question("outro filme", history) == topic
