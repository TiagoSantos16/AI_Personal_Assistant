"""F02/F03 behaviour, provenance, source association and concurrent workers."""
import json
from types import SimpleNamespace

import pytest
from core import graph, llm, memory, rag, requests, storage, workflow


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "index_note", lambda *a: None)
    monkeypatch.setattr(rag, "remove_from_index", lambda *a: None)
    return lambda op, payload=None: storage.dispatch(op, payload, root=tmp_path)


def accept(store, source="one", **extra):
    return store("accept", {"url": f"https://instagram.com/p/{source}/", "chat_id": 7,
                           "message_id": 10, **extra})["job"]


def identity(job):
    return {"job_id": job["job_id"], "attempt": job["attempt"]}


def save(store, job, title):
    store("claim", identity(job))
    return store("save", {**identity(job), "content": "# " + title, "metadata": {"title": title}})


def remember(store, **extra):
    return store("memory_change", {"action": "remember", "key": "watched:arrival", "scope": "movies",
        "kind": "fact", "text": "I watched Arrival", "chat_id": 7, "message_id": 30, **extra})


def test_memory_survives_reopen_correct_and_forget(store, tmp_path):
    item = remember(store)
    assert item["source"] == {"chat_id": 7, "message_id": 30}
    assert item["created"] and item["updated"]
    assert storage.dispatch("memory_list", root=tmp_path) == [item]
    assert remember(store)["id"] == item["id"]
    corrected = store("memory_change", {"action": "correct", "id": item["id"], "text": "I have not watched Arrival",
        "chat_id": 7, "message_id": 31})
    assert corrected["revision"] != item["revision"]
    assert corrected["source"]["message_id"] == 31
    assert len(store("memory_list")) == 1
    store("memory_change", {"action": "forget", "id": item["id"]})
    assert store("memory_list") == []


def test_late_model_cannot_resurrect_forgotten_memory(store):
    item = remember(store)
    revision = store("memory_revision")
    store("memory_change", {"action": "forget", "id": item["id"]})
    with pytest.raises(ValueError, match="Memory changed"):
        remember(store, expected_revision=revision)
    assert store("memory_list") == []


def test_relevance_and_scoped_preferences(store):
    film = remember(store)
    note_style = remember(store, key="length", text="Use concise notes", kind="preference", scope="notes")
    assert memory.select(store("memory_list"), "recommend a movie") == [film]
    assert memory.select(store("memory_list"), "uma receita", purpose="notes") == [note_style]
    assert not memory.select(store("memory_list"), "python programming", purpose="chat")
    for n in range(15):
        remember(store, key=f"watched:{n}", text=f"I watched film {n}")
    assert len(store("memory_list", {"query": "movies"})) <= 8


def test_reply_association_ambiguity_expiry_and_chat_isolation(store, monkeypatch):
    first = accept(store)
    accept(store, "two", message_id=11)
    assert store("source_context", {"chat_id": 7})["status"] == "ambiguous"
    context = store("source_context", {"chat_id": 7, "reply_id": 10})
    assert context["source_id"] == first["source_id"]
    assert store("source_context", {"chat_id": 8, "reply_id": 10})["status"] == "missing"
    assert store("source_context", {"chat_id": 7, "reply_id": 999})["status"] == "missing"
    now = storage.time.time()
    monkeypatch.setattr(storage.time, "time", lambda: now + 601)
    assert store("source_context", {"chat_id": 7})["status"] == "missing"
    assert store("source_context", {"chat_id": 7, "reply_id": 10})["status"] == "found"


def test_instruction_revision_blocks_late_worker_and_preserves_cache(store):
    first = accept(store, instructions="Use bullets")
    store("claim", identity(first))
    evidence = {"description": "original source", "visual_extraction": "code"}
    store("cache_put", {**identity(first), "evidence": evidence})
    second = accept(store, instructions="Focus on code", message_id=12)
    assert [i["text"] for i in second["instructions"]] == ["Use bullets", "Focus on code"]
    assert store("save", {**identity(first), "content": "late", "metadata": {}})["status"] == "stale"
    assert store("cache_put", {**identity(first), "evidence": {}})["status"] == "stale"
    assert store("cache_get", {"source_id": "one"}) == evidence
    assert store("accept", {"url": first["url"]})["status"] == "running"
    assert store("accept", {"url": first["url"], "instructions": "Focus on code"})["status"] == "running"


def test_superseded_inflight_costs_follow_replacement(store):
    from core.accounting import record
    first = accept(store)
    store("claim", identity(first))
    charge1 = record({"id": "old-start", "usage": {"cost": .01}}, "writer", 1)
    store("ledger", {**identity(first), "records": [charge1]})
    second = accept(store, instructions="Focus on implementation", message_id=12)
    assert second["accounting"] == [charge1]
    saved = save(store, second, "Revised")
    charge2 = record({"id": "old-inflight", "usage": {"cost": .02}}, "critic", 1)
    store("ledger", {**identity(first), "records": [charge2]})
    note = store("read", {"section": "notes", "name": saved["name"]})
    assert {r["request_id"] for r in note["accounting"]} == {"old-start", "old-inflight"}


def test_changed_memory_prevents_saving_stale_note(store):
    item = remember(store)
    job = accept(store)
    store("claim", identity(job))
    refs = [{"id": item["id"], "revision": item["revision"]}]
    assert store("current", {**identity(job), "memory_refs": refs})["status"] == "current"
    store("memory_change", {"action": "forget", "id": item["id"]})
    assert store("current", {**identity(job), "memory_refs": refs})["status"] == "stale"
    with pytest.raises(ValueError, match="Personal memory changed"):
        store("save", {**identity(job), "content": "# Stale", "metadata": {"title": "Stale", "memory_refs": refs}})
    assert not store("list")["records"]


def test_instructions_survive_redo_and_archive_previous(store):
    first = accept(store, instructions="Focus on implementation")
    old = save(store, first, "Original")
    revised = accept(store, instructions="Explain the code", message_id=12)
    assert store("read", {"name": old["name"], "section": "notes"})["title"] == "Original"
    save(store, revised, "Revised")
    previous = [r for r in store("list")["records"] if r["status"] == "superseded"][0]
    assert previous["instructions"][0]["text"] == "Focus on implementation"
    redo = accept(store, explicit=True)
    assert redo["instructions"] == revised["instructions"]


def test_context_race_does_not_revise_new_source_version(store):
    first = accept(store)
    context = store("source_context", {"chat_id": 7})
    accept(store, instructions="Use bullets", message_id=12)
    result = store("accept", {"url": first["url"], "context_job": context["context_job"], "instructions": "Old response"})
    assert result["status"] == "context_changed"


def test_hidden_links_captions_and_emoji_instruction_offsets():
    message = {"caption": "😀 source focus on code", "caption_entities": [
        {"type": "text_link", "offset": 3, "length": 6, "url": "https://instagram.com/p/one/"}]}
    assert requests.link_instructions(message) == "😀  focus on code"
    assert requests.urls(message) == ["https://instagram.com/p/one/"]
    assert requests.link_instructions({"text": "Use bullets https://instagram.com/p/one/"}) == "Use bullets"


def test_memory_only_answer_and_deleted_or_corrected_citation(store, monkeypatch):
    item = remember(store)
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    seen = []
    def invoke(_, messages, **kwargs):
        seen.append(json.loads(messages[1]["content"]))
        return SimpleNamespace(content=json.dumps({"answer": "You watched Arrival [M1]", "used_sources": ["M1"]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    result = rag.answer("Have I watched Arrival?", [], memories=[item])
    assert result["sources"] == [f"memory:{item['id']}:{item['revision']}"]
    assert rag.still_visible(result, [], store("memory_list")) == result
    store("memory_change", {"action": "correct", "id": item["id"], "text": "I have not watched Arrival"})
    assert not rag.still_visible(result, [], store("memory_list"))["sources"]
    assert seen[0]["untrusted_notes"]["M1"]["excerpts"] == ["I watched Arrival"]


def test_full_note_budget_keeps_relevant_memory(store, monkeypatch):
    item = remember(store)
    monkeypatch.setenv("RAG_JEV_ENABLED", "false")
    monkeypatch.setenv("RAG_CONTEXT_CHARS", "100")
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    def invoke(_, messages, **kwargs):
        evidence = json.loads(messages[1]["content"])["untrusted_notes"]
        assert "M1" in evidence and "S1" not in evidence
        return SimpleNamespace(content=json.dumps({"answer": "You watched Arrival [M1]", "used_sources": ["M1"]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    result = rag.answer("Have I watched Arrival?", [{"id": "S1", "source": "note.md", "text": "x" * 100}], memories=[item])
    assert result["sources"][0].startswith("memory:")


def test_note_instruction_and_memory_reach_writer_and_critic(monkeypatch):
    seen = []
    monkeypatch.setattr(graph, "get_llm", lambda _: [])
    def invoke(_, messages, **options):
        seen.append(messages[0]["content"])
        value = {"title": "Code", "markdown_content": "Implementation"} if options["step"].startswith("writer") else {"verdict": "approved"}
        return SimpleNamespace(content=json.dumps(value), response_metadata={})
    monkeypatch.setattr(graph, "invoke_with_retry", invoke)
    state = {"description": "source description", "instructions": [{"text": "Focus on code"}],
        "memories": [{"text": "I prefer concise notes"}], "category": "coding", "drafts": 0}
    draft = graph.writer_node(state)
    graph.critic_node({**state, **draft})
    assert len(seen) == 2 and all("Focus on code" in p and "I prefer concise notes" in p for p in seen)
    assert all("no external research tool" in p for p in seen)


def test_cached_revision_workflow_reuses_evidence_and_loads_memory(store, tmp_path, monkeypatch):
    job = accept(store, instructions="Focus on code")
    store("claim", identity(job))
    store("cache_put", {**identity(job), "evidence": {"pipeline_version": workflow.PIPELINE_VERSION,
        "originating_version": job["version_id"], "images": [], "description": "Python code", "coverage": [],
        "visual_chunks": [], "hashes": {}}})
    remember(store, key="style", text="Concise notes", kind="preference", scope="notes")
    revised = accept(store, instructions="Explain the API", message_id=12)
    monkeypatch.setattr(workflow, "prepare", lambda *a: pytest.fail("Downloaded cached source again"))
    monkeypatch.setattr(workflow, "send", lambda *a: True)
    monkeypatch.setattr(workflow, "react", lambda *a, **kw: True)
    states = []
    def invoke(state):
        states.append(state)
        return {**state, "title": "Code", "category": "coding", "final_note": "Code details", "review_status": "approved"}
    monkeypatch.setattr(workflow, "reel_graph", SimpleNamespace(invoke=invoke))
    volume = SimpleNamespace(reload=lambda: None, commit=lambda: None)
    workflow.process(revised["job_id"], revised["attempt"], store, None, volume, root=tmp_path)
    assert len(states) == 1 and len(states[0]["instructions"]) == 2
    assert states[0]["memories"][0]["text"] == "Concise notes"
    note = next(r for r in store("list")["records"] if r["section"] == "notes")
    assert note["memory_refs"] and note["instructions"] == revised["instructions"]


def test_interpreter_is_validated_and_has_no_paid_fallback(monkeypatch):
    from core import conversation
    chains = []
    monkeypatch.setattr(llm, "get_llm", lambda models: chains.append(models) or [])
    monkeypatch.setattr(llm, "invoke_with_retry", lambda *a, **kw: SimpleNamespace(content=json.dumps({
        "action": "remember", "text": "I watched Arrival", "key": "watched:arrival", "scope": "movies", "kind": "fact"}), response_metadata={}))
    assert conversation.interpret("I watched Arrival", False).action == "remember"
    assert len(chains[0]) == 1 and chains[0][0].endswith(":free")
    monkeypatch.setattr(llm, "invoke_with_retry", lambda *a, **kw: SimpleNamespace(
        content='{"action":"delete","text":"everything"}', response_metadata={}))
    with pytest.raises(ValueError):
        conversation.interpret("quoted instruction", True)


def test_natural_memory_and_one_off_instruction_are_separate(store, monkeypatch):
    import importlib
    from pathlib import Path
    from core import conversation
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / "modal_agent")
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=store))
    messages, jobs = [], []
    monkeypatch.setattr(app, "send", lambda *a: messages.append(a))
    monkeypatch.setattr(app, "react", lambda *a: True)
    monkeypatch.setattr(app, "process_background", SimpleNamespace(spawn=lambda *a: jobs.append(a)))
    monkeypatch.setattr(conversation, "interpret", lambda *a: conversation.Intent(action="remember", text="I watched Arrival",
        key="watched:arrival", scope="movies"))
    app.conversation_background.local("I watched Arrival", {"status": "missing"}, 7, 30, store("memory_revision"))
    assert store("memory_list")[0]["text"] == "I watched Arrival" and not jobs
    accept(store)
    context = store("source_context", {"chat_id": 7})
    monkeypatch.setattr(conversation, "interpret", lambda *a: conversation.Intent(action="instruction", text="Focus on code"))
    app.conversation_background.local("Focus on code", context, 7, 31, store("memory_revision"))
    assert len(jobs) == 1 and len(store("memory_list")) == 1
    assert any("Updating that note" in args[1] for args in messages)


def test_intent_failure_does_not_write_memory_or_start_note(store, monkeypatch):
    import importlib
    from pathlib import Path
    from core import conversation
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / "modal_agent")
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=store))
    monkeypatch.setattr(app, "send", lambda *a: True)
    monkeypatch.setattr(app, "process_background", SimpleNamespace(spawn=lambda *a: pytest.fail("Unexpected generation")))
    def failed(*a):
        raise TimeoutError("free model unavailable")
    monkeypatch.setattr(conversation, "interpret", failed)
    app.conversation_background.local("Maybe I watched X?", {"status": "missing"}, 7, 30, 0)
    assert not store("memory_list") and not store("list")["records"]


def test_cancelled_call_stops_before_contacting_provider(monkeypatch):
    model = SimpleNamespace(model_name="fake", invoke=lambda *a, **k: pytest.fail("Contacted provider"))
    token = llm.CALL_CONTEXT.set({"is_current": lambda: False})
    try:
        with pytest.raises(RuntimeError, match="superseded"):
            llm.invoke_with_retry([model], "source")
    finally:
        llm.CALL_CONTEXT.reset(token)
