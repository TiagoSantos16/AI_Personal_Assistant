import hmac
import logging
import os
import modal
from fastapi import Request, HTTPException
from core.requests import authorised, command, urls, canonical_source, source_instructions
from core.telegram import send, react, send_answer, source_text, call as telegram_call
from core.errors import redact
from core.voice import SEARCH_ERROR

image = (modal.Image.debian_slim(python_version="3.11").apt_install("ffmpeg", "tzdata")
    .env({"FASTEMBED_CACHE_PATH": "/models/fastembed"})
    .pip_install_from_pyproject("pyproject.toml")
    .run_commands('python -c "from fastembed import TextEmbedding; TextEmbedding(\'BAAI/bge-small-en-v1.5\')"')
    .add_local_python_source("core")
    .add_local_file("dashboard_server.py", "/root/dashboard_server.py")
    .add_local_file("dashboard_auth.py", "/root/dashboard_auth.py")
    .add_local_file("dashboard_store.py", "/root/dashboard_store.py")
    .add_local_dir("dashboard_web/dist", "/root/dashboard_web/dist"))
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
    if operation == 'dashboard_snapshot':
        from dashboard_store import snapshot
        return snapshot(volume=vol)
    return dispatch(operation, payload, volume=vol)


@app.function(image=image, secrets=secrets, volumes={'/data': vol.with_mount_options(read_only=True)}, schedule=modal.Cron('* * * * *'),
              timeout=90, max_containers=1, cpu=0.125, memory=256)
def reminder_tick():
    """The existing serialized store checks due reminders; idle ticks send nothing."""
    from core.storage import scheduled_work_due
    vol.reload()
    if not scheduled_work_due():
        return {'status': 'idle'}
    for _ in range(4):
        result = storage.remote('reminder_tick')
        if result['status'] == 'idle':
            break
    for _ in range(4):
        pending = storage.remote('deferred_poll')
        if pending['status'] == 'idle':
            break
        job = pending['job']
        if pending['status'] == 'ready':
            try:
                ask_background.spawn(job['question'], job['chat_id'], {**job, 'deferred': True})
            except Exception:
                # Do not retry an ambiguous spawn; the durable lease reports interruption.
                logging.warning('conversation_event correlation=%s stage=deferred_spawn status=uncertain source_id=%s', job['correlation_id'], job['source_id'])
        else:
            from core.reminders import deliver
            text = ("This source couldn't finish processing. I've kept your question and will resume after a successful retry." if pending['kind'] == 'source_failed' else
                "I couldn't finish the saved comparison request. Please ask again using this source; I won't repeat a possibly charged attempt automatically.")
            outcome = deliver(job['chat_id'], text + '\n' + job['source_url'], job.get('thread_id'))
            storage.remote('deferred_notice_done', {'id': job['id'], 'notice_attempt': job['notice']['attempt'],
                'outcome': outcome['status'] if isinstance(outcome, dict) else outcome})
    return result


@app.local_entrypoint()
def repair_personal_data(apply: bool = False):
    """Owner-run preview by default; --apply persists only supported repairs."""
    import json
    print(json.dumps(storage.remote('repair_personal_data', {'apply': apply}), ensure_ascii=False, indent=2))


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


def send_action_reply(chat_id, text, provenance):
    delivery = send(chat_id, text, capture=True,
        **({'thread_id': provenance['thread_id']} if provenance.get('thread_id') is not None else {}))
    if isinstance(delivery, dict):
        for message_id in delivery['message_ids']:
            try:
                storage.remote('action_message_register', {**provenance,
                    'origin_message_id': provenance.get('message_id'), 'message_id': message_id})
            except Exception:
                logging.getLogger(__name__).warning('Could not associate the task acknowledgement')


@app.function(image=image, secrets=secrets, timeout=180)
def redo_background(note_name: str, section: str = "notes"):
    note = storage.remote("read", {"name": note_name, "section": section})
    result = storage.remote("accept", {"url": note["url"], "explicit": True, "previous": section + "/" + note_name})
    if result["status"] == "accepted":
        enqueue(result)


@app.function(image=image, secrets=secrets, timeout=180)
def ask_background(question: str, chat_id: int = None, conversation: dict = None):
    from core.rag import grounded_answer
    from core.errors import log_answer_failure, AnswerPathError
    import uuid
    correlation_id, stage = uuid.uuid4().hex[:12], 'answer_path'
    delivery_options = {'thread_id': conversation['thread_id']} if conversation and conversation.get('thread_id') is not None else {}
    deferred = bool(conversation and conversation.get('deferred'))
    try:
        if deferred:
            stage = 'deferred_claim'
            if storage.remote('deferred_start', conversation)['status'] != 'started':
                return
        if conversation:
            result = grounded_answer(question, storage.remote, history=conversation['history'],
                allow_general=True, source_url=conversation.get('source_url'),
                **({'comparison': True} if conversation.get('comparison') else {}),
                **({'correlation_id': conversation['correlation_id']} if conversation.get('correlation_id') else {}))
            correlation_id = result.get('correlation_id', correlation_id)
            stage = 'chat_finish'
            finished = storage.remote('deferred_result', {**conversation, 'result': result}) if deferred else storage.remote(
                'chat_finish', {**conversation, 'answer': result['answer'],
                    'memory_revision': result['memory_revision'], 'sources': result.get('sources', [])})
            if finished['status'] == 'changed':
                send(chat_id, 'The source or your preferences changed while I was comparing. Please ask again for a current comparison.', **delivery_options)
                return
            if finished['status'] == 'stale':
                return
            if finished['status'] == 'memory_changed':
                send(chat_id, 'Personal memory changed while I was answering. Please ask again.', **delivery_options)
                return
            if finished['status'] == 'sources_changed':
                send(chat_id, 'A referenced note changed while I was answering. Please ask again.', **delivery_options)
                return
        else:
            result = grounded_answer(question, storage.remote)
    except Exception as exc:
        if not isinstance(exc, AnswerPathError):
            log_answer_failure(logging.getLogger(__name__), exc, stage=stage, correlation_id=correlation_id)
        send(chat_id, ("I couldn't complete the comparison. I've kept the question; please ask again using this source:\n" +
            conversation['source_url']) if deferred else SEARCH_ERROR, **delivery_options)
        if deferred:
            try:
                storage.remote('deferred_done', {**conversation, 'success': False})
            except Exception as cleanup_error:
                log_answer_failure(logging.getLogger(__name__), cleanup_error,
                    stage='deferred_state', correlation_id=conversation.get('correlation_id', correlation_id))
        return
    try:
        delivered = send_answer(chat_id, result['answer'], result.get('source_labels', {}), storage.remote, **delivery_options)
        if deferred:
            storage.remote('deferred_done', {**conversation, 'success': delivered is True})
    except Exception as exc:
        log_answer_failure(logging.getLogger(__name__), exc, stage='answer_delivery',
            correlation_id=result.get('correlation_id', correlation_id))


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
    callback, reaction = update.get('callback_query'), update.get('message_reaction')
    if isinstance(callback, dict) or isinstance(reaction, dict):
        event = callback if isinstance(callback, dict) else reaction
        sender = event.get('from') if callback else event.get('user')
        if not authorised(header, {'from': sender}, os.environ) or not isinstance(update.get('update_id'), int):
            return {'status': 'ignored'}
        if callback and callback.get('data') != 'answer_sources':
            return {'status': 'ignored'}
        if reaction and not any(r not in reaction.get('old_reaction', []) for r in reaction.get('new_reaction', [])):
            return {'status': 'ignored'}
        message = event.get('message', {}) if callback else event
        chat_id, message_id = message.get('chat', {}).get('id'), message.get('message_id')
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return {'status': 'ignored'}
        if callback:
            telegram_call('answerCallbackQuery', {'callback_query_id': callback['id']})
        if not storage.remote('update', {'update_id': update['update_id']}):
            return {'status': 'duplicate'}
        records = storage.remote('answer_sources_get', {'chat_id': chat_id, 'message_id': message_id})
        if records is not None:
            send(chat_id, source_text(records), **({'thread_id': message['message_thread_id']} if message.get('message_thread_id') is not None else {}))
        elif callback:
            send(chat_id, 'These sources are no longer available. Please ask the question again.',
                **({'thread_id': message['message_thread_id']} if message.get('message_thread_id') is not None else {}))
        return {'status': 'ok'}
    message = update.get("message") or {}
    if not authorised(header, message, os.environ):
        return {"status": "ignored"}
    if not isinstance(update.get("update_id"), int):
        return {"status": "ignored"}
    chat_id = message.get("chat", {}).get("id")
    def respond(text, **kwargs):
        options = {'thread_id': message['message_thread_id']} if message.get('message_thread_id') is not None else {}
        return send(chat_id, text, **options, **kwargs)
    text = (message.get("text") or message.get("caption") or "").strip()
    name, argument = command(text)
    # This idempotent command needs no Volume, index, worker, or model.
    if name == "link":
        respond(os.environ.get("UI_URL") or "Dashboard link is unavailable.")
        return {"status": "ok"}
    if not storage.remote("update", {"update_id": update["update_id"]}):
        return {"status": "duplicate"}
    provenance = {'chat_id': chat_id, 'message_id': message.get('message_id'),
        'date': message.get('date'), 'thread_id': message.get('message_thread_id'), 'user_id': message.get('from', {}).get('id')}
    if name == 'forgetrecall':
        try:
            reply = storage.remote('recall_forget', {**provenance, 'text': argument})
        except Exception:
            reply = "I couldn't change source recall. Please try again."
        respond(reply)
        return {'status': 'ok'}
    reminder_commands = {'remind': 'add', 'reminders': 'list', 'done': 'done', 'cancel': 'cancel',
        'reschedule': 'reschedule', 'snooze': 'reschedule', 'retryreminder': 'retry'}
    if name in reminder_commands:
        try:
            association = storage.remote('associate', {**provenance,
                'reply_id': (message.get('reply_to_message') or {}).get('message_id')})
            reply = storage.remote('reminder', {**provenance, 'command': reminder_commands[name],
                'text': argument, 'source_url': association.get('url'), 'source_text': text})
        except ValueError as exc:
            reply = str(exc)
        except Exception:
            reply = "I couldn't save or read your reminders. Please try again."
        send_action_reply(chat_id, reply, provenance)
        return {'status': 'ok'}
    if name in {'memory', 'remember', 'correct', 'forget'}:
        try:
            memory_source = None
            if name in {'remember', 'correct'}:
                from core.personal import needs_source
                if needs_source(argument):
                    memory_source = storage.remote('associate', {**provenance,
                        'reply_id': (message.get('reply_to_message') or {}).get('message_id')}).get('url')
            reply = storage.remote('memory', {**provenance, 'command': name, 'text': argument,
                'source_url': memory_source, 'source_text': text})
        except Exception:
            reply = "I couldn't save or read personal memory. Please try again."
        from core.reminders import human_reply
        send_action_reply(chat_id, human_reply(reply), provenance)
        return {'status': 'ok'}
    natural = None
    if (not name and not urls(message)) or (name == 'ask' and argument):
        from core.conversation import prepare_message
        try:
            natural = prepare_message(argument if name == 'ask' else text, message, provenance,
                update['update_id'], storage.remote, force_answer=name == 'ask')
        except Exception:
            respond("I couldn't handle that message right now. Please try again; I haven't confirmed any change.")
            return {'status': 'ok'}
        intent = natural['intent']
        if intent == 'ignore':
            return {'status': 'ignored'}
        if intent == 'reply':
            from core.reminders import human_reply
            if natural.get('task_reply'):
                send_action_reply(chat_id, human_reply(natural['reply']), provenance)
            else:
                respond(human_reply(natural['reply']))
            return {'status': 'ok'}
        if intent == 'answer':
            ask_background.spawn(natural['text'], chat_id, natural['conversation'])
            telegram_call('sendChatAction', {'chat_id': chat_id, 'action': 'typing'})
            return {'status': 'ok'}
        if intent == 'link':
            respond(os.environ.get('UI_URL') or 'Dashboard link is unavailable.')
            return {'status': 'ok'}
        name, argument = intent, natural.get('text', '')
    if name and name != 'revise':
        if name in {"notes", "find"}:
            if name == "find" and not argument:
                respond("Usage: /find <keyword>")
                return {"status": "ok"}
            records = [r for r in storage.remote("list")["records"] if r["section"] == "notes"]
            if name == "find":
                records = storage.remote("search", {"query": argument, "records": [
                    {"section": r["section"], "name": r["name"]} for r in records]})
            matches = [r["title"] for r in records]
            respond("\n".join(matches) or "No matches found.")
        elif name == "ask":
            if argument:
                ask_background.spawn(argument, chat_id)
                respond("Searching your notes...")
            else:
                respond("Usage: /ask <question>")
        return {"status": "ok"}
    links = urls(message)
    instructions = source_instructions(message) if links else (argument if name == 'revise' else text)
    if links and name == 'revise':
        instructions = command(instructions)[1]
    if natural and natural['intent'] == 'revise':
        links, instructions = [natural['source_url']], natural['text']
    if len(links) > 1:
        respond("Please send one Instagram link at a time.")
        return {"status": "ok"}
    if not links:
        if not instructions:
            respond('Usage: /revise <instructions>, replying to the intended link.')
            return {'status': 'ok'}
        association = storage.remote('associate', {**provenance,
            'reply_id': (message.get('reply_to_message') or {}).get('message_id')})
        if association['status'] != 'resolved':
            respond('Please reply to the intended link with /revise <instructions>. Use /remember for enduring facts or preferences.')
            return {'status': 'ok'}
        links = [association['url']]
    try:
        source = canonical_source(links[0])
    except ValueError:
        respond("Please send an Instagram reel or post link.")
        return {"status": "ok"}
    routing_guard = {'expected_sequence': natural['sequence']} if natural and natural['intent'] == 'revise' else {}
    result = storage.remote("accept", {**source, **provenance, **routing_guard, 'instructions': instructions,
        'followup': not bool(urls(message)), "update_id": update["update_id"]})
    if result['status'] == 'stale':
        return {'status': 'ignored'}
    if result["status"] == "accepted":
        react(chat_id, message.get("message_id"))
        if not enqueue(result):
            respond("I couldn't start this one. I've kept it in Error/Failed for retry.")
    elif result["status"] == "running":
        respond("I'm still working on that one.")
    elif result["status"] == "active":
        from urllib.parse import urlencode
        note = result["record"]
        reply = "That post is already in your notes: " + note["title"]
        if os.environ.get("UI_URL"):
            reply += "\n" + os.environ["UI_URL"].rstrip("/") + "/?" + urlencode({"note": note["name"]})
        delivery = respond(reply, capture=True)
        if isinstance(delivery, dict):
            for message_id in delivery['message_ids']:
                storage.remote('source_register', {**provenance, 'url': source['url'], 'message_id': message_id})
    elif result['status'] == 'instruction_limit':
        respond('Instructions exceed the limit (4,000 characters per message, 12,000 per source). Nothing changed.')
    else:
        respond("That post is in Error/Failed. You can retry it from the dashboard.")
    return {"status": "ok"}


@app.function(image=image, secrets=secrets, timeout=86400, max_containers=1)
@modal.concurrent(max_inputs=32)
@modal.web_server(8000, startup_timeout=60)
def ui():
    import subprocess
    import sys
    # This port is reached through Modal's TLS-terminating proxy. Preserve the
    # external HTTPS scheme for same-origin checks and secure session cookies.
    subprocess.Popen([sys.executable, "-m", "uvicorn", "dashboard_server:app",
        "--host", "0.0.0.0", "--port", "8000", "--proxy-headers",
        "--forwarded-allow-ips", "*"], cwd="/root")
