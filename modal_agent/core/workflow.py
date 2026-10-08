"""CPU orchestration. External work never executes inside shared storage."""
import os
import time
import hashlib
from pathlib import Path
from core.accounting import record, compute_estimate
from core.config import VISION_CONTENT_MODELS
from core.errors import classify, redact, user_message
from core.graph import reel_graph
from core.llm import CALL_CONTEXT, build_vision_messages, get_llm, invoke_with_retry, response_text
from core.media import prepare
from core.telegram import send, react

PIPELINE_VERSION = "evidence-v2"


def visual_extract(images, labels=None, cached_chunks=None):
    summaries, coverage, chunks = [], [], []
    cached_chunks = {c["start"]: c for c in (cached_chunks or []) if c.get("complete")}
    for start in range(0, len(images), 4):
        if start in cached_chunks:
            chunk = cached_chunks[start]
            summaries.append(chunk["text"])
            chunks.append(chunk)
            continue
        prompt = ("Extract every visible name, title, number, amount, URL, ingredient, code and list item verbatim. "
            "Do not merge or omit items. Identify each image by its media ID. Describe useful non-text subjects briefly. "
            "Mark uncertainty and unreadable text explicitly. Media IDs: " +
            ", ".join(str(i + 1) for i in range(start, min(start + 4, len(images)))))
        if labels:
            prompt += "\nMedia provenance: " + str(labels[start:start + 4])
        try:
            response = invoke_with_retry(get_llm(VISION_CONTENT_MODELS), build_vision_messages(prompt, images[start:start + 4]),
                step="visual_extraction", output=3000)
            text = f"Media {start + 1}-{min(start + 4, len(images))}:\n" + response_text(response)
            summaries.append(text)
            chunks.append({"start": start, "complete": True, "text": text})
        except Exception as exc:
            coverage.append(f"Visual text for media {start + 1}-{min(start + 4, len(images))} is incomplete ({type(exc).__name__}).")
            chunks.append({"start": start, "complete": False, "text": "", "diagnostic": redact(exc)})
    return "\n\n".join(summaries), coverage, chunks


def process(job_id, attempt, storage, transcribe, volume, root="/data"):
    identity = {"job_id": job_id, "attempt": attempt}
    claimed = storage("claim", identity)
    if claimed.get("status") != "claimed":
        return
    job = claimed["job"]
    started = time.monotonic()
    records = []
    def charge(item):
        item.update(version_id=job["version_id"], attempt=attempt)
        records.append(item)
        try:
            storage("ledger", {**identity, "records": [item]})
        except Exception:
            pass  # keep the local record; retry persistence with the final outcome
    deadline = time.monotonic() + min(360, float(os.environ.get("GENERATION_DEADLINE_SECONDS", "300")))
    context = CALL_CONTEXT.set({"deadline": deadline, "record": charge})
    stage, evidence, final, reused_evidence = "download", {}, {}, False
    saved = None
    try:
        cached = storage("cache_get", {"source_id": job["source_id"]})
        volume.reload()
        cache_valid = cached and cached.get("pipeline_version") == PIPELINE_VERSION and all(Path(p).is_file() and
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == cached.get("hashes", {}).get(p) for p in cached.get("images", []))
        if cache_valid and not cached.get("media_coverage"):
            evidence = cached
            reused_evidence = True
            reused = record({}, "cached_evidence", 0, True, originating_version=cached["originating_version"])
            reused.update(cost_status="reused", cost=0)
            charge(reused)
        else:
            evidence = prepare(job, Path(root) / "attempts" / attempt, transcribe, volume)
            evidence["hashes"] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in evidence["images"]}
            evidence["media_coverage"] = list(evidence["coverage"])
            evidence.update(pipeline_version=PIPELINE_VERSION, originating_version=job["version_id"])
            if cache_valid:
                reusable = []
                for chunk in cached.get("visual_chunks", []):
                    start = chunk["start"]
                    old_hashes = [cached["hashes"][p] for p in cached["images"][start:start + 4]]
                    new_hashes = [evidence["hashes"][p] for p in evidence["images"][start:start + 4]]
                    if chunk.get("complete") and old_hashes == new_hashes:
                        reusable.append(chunk)
                evidence["visual_chunks"] = reusable
                if reusable:
                    reused = record({}, "cached_visual_chunks", 0, True, originating_version=cached["originating_version"])
                    reused.update(cost_status="reused", cost=0)
                    charge(reused)
        # Reserve time for storage/notifications below the worker's 600s limit.
        active_context = CALL_CONTEXT.get()
        active_context["deadline"] = min(started + 540, time.monotonic() +
            min(360, float(os.environ.get("GENERATION_DEADLINE_SECONDS", "300"))))
        stage = "media"
        extraction, partial, chunks = visual_extract(evidence["images"], evidence.get("image_labels"), evidence.get("visual_chunks"))
        for chunk in chunks:
            chunk.setdefault("originating_version", job["version_id"])
        evidence.update(visual_extraction=extraction, visual_chunks=chunks)
        evidence["coverage"] = list(evidence.get("media_coverage", evidence.get("coverage", []))) + partial
        # Successful chunks survive failures elsewhere. Retry requests only missing chunks.
        storage("cache_put", {**identity, "evidence": evidence})
        stage = "generation"
        initial = {"url": job["url"], "video_path": "reel" if job["kind"] == "reel" else "",
            **{k: evidence.get(k, "" if k != "images" and k != "coverage" else []) for k in
                ("description", "transcript", "visual_extraction", "images", "coverage")}, "drafts": 0,
            "writer_history": [], "critique_history": [], "model_log": []}
        if not evidence.get("visual_extraction") and evidence.get("images"):
            initial["raw_media_ids"] = list(range(1, min(4, len(evidence["images"])) + 1))
        final = reel_graph.invoke(initial)
        content = f"# {final['title']}\n\n**Category:** {final['category'].title()}\n\n{final['final_note']}\n\n---\n**Generation Info:**\n- **Source URL:** {job['url']}\n"
        metadata = {"title": final["title"], "category": final["category"], "creator": evidence.get("creator", ""),
            "creator_url": "https://www.instagram.com/" + evidence.get("username", "").lstrip("@") if evidence.get("username") else "",
            "media": evidence.get("images", []), "generation": {key: final[key] for key in
                ("description", "transcript", "visual_extraction", "writer_history", "critique_history", "model_log", "routing_degraded") if key in final},
            "review_status": final["review_status"], "coverage": evidence.get("coverage", []),
            "compute": compute_estimate(time.monotonic() - started,
                0 if reused_evidence else (None if evidence.get("gpu_measurement_incomplete") else evidence.get("gpu_seconds", 0)))}
        stage = "storage"
        saved = storage("save", {**identity, "content": content, "metadata": metadata, "records": records})
        if saved.get("status") == "stale":
            return
    except Exception as exc:
        evidence = getattr(exc, "partial_evidence", evidence)
        kind = classify(exc, stage)
        try:
            failed = storage("fail", {**identity, "error": redact(exc), "error_kind": kind, "records": records,
                "metadata": {"media": evidence.get("images", []), "generation": final, "evidence": evidence,
                    "compute": compute_estimate(time.monotonic() - started,
                        0 if reused_evidence else (None if evidence.get("gpu_measurement_incomplete") else evidence.get("gpu_seconds", 0)))},
                "content": f"# Unfinished note\n\n{final.get('final_note', 'Kept for retry.')}\n"})
            if failed.get("status") == "stale":
                return
        except Exception:
            # Never claim a failure record exists if storage itself failed.
            kind = "storage"
        delivery = send(job.get("chat_id"), user_message(kind))
        storage("notify", {**identity, "success": delivery})
    finally:
        CALL_CONTEXT.reset(context)
        react(job.get("chat_id"), job.get("message_id"), clear=True)
    if saved:
        text = f"Saved: {saved['title']}"
        if saved.get("index_pending"):
            text += "\nI saved the note, but it isn't searchable yet."
        url = os.environ.get("UI_URL")
        if url:
            from urllib.parse import urlencode
            text += "\n" + url.rstrip("/") + "/?" + urlencode({"note": saved["name"]})
        delivery = send(job.get("chat_id"), text)
        storage("notify", {**identity, "success": delivery})
