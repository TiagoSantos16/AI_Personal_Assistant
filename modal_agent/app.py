import os
import re

import modal

from core.processor import (
    _error_message,
    find_notes,
    media_kind,
    process_post,
    process_reel,
    read_notes,
    redo_note,
    send_telegram_message,
    process_url,
)

URL_RE = re.compile(r"https?://\S+")

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg")
    .env({"FASTEMBED_CACHE_PATH": "/models/fastembed"})
    .pip_install_from_pyproject("pyproject.toml")
    .run_commands(
        'python -c "from fastembed import TextEmbedding; TextEmbedding(\'BAAI/bge-small-en-v1.5\')"'
    )
    .add_local_python_source("core")
    .add_local_file("dashboard.py", "/root/dashboard.py")
)

app = modal.App("personal-assistant")
vol = modal.Volume.from_name("personal-assistant-data-v2", create_if_missing=True, version=2)
secrets = [modal.Secret.from_name("personal-assistant-secrets")]


@app.function(image=image, volumes={"/data": vol}, secrets=secrets)
@modal.web_server(8000, startup_timeout=60.0)
def ui():
    import subprocess

    subprocess.Popen(
        [
            "streamlit",
            "run",
            "/root/dashboard.py",
            "--server.port=8000",
            "--server.address=0.0.0.0",
            "--server.headless=true",
            "--server.enableCORS=false",
            "--server.enableXsrfProtection=false",
        ]
    )


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, gpu="T4", timeout=600)
def process_background(url: str, chat_id: int | None):
    process_reel(url, chat_id, vol)


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, timeout=420)
def process_post_background(url: str, chat_id: int | None):
    process_post(url, chat_id, vol)


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, gpu="T4", timeout=600)
def redo_background(note_name: str):
    redo_note(note_name, None, vol)


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, timeout=420)
def redo_post_background(note_name: str):
    redo_note(note_name, None, vol)


@app.function(image=image, volumes={"/data": vol}, secrets=secrets, timeout=180)
def ask_background(question: str, chat_id: int | None):
    from core.rag import ask_notes

    try:
        result = ask_notes(question)
        reply = result["answer"]
        if result["sources"]:
            reply += "\n\nSources:\n" + "\n".join(f"- {s}" for s in result["sources"])
    except Exception as exc:
        reply = _error_message(exc, "question")
    send_telegram_message(chat_id, reply)


@app.function(image=image, volumes={"/data": vol}, secrets=secrets)
@modal.fastapi_endpoint(method="POST")
def telegram_webhook(request: dict):
    message = request.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return {"status": "ok"}

    if text == "/link":
        reply = os.environ.get(
            "UI_URL", "UI URL not configured yet. Update your Modal secret."
        )
        send_telegram_message(chat_id, reply)

    elif text == "/notes":
        titles = read_notes()
        send_telegram_message(chat_id, "\n".join(titles) if titles else "No notes yet.")

    elif text.startswith("/find"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_telegram_message(chat_id, "Usage: /find <keyword>")
        else:
            matches = find_notes(parts[1])
            send_telegram_message(
                chat_id, "\n".join(matches) if matches else "No matches found."
            )

    elif text.startswith("/ask"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_telegram_message(chat_id, "Usage: /ask <your question about your notes>")
        else:
            send_telegram_message(chat_id, "Searching your notes...")
            ask_background.spawn(parts[1], chat_id)

    elif match := URL_RE.search(text):
        url = match.group(0)
        if media_kind(url) == "post":
            send_telegram_message(chat_id, "Processing post in background...")
            process_post_background.spawn(url, chat_id)
        else:
            send_telegram_message(chat_id, "Processing reel in background...")
            process_background.spawn(url, chat_id)

    else:
        send_telegram_message(
            chat_id,
            "Send me an Instagram Reel or post URL, or try /notes, /find <keyword> and /ask <question>.",
        )

    return {"status": "ok"}
