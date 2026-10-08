import hmac
import logging
import os
import modal
from fastapi import Request, HTTPException
from core.requests import authorised, command, urls, canonical_source
from core.telegram import send, react
from core.errors import redact

image = (modal.Image.debian_slim(python_version="3.11").apt_install("ffmpeg")
    .env({"FASTEMBED_CACHE_PATH": "/models/fastembed"})
    .pip_install_from_pyproject("pyproject.toml")
    .run_commands('python -c "from fastembed import TextEmbedding; TextEmbedding(\'BAAI/bge-small-en-v1.5\')"')
    .add_local_python_source("core").add_local_file("dashboard.py", "/root/dashboard.py")
    .add_local_file("dashboard_server.py", "/root/dashboard_server.py")
    .add_local_file(".streamlit/config.toml", "/root/.streamlit/config.toml"))
app = modal.App("personal-assistant")
vol = modal.Volume.from_name("personal-assistant-data-v2", create_if_missing=True, version=2)
weights = modal.Volume.from_name("personal-assistant-whisper-weights", create_if_missing=True)
secrets = [modal.Secret.from_name("personal-assistant-secrets", required_keys=[
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_ALLOWED_USER_ID",
    "OPENROUTER_API_KEY", "DASHBOARD_PASSWORD", "UI_URL"])]


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, max_containers=1, timeout=600, cpu=1, memory=2048)
def storage(operation: str, payload: dict = None):
    from core.storage import dispatch
    vol.reload()  # no open handles survive dispatch
    return dispatch(operation, payload, volume=vol)


@app.cls(image=image, volumes={"/data": vol, "/weights": weights}, secrets=secrets,
         gpu="T4", min_containers=0, scaledown_window=30, timeout=360, cpu=1, memory=4096)
class Transcriber:
    @modal.enter()
    def load(self):
        import whisper
        self.model = whisper.load_model("turbo", download_root="/weights")
        weights.commit()

    @modal.method()
    def transcribe(self, path: str):
        import time
        from pathlib import Path
        from core.media import probe, MAX_BYTES
        target = Path(path)
        if not target.is_absolute() or not target.is_relative_to("/data/attempts") or ".." in target.parts:
            raise ValueError("Invalid transcription path")
        vol.reload()
        if target.stat().st_size > MAX_BYTES:
            raise ValueError("Transcription byte limit")
        _, audio = probe(target)
        if not audio:
            return {"segments": [], "text": ""}
        started = time.monotonic()
        result = self.model.transcribe(str(target), word_timestamps=False)
        return {"text": result.get("text", ""), "segments": [
            {"start": s["start"], "end": s["end"], "text": s["text"]} for s in result.get("segments", [])],
            "gpu_seconds": time.monotonic() - started}


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, timeout=600, cpu=1, memory=2048)
def process_background(job_id: str, attempt: str):
    from core.workflow import process
    process(job_id, attempt, storage.remote, Transcriber().transcribe.remote, vol)


def enqueue(result):
    job = result["job"]
    try:
        process_background.spawn(job["job_id"], job["attempt"])
        return True
    except Exception as exc:
        storage.remote("enqueue_failed", {"job_id": job["job_id"], "attempt": job["attempt"], "error": redact(exc)})
        return False


@app.function(image=image, secrets=secrets, timeout=180)
def redo_background(note_name: str, section: str = "notes"):
    note = storage.remote("read", {"name": note_name, "section": section})
    result = storage.remote("accept", {"url": note["url"], "explicit": True, "previous": section + "/" + note_name})
    if result["status"] == "accepted":
        enqueue(result)


@app.function(image=image, secrets=secrets, timeout=180)
def ask_background(question: str, chat_id: int = None):
    from core.rag import answer, still_visible
    try:
        result = answer(question, storage.remote("retrieve", {"question": question}))
        result = still_visible(result, storage.remote("list")["records"])
        reply = result["answer"]
        if result["sources"]:
            reply += "\n\nSources:\n" + "\n".join(result.get("source_titles", {}).get(name, name) for name in result.get("source_labels", {}).values())
    except Exception:
        reply = "I couldn't search your notes right now. Please try again later."
    send(chat_id, reply)


@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST")
async def telegram_webhook(request: Request):
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    missing = [name for name in ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_ALLOWED_USER_ID")
               if not os.environ.get(name, "").strip()]
    if missing:
        logging.error("Webhook access denied: missing %s in Modal secret personal-assistant-secrets", ", ".join(missing))
        raise HTTPException(503, "Unavailable")
    if not hmac.compare_digest(header.encode(), os.environ["TELEGRAM_WEBHOOK_SECRET"].encode()):
        raise HTTPException(403, "Forbidden")
    update = await request.json()
    if not isinstance(update, dict):
        return {"status": "ignored"}
    message = update.get("message") or {}
    if not authorised(header, message, os.environ):
        return {"status": "ignored"}
    if not isinstance(update.get("update_id"), int):
        return {"status": "ignored"}
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    name, argument = command(text)
    # This idempotent command needs no Volume, index, worker, or model.
    if name == "link":
        send(chat_id, os.environ.get("UI_URL") or "Dashboard link is unavailable.")
        return {"status": "ok"}
    if not storage.remote("update", {"update_id": update["update_id"]}):
        return {"status": "duplicate"}
    if name:
        if name in {"notes", "find"}:
            if name == "find" and not argument:
                send(chat_id, "Usage: /find <keyword>")
                return {"status": "ok"}
            records = [r for r in storage.remote("list")["records"] if r["section"] == "notes"]
            if name == "find":
                records = storage.remote("search", {"query": argument, "records": [
                    {"section": r["section"], "name": r["name"]} for r in records]})
            matches = [r["title"] for r in records]
            send(chat_id, "\n".join(matches) or "No matches found.")
        elif name == "ask":
            if argument:
                ask_background.spawn(argument, chat_id)
                send(chat_id, "Searching your notes...")
            else:
                send(chat_id, "Usage: /ask <question>")
        return {"status": "ok"}
    links = urls(message)
    if len(links) > 1:
        send(chat_id, "Please send one Instagram link at a time.")
        return {"status": "ok"}
    if not links:
        return {"status": "ignored"}
    try:
        source = canonical_source(links[0])
    except ValueError:
        send(chat_id, "Please send an Instagram reel or post link.")
        return {"status": "ok"}
    result = storage.remote("accept", {**source, "chat_id": chat_id, "message_id": message.get("message_id"), "update_id": update["update_id"]})
    if result["status"] == "accepted":
        react(chat_id, message.get("message_id"))
        if not enqueue(result):
            send(chat_id, "I couldn't start this one. I've kept it in Error/Failed for retry.")
    elif result["status"] == "running":
        send(chat_id, "I'm still working on that one.")
    elif result["status"] == "active":
        from urllib.parse import urlencode
        note = result["record"]
        reply = "That post is already in your notes: " + note["title"]
        if os.environ.get("UI_URL"):
            reply += "\n" + os.environ["UI_URL"].rstrip("/") + "/?" + urlencode({"note": note["name"]})
        send(chat_id, reply)
    else:
        send(chat_id, "That post is in Error/Failed. You can retry it from the dashboard.")
    return {"status": "ok"}


@app.function(image=image, secrets=secrets, timeout=86400, max_containers=1)
@modal.concurrent(max_inputs=32)
@modal.web_server(8000, startup_timeout=60)
def ui():
    import subprocess
    subprocess.Popen(["streamlit", "run", "/root/dashboard_server.py", "--server.port=8000",
        "--server.address=0.0.0.0", "--server.headless=true", "--server.enableCORS=true",
        "--server.enableXsrfProtection=true", "--browser.gatherUsageStats=false"], cwd="/root")
