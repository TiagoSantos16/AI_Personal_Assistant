"""All shared mutations run inside the single-input Modal storage function.

The registry is an atomic visibility pointer. Files are committed before a pointer
switch; interrupted operations leave recoverable extra files, never lost notes.
"""
import json
import os
import re
import time
import uuid
from pathlib import Path

from core.accounting import deduplicate
from core.errors import redact
from core.requests import canonical_source


def identifier(value):
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in '/\\\x00:'):
        raise ValueError("Invalid filesystem identifier")
    return value


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def legacy(path, section):
    content = path.read_text(encoding="utf-8")
    def field(label):
        match = re.search(r"\*\*" + label + r":\*\*\s*([^\n]+)", content)
        return match[1].strip() if match else ""
    match = re.search(r"^#\s+(.+)", content, re.M)
    try:
        source = canonical_source(field("(?:Source URL|URL|Source)"))
    except ValueError:
        source = {"source_id": "legacy-" + uuid.uuid5(uuid.NAMESPACE_URL, path.name).hex, "url": "", "kind": "reel"}
    return {**source, "name": path.name, "title": match[1].removeprefix("FAILED: ") if match else path.stem,
            "category": field("Category") or path.stem.split("_")[0], "creator": field("(?:Creator|Uploader|User)"),
            "creator_url": field("Creator URL"), "saved": path.stat().st_mtime,
            "status": "saved" if section == "notes" else "failed", "section": section,
            "version_id": uuid.uuid5(uuid.NAMESPACE_URL, section + path.name).hex,
            "media": re.findall(r"`([^`]+\.jpg)`", content), "accounting": None, "legacy": True}


def dispatch(operation, payload=None, *, root="/data", volume=None):
    payload = payload or {}
    root = Path(root)
    for directory in ("notes", "failed", "images", "attempts", "private", "evidence"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    registry = root / "private/registry.json"
    if operation == "revision":
        return registry.stat().st_mtime_ns if registry.exists() else 0
    state = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {
        "records": {}, "sources": {}, "jobs": {}, "updates": {}, "revision": 0}
    state.setdefault("retired", [])
    if "section" in payload and payload["section"] not in {"notes", "failed"}:
        raise ValueError("Invalid record section")
    def commit():
        if volume:
            volume.commit()
    def persist():
        state["revision"] += 1
        atomic(registry, state)
        commit()
    # Bootstrap only unknown legacy files. New files become visible only through
    # the registry switch, even if an earlier worker stopped mid-save.
    known = {(r["section"], r["name"]) for r in state["records"].values()}
    changed = False
    for section in ("notes", "failed"):
        for path in (root / section).glob("*.md"):
            if (section, path.name) in known or section + "/" + path.name in state["retired"] or "--" in path.stem:
                continue
            item = legacy(path, section)
            key = section + "/" + path.name
            state["records"][key] = item
            source = state["sources"].setdefault(item["source_id"], {})
            source["active" if section == "notes" else "failed"] = key
            changed = True
    if changed:
        persist()
    if operation == "list":
        lease = max(660, float(os.environ.get("JOB_LEASE_SECONDS", "900")))
        for job in state["jobs"].values():
            if job["status"] in {"accepted", "running"} and time.time() - job.get("claimed", job["created"]) >= lease:
                name = "failure--" + job["version_id"] + ".md"
                item = {**job, "name": name, "section": "failed", "status": "failed", "title": "Interrupted processing",
                    "error_kind": "storage", "diagnostic": "Worker lease expired. Explicit retry is required.", "media": []}
                atomic(root / "failed" / name, "# Interrupted processing\n\nKept for explicit retry.\n")
                commit()
                state["records"]["failed/" + name] = item
                state["sources"][job["source_id"]]["failed"] = "failed/" + name
                job["status"] = "failed"
                persist()
        return {"revision": state["revision"], "records": list(state["records"].values())}
    if operation == "read":
        key = payload["section"] + "/" + identifier(payload["name"])
        item = state["records"][key]
        return {**item, "content": (root / key).read_text(encoding="utf-8")}
    if operation == "media":
        key = payload["section"] + "/" + identifier(payload["name"])
        result = []
        for value in state["records"][key].get("media", []):
            path = Path(value)
            if not path.is_absolute():
                path = root / "images" / identifier(value)
            if path.resolve().is_relative_to(root.resolve()) and path.is_file():
                result.append(path.read_bytes())
        return result
    if operation == "search":
        matches = []
        for entry in payload["records"]:
            key = entry["section"] + "/" + identifier(entry["name"])
            if key in state["records"] and payload["query"].lower() in (root / key).read_text(encoding="utf-8").lower():
                matches.append(state["records"][key])
        return matches
    if operation == "update":
        key = str(payload["update_id"])
        if key in state["updates"]:
            return False
        state["updates"][key] = {"accepted": time.time()}
        persist()
        return True
    if operation == "accept":
        source = canonical_source(payload["url"])
        lookup = state["sources"].setdefault(source["source_id"], {})
        previous = payload.get("previous")
        job = state["jobs"].get(lookup.get("job"))
        lease = max(660, float(os.environ.get("JOB_LEASE_SECONDS", "900")))
        if job and job["status"] in {"accepted", "running"}:
            if not payload.get("explicit") or time.time() - job.get("claimed", job["created"]) < lease:
                return {"status": "running"}
        if not payload.get("explicit"):
            for field in ("active", "failed"):
                if lookup.get(field) in state["records"]:
                    return {"status": field, "record": state["records"][lookup[field]]}
        if previous and previous not in state["records"]:
            raise ValueError("Record no longer available")
        if previous:
            previous_record = state["records"][previous]
            if previous_record["source_id"] != source["source_id"]:
                raise ValueError("Retry source does not match the record")
            source["kind"] = previous_record["kind"]
        job_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        job = {**source, "job_id": job_id, "attempt": token, "version_id": uuid.uuid4().hex,
               "created": time.time(), "status": "accepted", "previous": lookup.get("active"),
               "chat_id": payload.get("chat_id"), "message_id": payload.get("message_id"), "accounting": []}
        state["jobs"][job_id] = job
        lookup["job"] = job_id
        if "update_id" in payload:
            state["updates"][str(payload["update_id"])] = {"accepted": time.time(), "job_id": job_id}
        persist()
        return {"status": "accepted", "job": job}
    if operation in {"claim", "save", "fail", "enqueue_failed", "notify", "ledger", "cache_put"}:
        job = state["jobs"][identifier(payload["job_id"])]
        if payload.get("attempt") != job["attempt"] or state["sources"][job["source_id"]].get("job") != job["job_id"]:
            return {"status": "stale"}
        if operation == "claim":
            if job["status"] != "accepted":
                return {"status": "stale"}
            job.update(status="running", claimed=time.time())
            persist()
            return {"status": "claimed", "job": job}
        if operation == "ledger":
            job["accounting"] = deduplicate(job["accounting"] + payload["records"])
            persist()
            return True
        if operation == "notify":
            job["notification_pending"] = not payload["success"]
            persist()
            return True
        if operation == "cache_put":
            if job["status"] != "running":
                return {"status": "stale"}
            atomic(root / "evidence" / (identifier(job["source_id"]) + ".json"), payload["evidence"])
            commit()
            return True
        if job["status"] not in {"accepted", "running"}:
            return {"status": "stale"}
        job["accounting"] = deduplicate(job["accounting"] + payload.get("records", []))
        if operation in {"fail", "enqueue_failed"}:
            item = {**job, **payload.get("metadata", {}), "status": "failed", "section": "failed",
                    "name": "failure--" + job["version_id"] + ".md", "title": payload.get("title", "Unfinished note"),
                    "error_kind": payload.get("error_kind", "storage"), "diagnostic": redact(payload.get("error", ""))}
            atomic(root / "failed" / item["name"], payload.get("content", "# Unfinished note\n\nKept for retry.\n"))
            commit()
            key = "failed/" + item["name"]
            state["records"][key] = item
            state["sources"][job["source_id"]]["failed"] = key
            job["status"] = "failed"
            persist()
            return item
        name = "note--" + job["source_id"] + "--" + job["version_id"] + ".md"
        item = {**job, **payload["metadata"], "name": name, "section": "notes", "status": "saved",
                "saved": time.time(), "index_pending": True, "accounting": deduplicate(job["accounting"])}
        atomic(root / "notes" / name, payload["content"])
        commit()  # new note exists durably before archiving the old one
        old_key = job.get("previous")
        if old_key and old_key in state["records"]:
            old = state["records"][old_key]
            archive_name = "previous--" + old["version_id"] + ".md"
            atomic(root / "failed" / archive_name, (root / old_key).read_text(encoding="utf-8"))
            commit()  # archive first; old active pointer still works if interrupted
            archive = {**old, "name": archive_name, "section": "failed", "status": "superseded",
                       "explanation": "Previous version — replaced by redo", "replacement": item["version_id"]}
            state["records"]["failed/" + archive_name] = archive
            del state["records"][old_key]
            state["retired"].append(old_key)
        state["records"]["notes/" + name] = item
        state["sources"][job["source_id"]]["active"] = "notes/" + name
        state["sources"][job["source_id"]].pop("failed", None)
        job["status"] = "saved"
        persist()  # visibility switch, recovery never bootstraps new-format files
        if old_key:
            try:
                (root / old_key).unlink(missing_ok=True)
                commit()
            except OSError:
                pass  # registry excludes it; archive and replacement are durable
            from core.rag import remove_from_index
            remove_from_index(Path(old_key).name, volume)
        index_started = time.monotonic()
        try:
            from core import rag
            rag.index_note(str(root / "notes" / name), volume)
            item["index_pending"] = False
        except Exception as exc:
            item["index_diagnostic"] = redact(exc)
        if "compute" in item:
            from core.accounting import compute_estimate
            details = {d["resource"]: d["seconds"] for d in item["compute"].get("details", [])}
            item["compute"] = compute_estimate(details.get("cpu", 0), details.get("gpu", 0), time.monotonic() - index_started)
        try:
            persist()
        except OSError:
            # Saved registry already committed above; a later accounting/index
            # metadata failure must not turn successful persistence into failure.
            item["index_pending"] = True
        return item
    if operation == "cache_get":
        path = root / "evidence" / (identifier(payload["source_id"]) + ".json")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if operation == "delete":
        for entry in payload["records"]:
            if entry["section"] not in {"notes", "failed"}:
                raise ValueError("Invalid section")
            key = entry["section"] + "/" + identifier(entry["name"])
            item = state["records"].pop(key, None)
            if item:
                state["retired"].append(key)
                for lookup in state["sources"].values():
                    for field in ("active", "failed"):
                        if lookup.get(field) == key:
                            lookup.pop(field)
                # A running replacement must not resurrect an explicitly deleted note.
                lookup = state["sources"].get(item["source_id"], {})
                active_job = state["jobs"].get(lookup.get("job"))
                if active_job and active_job["status"] in {"accepted", "running"}:
                    active_job["status"] = "cancelled"
        persist()  # retrieval checks visibility even if index removal fails
        for entry in payload["records"]:
            (root / entry["section"] / identifier(entry["name"])).unlink(missing_ok=True)
            if entry["section"] == "notes":
                from core.rag import remove_from_index
                remove_from_index(entry["name"], volume)
        commit()
        return True  # conservatively retain media; shared references are never deleted
    if operation in {"retrieve", "reindex"}:
        from core import rag
        if operation == "retrieve":
            active = {r["name"]: r["title"] for r in state["records"].values() if r["section"] == "notes"}
            return rag.retrieve(payload["question"], active)
        active = [root / r["section"] / r["name"] for r in state["records"].values() if r["section"] == "notes"]
        rag.reindex_all_notes(volume, active)
        for r in state["records"].values():
            if r["section"] == "notes":
                r["index_pending"] = False
        persist()
        return True
    raise ValueError("Unknown storage operation")
