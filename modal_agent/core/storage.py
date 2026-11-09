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


def scheduled_work_due(*, root='/data', now=None):
    """Cheap read-only tick gate. No dispatch/bootstrap, directory scans or commits."""
    try:
        state = json.loads((Path(root) / 'private/registry.json').read_text(encoding='utf-8'))
        from core.reminders import due_items
        from core.deferred_answers import has_due_work
        now = time.time() if now is None else now
        due, changed = due_items(state, now)
        return bool(due or changed or has_due_work(state, now))
    except FileNotFoundError:
        return False  # No registry means no persisted task or question to dispatch.
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True  # An unreadable preflight must not silently suppress recovery.


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
    state.setdefault('contexts', {})
    state.setdefault('instructions', {})
    state.setdefault('memory', {'revision': 0, 'items': {}})
    state.setdefault('answer_revision', state['memory']['revision'])
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
    if operation.startswith('deferred_'):
        from core.deferred_answers import operate
        result = operate(state, operation, payload)
        if result['status'] != 'idle' or result.get('changed'):
            persist()
        return result
    if operation == 'repair_personal_data':
        from core.legacy_repair import repair
        result = repair(state, apply=payload.get('apply') is True)
        if result['applied'] and result['changed']:
            persist()
        return result
    if operation == 'reminder_tick':
        from core.reminders import tick
        return tick(state, persist)
    if operation == 'action_message_register':
        from core.reminders import context_key
        context = state.get('reminder_contexts', {}).get(context_key(payload), {})
        if context.get('origin_message_id') != payload.get('origin_message_id'):
            return False
        if context.get('ids'):
            state.setdefault('reminder_messages', {})[context_key(payload) + ':' + str(payload['message_id'])] = {
                'ids': context['ids'], 'date': time.time()}
            state['reminder_messages'] = dict(sorted(state['reminder_messages'].items(), key=lambda p: p[1]['date'])[-1000:])
            persist()
        return True
    if operation == 'reminder':
        from core.reminders import command as reminder_command
        if payload.get('expected_revision', state['answer_revision']) != state['answer_revision']:
            return 'Your reminders or personal memory changed while I was handling that. Please repeat the request.'
        if 'sequence' in payload:
            chat_key = str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)
            if state.get('chats', {}).get(chat_key, {}).get('sequence') != payload['sequence']:
                return 'A newer message arrived. Please repeat the reminder request.'
        from core.reminders import effective_tasks
        before = effective_tasks(state)
        reply, changed = reminder_command(state, payload)
        if changed:
            if effective_tasks(state) != before:
                state['answer_revision'] += 1
            persist()
        return reply
    if operation == 'source_register':
        from core.source_context import register
        register(state, payload, role='bot')
        persist()
        return True
    if operation == 'recall':
        from core import recall
        if payload.get('expected_revision', state['answer_revision']) != state['answer_revision']:
            return 'Your saved context changed while I was handling that. Please tell me again.'
        if 'sequence' in payload:
            from core.source_context import key
            if state.get('chats', {}).get(key(payload), {}).get('sequence') != payload['sequence']:
                return 'A newer message arrived. Please repeat the request.'
        rule, changed = recall.add(state, payload)
        if changed:
            state['answer_revision'] += 1
            persist()
        return "I'll bring up " + recall.title(state, rule) + ' when you ask about ' + rule['topic'] + '.'
    if operation == 'recall_get':
        from core.recall import match
        return {'revision': state['answer_revision'], 'rules': match(state, payload['question'])}
    if operation == 'recall_forget':
        from core.recall import tokens
        subject = tokens(payload['text'])
        matches = [r for r in state.get('recalls', {}).values() if r['active'] and
            (r['id'] == payload['text'] or subject <= tokens(r['topic']))]
        if len(matches) != 1 or not subject:
            return 'Which topic should I stop bringing up? Inspect /memory, then use /forgetrecall <topic>.'
        matches[0]['active'] = False
        state['answer_revision'] += 1
        persist()
        return 'I will no longer bring up that source for this topic. The note and Telegram messages remain.'
    if operation.startswith('chat_'):
        from core.conversation import bounded_history
        chats = state.setdefault('chats', {})
        key = str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)
        now = time.time()
        chat = chats.get(key, {})
        if now - chat.get('updated', 0) > 86400:
            chat = {}
        if operation == 'chat_context':
            chat['sequence'] = max(chat.get('sequence', -1), payload['update_id'])
            chat['updated'] = now
            chat['history'] = bounded_history([m for m in chat.get('history', []) if now - m['date'] <= 86400])
            if now - chat.get('pending', {}).get('date', 0) > 600:
                chat.pop('pending', None)
            chats[key] = chat
            state['chats'] = dict(sorted(chats.items(), key=lambda pair: pair[1].get('updated', 0))[-200:])
            persist()
            reminder_ctx = state.get('reminder_contexts', {}).get(key, {})
            from core.source_context import candidates
            tasks = [r for r in state.get('reminders', {}).values() if r['chat_id'] == payload['chat_id'] and
                r.get('thread_id') == payload.get('thread_id') and r['status'] not in {'done', 'cancelled'}]
            return {'history': chat['history'], 'pending': chat.get('pending'),
                'sources': candidates(state, payload),
                'tasks': [{'description': r['text'], 'status': r['status']} for r in tasks[-12:]],
                'reminder_pending': reminder_ctx.get('pending') if now - reminder_ctx.get('date', 0) <= 600 else None,
                'memory_revision': state['answer_revision'], 'sequence': chat['sequence']}
        current = chat.get('sequence') == payload.get('sequence')
        if operation == 'chat_current':
            return current
        if not current:
            return {'status': 'stale'}
        if operation == 'chat_pending':
            if payload.get('pending'):
                chat['pending'] = {**payload['pending'], 'date': now}
            else:
                chat.pop('pending', None)
        elif operation == 'chat_start':
            state.get('reminder_contexts', {}).pop(key, None)
            if not payload.get('source_url'):
                chat['source_focus'] = None
            from core.rag import retrieval_topic
            history = chat.get('history', [])
            chat['turn'] = uuid.uuid4().hex
            chat['history'] = bounded_history(history + [{'role': 'user', 'content': payload['question'],
                'retrieval_topic': retrieval_topic(payload['question'], history), 'date': now}])
            chat.pop('pending', None)
            if payload.get('comparison') and payload.get('source_url'):
                from core.source_context import note_resolution
                source = canonical_source(payload['source_url'])
                resolved = note_resolution(state['records'], state['sources'], state['jobs'], source['source_id'])
                if resolved['state'] != 'saved':
                    from core.deferred_answers import enqueue
                    enqueue(state, {**payload, 'history': history})
                    persist()
                    return {**payload, 'status': 'deferred', 'source_status': resolved['state']}
            persist()
            return {**payload, 'status': 'started', 'history': history, 'turn': chat['turn']}
        elif operation == 'chat_finish':
            if payload.get('turn') != chat.get('turn'):
                return {'status': 'stale'}
            if payload.get('memory_revision') != state['answer_revision']:
                return {'status': 'memory_changed'}
            active = {r['name'] for r in state['records'].values() if r['section'] == 'notes'}
            if any(not name.startswith('memory:') and name not in active for name in payload.get('sources', [])):
                return {'status': 'sources_changed'}
            chat['history'] = bounded_history(chat.get('history', []) + [
                {'role': 'assistant', 'content': payload['answer'], 'date': now}])
        else:
            raise ValueError('Unknown chat operation')
        persist()
        return {'status': 'ok'}
    if operation == 'memory_snapshot':
        return {'revision': state['answer_revision'], 'items': list(state['memory']['items'].values())}
    if operation == 'answer_sources_put':
        answers = state.setdefault('telegram_answers', {})
        cutoff = time.time() - 30 * 86400
        answers = {k: v for k, v in answers.items() if v['created'] >= cutoff}
        answers[str(payload['chat_id']) + ':' + str(payload['message_id'])] = {
            'sources': payload['sources'], 'created': time.time()}
        state['telegram_answers'] = dict(list(answers.items())[-1000:])
        persist()
        return True
    if operation == 'answer_sources_get':
        entry = state.get('telegram_answers', {}).get(str(payload['chat_id']) + ':' + str(payload['message_id']))
        if not entry:
            return None
        if time.time() - entry['created'] > 30 * 86400:
            return []
        active = {r['name']: r for r in state['records'].values() if r['section'] == 'notes'}
        return [{**active[name], 'label': label} for label, name in entry['sources'].items() if name in active]
    if operation == 'memory':
        if payload.get('expected_revision', state['answer_revision']) != state['answer_revision']:
            return 'Your personal memory changed while I was interpreting that. Please tell me again.'
        from core.personal import memory_command
        previous_memory = dict(state['memory']['items'])
        reply, changed = memory_command(state, payload.get('command', 'memory'), payload.get('text', ''),
            {**{k: payload.get(k) for k in ('chat_id', 'user_id', 'thread_id', 'message_id', 'update_id', 'source_text')},
                'source_url': payload.get('source_url'), 'date': payload.get('date') or time.time()})
        if payload.get('command') == 'memory':
            from core.recall import inspect
            reply += inspect(state)
        if changed:
            from core.reminders import sync_memory
            scheduling = sync_memory(state, previous_memory, payload)
            if scheduling:
                reply += '\n' + scheduling
            state['answer_revision'] += 1
            persist()
        return reply
    if operation == 'memory_get':
        from core.personal import relevant, contextualize
        items = [contextualize(i, state['records'], state['sources']) for i in state['memory']['items'].values()
            if i.get('active', True) and i.get('repair_status') != 'needs_clarification']
        for item in items:
            associated = [r for r in state.get('reminders', {}).values() if r.get('memory_id') == item['id']]
            if associated:
                from core.reminders import STATUS, when
                reminder = associated[-1]
                item['text'] += '\nAssociated reminder: ' + STATUS[reminder['status']] + '; ' + when(reminder)
        from core.reminders import memory_items
        scope = payload.get('scope', 'answer')
        if scope == 'notes':
            items = [i for i in items if i['kind'] in {'fact', 'preference'}]
        else:
            items.extend(memory_items(state))
        selected = relevant(items, payload.get('question', ''), scope)
        import hashlib
        fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {'revision': state['memory']['revision'] if scope == 'notes' else state['answer_revision'],
            'fingerprint': fingerprint, 'items': selected}
    if operation == 'memory_sources':
        # Only live memory IDs may supply links; forgotten entries cannot be revived.
        from core import rag
        from core.reminders import memory_items
        available = {i: item for i, item in state['memory']['items'].items()
            if item.get('active', True) and item.get('repair_status') != 'needs_clarification'}
        available.update({i['id']: i for i in memory_items(state)})
        source_ids = {available[i].get('source_id') for i in payload.get('ids', []) if i in available}
        hits, titles = [], {}
        for source_id in sorted(s for s in source_ids if s):
            item = state['records'].get(state['sources'].get(source_id, {}).get('active'))
            if not item:
                continue
            titles[item['name']] = item['title']
            body = rag._note_body((root / 'notes' / identifier(item['name'])).read_text(encoding='utf-8'))
            hits.extend({'source': item['name'], 'text': text, '_distance': 0} for text in rag._split(body))
        return rag.select_hits(hits, titles)
    if operation == 'memory_revision':
        return state['answer_revision']
    if operation == 'associate':
        from core.source_context import associate
        return associate(state, payload)
    if operation == 'current':
        job = state['jobs'].get(payload['job_id'], {})
        return bool(job and job['attempt'] == payload['attempt'] and job['status'] == 'running' and
            state['sources'][job['source_id']].get('job') == job['job_id'] and
            payload.get('memory_revision', state['memory']['revision']) == state['memory']['revision'])
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
        item = dict(state["records"][key])
        # Recover missing note metadata only from its own version's durable ledger.
        job = state['jobs'].get(item.get('job_id'), {})
        if not item.get('accounting') and job.get('version_id') == item.get('version_id') and job.get('accounting'):
            item['accounting'] = deduplicate(job['accounting'])
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
        if 'expected_sequence' in payload:
            chat_key = str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)
            if state.get('chats', {}).get(chat_key, {}).get('sequence') != payload['expected_sequence']:
                return {'status': 'stale'}
        source = canonical_source(payload["url"])
        if payload.get('chat_id') is not None and 'update_id' in payload:
            chat_key = str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)
            chat = state.setdefault('chats', {}).setdefault(chat_key, {})
            chat.update(sequence=max(chat.get('sequence', -1), payload['update_id']), updated=time.time())
            chat.pop('pending', None)
        lookup = state["sources"].setdefault(source["source_id"], {})
        from core.source_context import register
        register(state, payload, role='instruction' if payload.get('followup') else 'user')
        instructions = state['instructions'].setdefault(source['source_id'], [])
        wording = payload.get('instructions', '').strip()
        new_instruction = bool(wording and (not instructions or instructions[-1]['text'] != wording))
        if new_instruction:
            if len(wording) > 4000 or sum(len(i['text']) for i in instructions) + len(wording) > 12000:
                return {'status': 'instruction_limit'}
            instructions.append({'text': wording, **{k: payload.get(k) for k in ('message_id', 'chat_id')},
                'date': payload.get('date') or time.time()})
        previous = payload.get("previous")
        job = state["jobs"].get(lookup.get("job"))
        lease = max(660, float(os.environ.get("JOB_LEASE_SECONDS", "900")))
        if job and job["status"] in {"accepted", "running"}:
            if not new_instruction and (not payload.get("explicit") or time.time() - job.get("claimed", job["created"]) < lease):
                persist()
                return {"status": "running"}
        if not payload.get("explicit") and not new_instruction:
            for field in ("active", "failed"):
                if lookup.get(field) in state["records"]:
                    persist()
                    return {"status": field, "record": state["records"][lookup[field]]}
        if previous and previous not in state["records"]:
            raise ValueError("Record no longer available")
        if previous:
            previous_record = state["records"][previous]
            if previous_record["source_id"] != source["source_id"]:
                raise ValueError("Retry source does not match the record")
            source["kind"] = previous_record["kind"]
        job_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        if job and job['status'] in {'accepted', 'running'}:
            job['status'] = 'superseded'
            name = 'superseded--' + job['version_id'] + '.md'
            atomic(root / 'failed' / name, '# Superseded processing attempt\n\nNewer instructions replaced this attempt. Charges already incurred remain attached.\n')
            commit()
            state['records']['failed/' + name] = {**job, 'name': name, 'section': 'failed',
                'title': 'Superseded processing attempt', 'media': [],
                'explanation': 'Processing replaced by newer source instructions.'}
        job = {**source, "job_id": job_id, "attempt": token, "version_id": uuid.uuid4().hex,
               'instructions': list(instructions),
               "created": time.time(), "status": "accepted", "previous": lookup.get("active"),
               "chat_id": payload.get("chat_id"), "message_id": payload.get("message_id"), "accounting": []}
        job.update({k: payload.get(k) for k in ('user_id', 'thread_id', 'date', 'update_id')})
        state["jobs"][job_id] = job
        lookup["job"] = job_id
        if "update_id" in payload:
            state["updates"][str(payload["update_id"])] = {"accepted": time.time(), "job_id": job_id}
        persist()
        return {"status": "accepted", "job": job}
    if operation in {"claim", "save", "fail", "enqueue_failed", "notify", "ledger", "cache_put"}:
        job = state["jobs"][identifier(payload["job_id"])]
        # Charges belong to the original version even after supersession.
        if operation == 'ledger' and payload.get('attempt') == job['attempt']:
            job['accounting'] = deduplicate(job['accounting'] + payload['records'])
            for item in state['records'].values():
                if item.get('version_id') == job['version_id']:
                    item['accounting'] = job['accounting']
            persist()
            return True
        if payload.get("attempt") != job["attempt"] or state["sources"][job["source_id"]].get("job") != job["job_id"]:
            return {"status": "stale"}
        if operation == "claim":
            if job["status"] != "accepted":
                return {"status": "stale"}
            job.update(status="running", claimed=time.time())
            persist()
            return {"status": "claimed", "job": job}
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
        if operation == 'save':
            if payload.get('memory_fingerprint'):
                current_memory = dispatch('memory_get', {'question': payload.get('memory_query', ''), 'scope': 'notes'}, root=root)
                if current_memory['fingerprint'] != payload['memory_fingerprint']:
                    raise ValueError('Relevant personal memory changed during generation; retry with current memory.')
            elif payload.get('memory_revision', state['memory']['revision']) != state['memory']['revision']:
                raise ValueError('Personal memory changed during generation; retry with current memory.')
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
    if operation == 'source_context':
        from core import rag
        from core.source_context import note_resolution
        source = canonical_source(payload['url'])
        resolved = note_resolution(state['records'], state['sources'], state['jobs'], source['source_id'])
        item = resolved.get('note')
        if not item:
            return {'status': 'unavailable', 'source_status': resolved['state'], 'rows': []}
        body = rag._note_body((root / item['section'] / identifier(item['name'])).read_text(encoding='utf-8'))
        hits = [{'source': item['name'], 'text': text, '_distance': 0} for text in rag._split(body)]
        return {'status': 'ready', 'source_status': 'saved', 'review_status': item.get('review_status'),
            'category': item.get('category', 'general'),
            'rows': rag.select_hits(hits, {item['name']: item['title']}, budget=max(900, min(16000, int(payload.get('budget', 16000)))))}
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
