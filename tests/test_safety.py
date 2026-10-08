import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from core import requests as inputs, storage, accounting, telegram, graph, decisions, llm, rag, media, workflow


class Volume:
    def __init__(self):
        self.commits = 0
    def commit(self):
        self.commits += 1
    def reload(self):
        pass


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "index_note", lambda *args: None)
    return lambda op, payload=None: storage.dispatch(op, payload, root=tmp_path, volume=Volume())


def accepted(store, url="https://instagram.com/p/abc/", **kwargs):
    return store("accept", {"url": url, **kwargs})["job"]


def claim(store, job):
    return store("claim", {"job_id": job["job_id"], "attempt": job["attempt"]})


def save(store, job, title="Same title"):
    return store("save", {"job_id": job["job_id"], "attempt": job["attempt"],
        "content": "# " + title + "\n\nPortuguês: 2 ovos, 150 g farinha.\n",
        "metadata": {"title": title, "category": "culinary", "media": []}})


def test_authorisation_and_exact_commands():
    env = {"TELEGRAM_WEBHOOK_SECRET": "secret", "TELEGRAM_ALLOWED_USER_ID": "7"}
    assert inputs.authorised("secret", {"from": {"id": 7}}, env)
    assert not inputs.authorised("secret", {"from": {"id": 8}}, env)
    assert not inputs.authorised("bad", {"from": {"id": 7}}, env)
    assert not inputs.authorised("secret", {"from": {"id": 7}}, {})
    assert inputs.command("/ask@my_bot pão") == ("ask", "pão")
    assert inputs.command("/asking x")[0] == "asking"
    assert inputs.command("/askish")[0] != "ask"


@pytest.mark.parametrize("url", ["https://evil.com/p/abc/", "http://127.0.0.1/p/abc", "https://instagram.com.evil/p/abc/",
    "https://user@instagram.com/p/abc/", "file:///p/abc", "https://instagram.com/stories/abc/", "https://instagram.com:8080/p/abc/", "https://instagram.com/p/a%2fb/"])
def test_bad_urls(url):
    with pytest.raises(ValueError):
        inputs.canonical_source(url)


def test_utf16_and_canonicalisation():
    text = "😀 https://instagram.com/reels/Ab_c/?utm=1#x"
    url = text.split()[1]
    result = inputs.urls({"text": text, "entities": [{"type": "url", "offset": 3, "length": len(url)}]})
    assert result == [url]
    assert inputs.canonical_source(url) == {"source_id": "Ab_c", "kind": "reel", "url": "https://www.instagram.com/reel/Ab_c/"}


def test_duplicates_update_and_sources(store):
    assert store("update", {"update_id": 1})
    assert not store("update", {"update_id": 1})
    job = accepted(store)
    assert store("accept", {"url": job["url"]})["status"] == "running"
    claim(store, job)
    note = save(store, job)
    duplicate = store("accept", {"url": "https://instagram.com/p/abc/?tracking=yes"})
    assert duplicate["status"] == "active"
    assert duplicate["record"]["name"] == note["name"]


def test_title_collisions_and_late_workers(store):
    first = accepted(store)
    second = accepted(store, "https://instagram.com/p/other/")
    claim(store, first)
    assert claim(store, first)["status"] == "stale"
    claim(store, second)
    assert save(store, first)["name"] != save(store, second)["name"]
    assert save(store, first)["status"] == "stale"


def test_redo_failure_keeps_active(store):
    job = accepted(store)
    claim(store, job)
    old = save(store, job)
    replacement = accepted(store, explicit=True)
    claim(store, replacement)
    store("fail", {"job_id": replacement["job_id"], "attempt": replacement["attempt"], "error": "bad"})
    records = store("list")["records"]
    assert any(r["name"] == old["name"] and r["section"] == "notes" for r in records)
    assert any(r["status"] == "failed" for r in records)


def test_successful_redo_archives_previous(store):
    job = accepted(store)
    claim(store, job)
    store("ledger", {"job_id": job["job_id"], "attempt": job["attempt"], "records": [accounting.record({"id": "paid", "usage": {"cost": .002}}, "writer", 1)]})
    old = save(store, job)
    replacement = accepted(store, explicit=True)
    claim(store, replacement)
    new = save(store, replacement, "Replacement")
    records = store("list")["records"]
    archive = next(r for r in records if r["status"] == "superseded")
    assert archive["accounting"] == old["accounting"]
    assert archive["replacement"] == new["version_id"]
    assert len([r for r in records if r["section"] == "notes"]) == 1
    assert "Português" in store("read", {"section": "failed", "name": archive["name"]})["content"]


def test_index_failure_keeps_note(store, monkeypatch):
    monkeypatch.setattr(rag, "index_note", lambda *args: (_ for _ in ()).throw(ValueError("index unavailable")))
    job = accepted(store)
    claim(store, job)
    assert save(store, job)["index_pending"]
    assert store("accept", {"url": job["url"]})["status"] == "active"


def test_legacy_bootstrap_and_quote_filename(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "Chef's_note.md").write_text("# Receita\n\n**Category:** Culinary\n\n2 ovos\n\n---\n**Generation Info:**\n- **Source URL:** https://instagram.com/p/Legacy/\n", encoding="utf-8")
    result = storage.dispatch("accept", {"url": "https://instagram.com/p/Legacy/?x=1"}, root=tmp_path)
    assert result["status"] == "active"
    assert result["record"]["accounting"] is None
    assert rag.filter_value("Chef's_note.md") == "'Chef''s_note.md'"


def test_atomic_fresh_directories(store, tmp_path):
    store("list")
    assert all((tmp_path / p).is_dir() for p in ("notes", "failed", "attempts", "images"))
    with pytest.raises(ValueError):
        storage.identifier("../file")


def test_reactions_and_delivery(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    payloads = []
    def post(url, json, timeout):
        payloads.append(json)
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"ok": True})
    monkeypatch.setattr(telegram.requests, "post", post)
    assert telegram.react(7, 3)
    assert payloads[-1]["reaction"] == [{"type": "emoji", "emoji": "👀"}]
    assert telegram.react(7, 3, clear=True)
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: SimpleNamespace(ok=False, status_code=400, json=lambda: {"ok": False}))
    assert not telegram.react(7, 3)


def test_evidence_revision_and_review_limit(monkeypatch):
    calls = []
    def invoke(state, prompt, step, output):
        calls.append((step, prompt))
        text = json.dumps({"title": "Receita", "markdown_content": "2 ovos"}) if step == "writer" else json.dumps({"verdict": "revise", "feedback": "include every item"})
        return text, {"model": "fake"}
    monkeypatch.setattr(graph, "invoke", invoke)
    monkeypatch.setattr(graph, "decide", lambda _: {"category": "culinary"})
    initial = {"description": "x" * 1800 + "LAST NAME", "transcript": "[1-2s] pão", "visual_extraction": "1. A\n2. B", "images": [], "drafts": 0, "writer_history": []}
    original = copy.deepcopy(initial)
    final = graph.reel_graph.invoke(initial)
    assert initial == original
    assert final["drafts"] == 2 and final["review_status"] == "needs_review"
    assert all("LAST NAME" in prompt and "1. A" in prompt for _, prompt in calls)
    assert len(calls) == 4


def test_invalid_critic_never_approves(monkeypatch):
    monkeypatch.setattr(graph, "invoke", lambda *a: ("nonsense", {}))
    assert graph.critic_node({"final_note": "Usable draft"})["review_status"] == "unverified"


def test_jev_contract_and_degrade(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "answers": {"category": {"choice": "culinary", "confidence": .9}}, "usage": {"cost": 0}})
    monkeypatch.setattr(decisions.requests, "post", post)
    assert decisions.decide({"caption": "pão"})["category"] == "culinary"
    assert calls[0][0].endswith("/systemone")
    assert calls[0][1]["json"]["model"] == "typesafe/jev-1.13"
    monkeypatch.setattr(decisions.requests, "post", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert decisions.decide({}) == {"category": "general", "routing_degraded": True}
    with pytest.raises(ValueError):
        decisions.Choice(choice="general", confidence=float("nan"))


@pytest.mark.parametrize("status,retry", [(402, False), (401, False), (404, False), (400, False), (500, True), (503, True)])
def test_retry_eligibility(status, retry):
    exc = Exception("error")
    exc.status_code = status
    assert llm.is_retryable(exc) is retry


def test_retry_candidates_and_deadline(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    calls = []
    def client(name, status):
        def invoke(_, **options):
            calls.append(name)
            exc = Exception("error")
            exc.status_code = status
            raise exc
        return SimpleNamespace(model_name=name, invoke=invoke)
    with pytest.raises(llm.ProviderChainError):
        llm.invoke_with_retry([client("permanent", 402), client("transient", 503)], "x")
    assert calls == ["permanent", "transient", "transient"]
    with pytest.raises(TimeoutError):
        llm.invoke_with_retry([client("never", 500)], "x", deadline=time.monotonic() - 1)
    assert "never" not in calls


def test_content_blocks_truncation():
    assert llm.response_text(SimpleNamespace(content=[{"type": "text", "text": "pão"}], response_metadata={})) == "pão"
    with pytest.raises(ValueError):
        llm.response_text(SimpleNamespace(content="partial", response_metadata={"finish_reason": "length"}))


def test_accounting_unknown_reuse_dedup():
    known = accounting.record({"id": "one", "usage": {"cost": .000004, "upstream_inference_cost": 99}}, "writer", 1)
    assert known["cost"] == .000004
    assert len(accounting.deduplicate([known, known])) == 1
    unknown = accounting.record({}, "timeout", 1, False)
    assert unknown["cost"] is None and unknown["cost_status"] == "unknown"
    assert "incomplete" in accounting.summary([known, unknown])
    assert accounting.summary(None) == "Cost not recorded"


def test_multiple_chunks_and_unrelated():
    hits = [{"source": "a", "text": "2 ovos", "_distance": .1}, {"source": "a", "text": "150 g farinha", "_distance": .2},
        {"source": "b", "text": "unrelated", "_distance": .9}, {"source": "deleted", "text": "stale", "_distance": .1}]
    kept = rag.select_hits(hits, {"a", "b"})
    assert len(kept) == 2 and {r["id"] for r in kept} == {"S1"}
    assert rag.select_hits(hits, set()) == []


def test_invalid_citations_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "get_llm", lambda _: [])
    def invoke(*a, **k):
        calls.append(k)
        return SimpleNamespace(content=json.dumps({"answer": "Invented [S99]", "used_sources": ["S99"]}), response_metadata={})
    monkeypatch.setattr(rag, "invoke_with_retry", invoke)
    result = rag.answer("quantos ovos?", [{"id": "S1", "source": "a", "text": "2 ovos"}])
    assert not result["sources"] and len(calls) == 2


def test_internal_sections_excluded():
    assert "secret" not in rag._note_body("# Food\n2 ovos\n\n---\n\n**Generation Info:**\nsecret")
    assert "charge" not in rag._note_body("# Food\n2 ovos\n**Cost details:**\ncharge")


def test_full_duration_and_numeric_order():
    assert media.frame_times(100, 4) == [12.5, 37.5, 62.5, 87.5]
    assert [p.name for p in media.numerical([Path("cand_1000.jpg"), Path("cand_999.jpg")])] == ["cand_999.jpg", "cand_1000.jpg"]


@pytest.mark.parametrize("audio", [False, True])
def test_silent_and_carousel_audio(tmp_path, monkeypatch, audio):
    fake_path = tmp_path / "clip.mp4"
    fake_path.write_bytes(b"video")
    monkeypatch.setattr(media, "download_video", lambda *a: (fake_path, {"description": "Receita"}))
    monkeypatch.setattr(media, "probe", lambda _: (20, audio))
    monkeypatch.setattr(media, "frames", lambda *a: [str(tmp_path / "frame.jpg")])
    calls = []
    def transcribe(path):
        calls.append(path)
        return {"segments": [{"start": 0, "end": 1, "text": "2 ovos"}]}
    result = media.prepare({"kind": "reel", "url": "fake"}, tmp_path / "attempt", transcribe, Volume())
    assert bool(calls) is audio
    assert ("Media item 1" in result["transcript"]) is audio
    assert not fake_path.exists()


def test_notification_failure_preserves_saved(store, tmp_path, monkeypatch):
    job = accepted(store)
    monkeypatch.setattr(workflow, "prepare", lambda *a: {"description": "Receita", "transcript": "", "images": [], "coverage": []})
    monkeypatch.setattr(workflow, "visual_extract", lambda *a: ("", [], []))
    monkeypatch.setattr(workflow.reel_graph, "invoke", lambda _: {"title": "Receita", "category": "culinary", "final_note": "2 ovos", "review_status": "approved"})
    monkeypatch.setattr(workflow, "send", lambda *a: False)
    monkeypatch.setattr(workflow, "react", lambda *a, **k: False)
    workflow.process(job["job_id"], job["attempt"], store, lambda _: {}, Volume(), str(tmp_path))
    records = store("list")["records"]
    assert len(records) == 1 and records[0]["status"] == "saved"
    state = json.loads((tmp_path / "private/registry.json").read_text())
    assert state["jobs"][job["job_id"]]["notification_pending"]


def test_jev_status_before_json_and_invalid_probabilities(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    def fail():
        raise ValueError("HTTP unavailable")
    monkeypatch.setattr(decisions.requests, "post", lambda *a, **k: SimpleNamespace(raise_for_status=fail,
        json=lambda: pytest.fail("JSON must not be parsed before a successful status")))
    assert decisions.decide({})["routing_degraded"]
    with pytest.raises(ValueError):
        decisions.Choice(choice="general", confidence=.8, probabilities={"general": float("inf")})


def test_gpu_failure_still_extracts_frames(tmp_path, monkeypatch):
    fake = tmp_path / "video.mp4"
    fake.write_bytes(b"fake")
    monkeypatch.setattr(media, "download_video", lambda *a: (fake, {"description": "caption"}))
    monkeypatch.setattr(media, "probe", lambda _: (10, True))
    monkeypatch.setattr(media, "frames", lambda *a: ["useful-frame.jpg"])
    result = media.prepare({"kind": "reel", "url": "fake"}, tmp_path / "attempt", lambda _: (_ for _ in ()).throw(TimeoutError("GPU failed")), Volume())
    assert result["images"] and result["coverage"] and result["diagnostics"]


def test_carousel_transcribes_video_item(tmp_path, monkeypatch):
    import sys
    class Downloader:
        def __init__(self, *a):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def extract_info(self, *a, **k):
            return {"entries": [{"url": "image.jpg", "vcodec": "none"}, {"url": "video.mp4", "vcodec": "h264"}]}
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Downloader))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(media, "download_image", lambda *a: "image.jpg")
    monkeypatch.setattr(media, "download_video", lambda *a: (video, {}))
    monkeypatch.setattr(media, "probe", lambda _: (8, True))
    monkeypatch.setattr(media, "frames", lambda *a: ["frame.jpg"])
    calls = []
    def transcribe(path):
        calls.append(path)
        return {"segments": [{"start": 0, "end": 2, "text": "150 g farinha"}]}
    result = media.prepare({"kind": "post", "url": "fake"}, tmp_path / "attempt", transcribe, Volume())
    assert len(calls) == 1 and "Media item 2" in result["transcript"]
    assert result["image_labels"][0]["media_item"] == 1


def test_media_bounds_and_cleanup(tmp_path, monkeypatch):
    options = media.download_options(tmp_path)
    with pytest.raises(ValueError):
        options["progress_hooks"][0]({"downloaded_bytes": media.MAX_BYTES + 1})
    assert options["match_filter"]({"duration": media.MAX_DURATION + 1})
    seen = []
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: seen.append(k) or SimpleNamespace(stdout=b""))
    media.run(["ffmpeg"])
    assert seen[0]["timeout"] == media.FFMPEG_TIMEOUT
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    (scratch / "fragment.part").write_bytes(b"tmp")
    monkeypatch.setattr(media, "_prepare", lambda *a: (_ for _ in ()).throw(ValueError("failed")))
    with pytest.raises(ValueError):
        media.prepare({}, scratch, None, Volume())
    assert not (scratch / "fragment.part").exists()


def test_compute_estimates_free_and_unknown(monkeypatch):
    monkeypatch.setenv("MODAL_CPU_USD_PER_SECOND", ".00001")
    monkeypatch.setenv("MODAL_GPU_USD_PER_SECOND", ".0001")
    monkeypatch.delenv("MODAL_STORAGE_USD_PER_SECOND", raising=False)
    computed = accounting.compute_estimate(10, 2)
    assert computed["estimate"] == pytest.approx(.0003)
    free = accounting.record({"usage": {"cost": 0}}, "writer", 1)
    assert free["cost_status"] == "reported"
    assert "Estimated total" in accounting.summary([free], computed)


def test_changed_source_cannot_be_cited():
    result = {"answer": "2 eggs [S1]", "sources": ["deleted.md"]}
    assert not rag.still_visible(result, [{"name": "deleted.md", "section": "failed"}])["sources"]


def test_successful_visual_chunks_reused(monkeypatch):
    calls = []
    monkeypatch.setattr(workflow, "get_llm", lambda _: [])
    monkeypatch.setattr(workflow, "build_vision_messages", lambda prompt, images: prompt)
    def invoke(*a, **k):
        calls.append(1)
        return SimpleNamespace(content="Recovered missing titles", response_metadata={})
    monkeypatch.setattr(workflow, "invoke_with_retry", invoke)
    text, partial, chunks = workflow.visual_extract(["fake"] * 8, cached_chunks=[
        {"start": 0, "complete": True, "text": "Original first four images", "originating_version": "original"},
        {"start": 4, "complete": False, "text": ""}])
    assert len(calls) == 1 and not partial
    assert "Original first four images" in text and "Recovered missing titles" in text
    assert chunks[0]["originating_version"] == "original"
