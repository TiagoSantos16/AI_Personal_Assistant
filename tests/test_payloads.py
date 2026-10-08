"""Offline complete-workflow comparison against the reviewed Git baseline."""
import json
import subprocess
from types import SimpleNamespace
from core import graph, llm, decisions, workflow, state

BASELINE = "61fbaaac66a1b458bf61e2133649175f9c2db6fe"


def measure(payloads):
    text, images = 0, 0
    for content in payloads:
        if isinstance(content, str):
            text += len(content)
        elif isinstance(content, dict):
            text += len(json.dumps(content, ensure_ascii=False))
        else:
            for message in content:
                value = message["content"]
                if isinstance(value, str):
                    text += len(value)
                else:
                    for block in value:
                        text += len(block.get("text", ""))
                        images += block.get("type") == "image_url"
    return {"calls": len(payloads), "text_characters": text, "image_attachments": images}


def baseline(kind, images, evidence, monkeypatch, tmp_path):
    payloads, reviews = [], []
    def invoke(_, content, **options):
        payloads.append(content)
        flat = str(content)
        if "Name a personal note" in flat:
            value = "Receita"
        elif "Analyze the transcript" in flat:
            value = "culinary"
        elif "completeness critic" in flat:
            value = "OK"
        elif "meticulous editor" in flat:
            reviews.append(1)
            value = "VERDICT revise\nKeep all names" if len(reviews) == 1 else "VERDICT approve"
        elif "List every visible" in flat:
            value = "2 ovos\n150 g farinha\nLeite"
        else:
            value = "2 ovos, 150 g farinha, leite"
        return SimpleNamespace(content=value, response_metadata={"model_name": "fake"})
    monkeypatch.setattr(llm, "invoke_with_retry", invoke)
    monkeypatch.setattr(llm, "get_llm", lambda _: [])
    original_state = subprocess.check_output(["git", "show", BASELINE + ":modal_agent/core/state.py"], text=True, encoding="utf-8")
    state_namespace = {}
    exec(compile(original_state, "baseline_state.py", "exec"), state_namespace)
    monkeypatch.setattr(state, "AgentState", state_namespace["AgentState"])
    original_graph = subprocess.check_output(["git", "show", BASELINE + ":modal_agent/core/graph.py"], text=True, encoding="utf-8")
    namespace = {}
    exec(compile(original_graph, "baseline_graph.py", "exec"), namespace)
    monkeypatch.setattr(graph, "reel_graph", namespace["reel_graph"])
    original_processor = subprocess.check_output(["git", "show", BASELINE + ":modal_agent/core/processor.py"], text=True, encoding="utf-8")
    processor = {}
    exec(compile(original_processor, "baseline_processor.py", "exec"), processor)
    processor["save_note"] = lambda *a: "fake.md"
    processor["send_telegram_message"] = lambda *a: None
    processor["FAILED_DIR"] = str(tmp_path / "failed")
    processor["NOTES_DIR"] = str(tmp_path / "notes")
    if kind == "post":
        processor["download_post"] = lambda _: (images, {"description": evidence["description"]})
        processor["process_post"]("https://instagram.com/p/fake/", None, None)
    else:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        processor["download_video"] = lambda _: (str(video), {"description": evidence["description"]})
        processor["transcribe_with_timestamps"] = lambda _: {"text": evidence["transcript"], "segments": [{"start": 0, "end": 12, "text": evidence["transcript"]}]}
        processor["extract_frames_for_state"] = lambda *a: (images, True)
        processor["process_reel"]("https://instagram.com/reel/fake/", None, None)
    return measure(payloads)


def current(images, evidence, monkeypatch):
    payloads, reviews = [], []
    def invoke(_, content, **options):
        payloads.append(content)
        if options["step"] == "visual_extraction":
            value = "2 ovos\n150 g farinha\nLeite"
        elif options["step"] == "critic":
            reviews.append(1)
            value = json.dumps({"verdict": "revise" if len(reviews) == 1 else "approved", "feedback": "Keep all names"})
        else:
            value = json.dumps({"title": "Receita", "markdown_content": "2 ovos, 150 g farinha, leite"})
        return SimpleNamespace(content=value, response_metadata={"model_name": "fake"})
    monkeypatch.setattr(workflow, "invoke_with_retry", invoke)
    monkeypatch.setattr(graph, "invoke_with_retry", invoke)
    monkeypatch.setattr(workflow, "get_llm", lambda _: [])
    monkeypatch.setattr(graph, "get_llm", lambda _: [])
    def decision_post(url, **kwargs):
        payloads.append(kwargs["json"])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"answers": {"category": {"choice": "culinary", "confidence": .9}}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setattr(decisions.requests, "post", decision_post)
    extraction, _, _ = workflow.visual_extract(images)
    graph.build_graph().invoke({**evidence, "visual_extraction": extraction, "images": images, "drafts": 0})
    return measure(payloads)


def test_complete_payload_comparison(monkeypatch, tmp_path):
    evidence = {"description": "Receita portuguesa: ingredientes e quantidades.", "transcript": "[0-12s] " + "2 ovos, 150 g farinha e leite. " * 30}
    results = {}
    for kind, count in (("reel", 20), ("post", 4)):
        source_evidence = evidence if kind == "reel" else {**evidence, "transcript": ""}
        images = []
        for index in range(count):
            path = tmp_path / f"{kind}_{index}.jpg"
            path.write_bytes(b"fake-image")
            images.append(str(path))
        with monkeypatch.context() as patch:
            before = baseline(kind, images, source_evidence, patch, tmp_path)
        with monkeypatch.context() as patch:
            after = current(images, source_evidence, patch)
        results[kind] = {"baseline": before, "current": after}
        assert after["image_attachments"] < before["image_attachments"] if kind == "reel" else after["calls"] < before["calls"]
    print("OFFLINE_PAYLOAD_COMPARISON=" + json.dumps(results, sort_keys=True))
