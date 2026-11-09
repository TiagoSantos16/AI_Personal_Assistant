"""Idle ticks must not wake the expensive storage function. No remote services."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import conversation, rag, reminders, storage
from test_reminders import s, clock, add, items, ZONE
from test_webhook import endpoint


def due(root):
    return storage.scheduled_work_due(root=root)


def test_missing_registry_is_idle_without_creating_files(tmp_path):
    assert not due(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_empty_preflight_is_read_only_and_never_scans_or_dispatches(tmp_path, monkeypatch):
    path = tmp_path / 'private/registry.json'
    storage.atomic(path, {'records': {}, 'sources': {}, 'jobs': {}})
    before = path.read_bytes()
    monkeypatch.setattr(Path, 'glob', lambda *a, **kw: pytest.fail('idle preflight scanned note files'))
    monkeypatch.setattr(storage, 'dispatch', lambda *a, **kw: pytest.fail('idle preflight dispatched expensive storage'))
    monkeypatch.setattr(rag, '_get_model', lambda: pytest.fail('idle preflight loaded embeddings'))
    assert not due(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('day', [(2026, 11, 2), (2026, 3, 28), (2026, 10, 24)])
def test_due_brief_recurrence_and_snooze_keep_lisbon_time(s, clock, tmp_path, monkeypatch, day):
    clock[0] = datetime(*day, 18, tzinfo=ZONE).timestamp()
    add(s, 'Call dentist')
    item = next(iter(items(tmp_path).values()))
    clock[0] = item['due'] - 1
    assert not due(tmp_path)
    clock[0] = item['due']
    assert due(tmp_path)
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a) or 'sent')
    assert s('reminder_tick')['count'] == 1
    assert not due(tmp_path)  # Today's successful delivery is not another poll reason.
    clock[0] = (reminders.local_now(item['due']) + timedelta(days=1)).timestamp()
    assert due(tmp_path)
    assert s('reminder_tick')['count'] == 1
    s('reminder', {'command': 'reschedule', 'target': item['id'], 'timing': 'in 2 days', 'chat_id': 7})
    assert not due(tmp_path)
    clock[0] = items(tmp_path)[item['id']]['due']
    assert due(tmp_path)
    s('reminder', {'command': 'done', 'target': item['id'], 'chat_id': 7})
    assert not due(tmp_path) and len(sent) == 2


def test_legacy_clarification_normalization_does_not_keep_storage_warm(s, tmp_path):
    add(s, 'Call dentist')
    path = tmp_path / 'private/registry.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    next(iter(state['reminders'].values()))['text'] = 'Search this'
    storage.atomic(path, state)
    assert due(tmp_path)  # Persist the conservative legacy repair marker once.
    assert s('reminder_tick')['status'] == 'idle'
    assert not due(tmp_path)


def queue_question(s):
    source = s('accept', {'url': 'https://www.instagram.com/p/CostFixture/', 'chat_id': 7, 'message_id': 1})['job']
    result = conversation.prepare_message('Compare these tools with my saved notes', {},
        {'chat_id': 7, 'message_id': 2}, 2, s)
    assert result['intent'] == 'reply'
    return source


def save(s, source):
    identity = {k: source[k] for k in ('job_id', 'attempt')}
    s('claim', identity)
    return s('save', {**identity, 'content': '# Video tool\nGrounded video editing evidence.',
        'metadata': {'title': 'Video tool', 'category': 'coding', 'review_status': 'needs_review'}})


def test_pending_comparison_does_not_poll_storage_until_source_ready(s, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(rag, 'index_note', lambda *a: None)
    source = queue_question(s)
    assert not due(tmp_path)
    save(s, source)
    assert due(tmp_path)
    claimed = s('deferred_poll')['job']
    assert not due(tmp_path)  # Already claimed: do not wake storage or generate again.
    clock[0] += 241
    assert due(tmp_path)
    assert s('deferred_poll')['kind'] == 'interrupted'
    assert not due(tmp_path)
    assert s('deferred_start', claimed)['status'] == 'stale'


def test_failed_source_notice_once_then_successful_retry_resumes(s, tmp_path, monkeypatch):
    monkeypatch.setattr(rag, 'index_note', lambda *a: None)
    source = queue_question(s)
    identity = {k: source[k] for k in ('job_id', 'attempt')}
    s('claim', identity)
    failed = s('fail', {**identity, 'error': 'fixture extraction failure'})
    assert due(tmp_path)
    assert s('deferred_poll')['kind'] == 'source_failed'
    assert not due(tmp_path)
    retry = s('accept', {'url': source['url'], 'explicit': True, 'previous': 'failed/' + failed['name']})['job']
    assert not due(tmp_path)
    save(s, retry)
    assert due(tmp_path) and s('deferred_poll')['status'] == 'ready'


def test_pending_expiry_and_retention_are_not_skipped_by_idle_gate(s, tmp_path, clock):
    queue_question(s)
    clock[0] += 86401
    assert due(tmp_path)
    assert s('deferred_poll')['kind'] == 'expired'
    assert not due(tmp_path)
    clock[0] += 14 * 86400
    assert due(tmp_path)
    assert s('deferred_poll')['status'] == 'idle'
    assert not due(tmp_path)


@pytest.mark.parametrize('contents', ['{invalid', '[]', '{"reminders": []}'])
def test_unreadable_snapshot_falls_back_to_existing_serialized_recovery(tmp_path, contents):
    path = tmp_path / 'private/registry.json'
    path.parent.mkdir()
    path.write_text(contents, encoding='utf-8')
    assert due(tmp_path)


def test_scheduler_still_dispatches_due_work_after_a_missed_run(endpoint, monkeypatch):
    _, calls, app = endpoint
    from core import storage as core_storage
    monkeypatch.setattr(core_storage, 'scheduled_work_due', lambda: True)
    outcomes = iter([{'status': 'sent', 'count': 1}, {'status': 'idle'}, {'status': 'idle'}])
    operations = []
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=lambda op: operations.append(op) or next(outcomes)))
    app.reminder_tick.local()
    assert operations == ['reminder_tick', 'reminder_tick', 'deferred_poll']
    assert not calls['spawn']  # Only due reminders, no comparison generation.
