import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from core import storage, rag, llm, accounting


def store(root):
    return lambda op, payload=None: storage.dispatch(op, payload, root=root)


def accept_claim(s, explicit=False):
    job = s("accept", {"url": "https://instagram.com/p/Recovery/", "explicit": explicit})["job"]
    s("claim", {"job_id": job["job_id"], "attempt": job["attempt"]})
    return {"job_id": job["job_id"], "attempt": job["attempt"]}


@pytest.mark.parametrize("interrupt", ["new", "archive", "pointer"])
def test_redo_interruption_never_loses_both(tmp_path, monkeypatch, interrupt):
    monkeypatch.setattr(rag, "index_note", lambda *a: None)
    s = store(tmp_path)
    identity = accept_claim(s)
    old = s("save", {**identity, "content": "# Old\nRetained evidence", "metadata": {"title": "Old"}})
    identity = accept_claim(s, True)
    original = storage.atomic
    def crash(path, value):
        name = Path(path).name
        fail = (interrupt == "new" and name.startswith("note--")) or (interrupt == "archive" and name.startswith("previous--")) or (interrupt == "pointer" and name == "registry.json")
        if fail:
            raise OSError("Simulated crash")
        return original(path, value)
    monkeypatch.setattr(storage, "atomic", crash)
    with pytest.raises(OSError):
        s("save", {**identity, "content": "# New\nReplacement", "metadata": {"title": "New"}})
    monkeypatch.setattr(storage, "atomic", original)
    reopened = store(tmp_path)("list")["records"]
    active = [r for r in reopened if r["section"] == "notes"]
    assert len(active) == 1
    assert active[0]["name"] == old["name"]
    assert (tmp_path / "notes" / old["name"]).exists()


def test_expired_claim_explicit_retry_and_stale_worker(tmp_path, monkeypatch):
    s = store(tmp_path)
    first = accept_claim(s)
    registry = tmp_path / "private/registry.json"
    state = json.loads(registry.read_text())
    state["jobs"][first["job_id"]]["claimed"] = 0
    registry.write_text(json.dumps(state))
    assert s("accept", {"url": "https://instagram.com/p/Recovery/"})["status"] == "running"
    second = accept_claim(s, True)
    assert second != first
    assert s("save", {**first, "content": "late", "metadata": {"title": "Late"}})["status"] == "stale"


def test_interrupted_reindex_keeps_pointer(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "VECTOR_DIR", str(tmp_path / "vectors"))
    monkeypatch.setattr(rag, "NOTES_DIR", str(tmp_path / "notes"))
    Path(rag.VECTOR_DIR).mkdir()
    Path(rag.NOTES_DIR).mkdir()
    (Path(rag.VECTOR_DIR) / "active.json").write_text('{"table":"working"}')
    source = Path(rag.NOTES_DIR) / "note.md"
    source.write_text("# Receita\n2 ovos")
    monkeypatch.setattr(rag, "_embed_passages", lambda _: [[0] * 384])
    monkeypatch.setitem(sys.modules, "lancedb", SimpleNamespace(connect=lambda _: None))
    monkeypatch.setattr(rag, "_table", lambda *a: SimpleNamespace(add=lambda _: (_ for _ in ()).throw(RuntimeError("interrupted"))))
    with pytest.raises(RuntimeError):
        rag.reindex_all_notes(None, [source])
    assert rag.active_table() == "working"


def test_provider_charge_survives_langchain(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    model = llm._chat_model("fake")
    raw = {"id": "gen-123", "model": "served", "provider": "actual", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": .00001}}
    response = model._create_chat_result(raw).generations[0].message
    assert response.response_metadata["accounting"]["usage"]["cost"] == .00001


def test_cached_evidence_and_ledger_replay(tmp_path):
    s = store(tmp_path)
    identity = accept_claim(s)
    original = accounting.record({"id": "same", "usage": {"cost": .01}}, "extract", 1)
    s("ledger", {**identity, "records": [original]})
    s("ledger", {**identity, "records": [original]})
    evidence = {"pipeline_version": "v2", "originating_version": "original", "visual_extraction": "all 17 titles"}
    s("cache_put", {**identity, "evidence": evidence})
    assert s("cache_get", {"source_id": "Recovery"}) == evidence
    registry = json.loads((tmp_path / "private/registry.json").read_text())
    assert len(registry["jobs"][identity["job_id"]]["accounting"]) == 1


def test_expired_job_visible_for_retry(tmp_path):
    s = store(tmp_path)
    identity = accept_claim(s)
    registry = tmp_path / "private/registry.json"
    data = json.loads(registry.read_text())
    data["jobs"][identity["job_id"]]["claimed"] = 0
    registry.write_text(json.dumps(data))
    record = s("list")["records"][0]
    assert record["section"] == "failed" and record["status"] == "failed"
    assert s("accept", {"url": record["url"], "explicit": True})["status"] == "accepted"


def test_real_sdk_mock_transport_preserves_charge_and_timeout():
    import httpx
    import time
    seen, charges = [], []
    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "gen-mocked", "object": "chat.completion", "created": 1,
            "model": "actually-served", "provider": "provider", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "complete"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6, "cost": .00002}})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = llm.AccountingChatOpenAI(model="configured", api_key="fake", base_url="https://example.invalid/v1",
            http_client=client, max_retries=0, timeout=45)
        token = llm.CALL_CONTEXT.set({"record": charges.append})
        try:
            result = llm.invoke_with_retry([model], "evidence", output=256, deadline=time.monotonic() + 2)
        finally:
            llm.CALL_CONTEXT.reset(token)
    assert llm.response_text(result) == "complete" and len(seen) == 1
    assert charges[0]["model"] == "actually-served" and charges[0]["cost"] == .00002
    assert seen[0].extensions["timeout"]["read"] <= 2
    body = json.loads(seen[0].content)
    assert body.get("max_tokens", body.get("max_completion_tokens")) == 256


def test_retry_uses_persisted_source_kind(tmp_path):
    s = store(tmp_path)
    identity = accept_claim(s)
    failed = s("fail", {**identity, "metadata": {"kind": "reel"}, "error": "failed"})
    result = s("accept", {"url": failed["url"], "explicit": True, "previous": "failed/" + failed["name"]})
    assert result["job"]["kind"] == "reel"
