"""Live-test reproductions through ingress, registry, dynamic rendering and RAG."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core import decisions, rag, reminders, storage
from core.errors import AnswerPathError
from test_webhook import endpoint, request

LINK = 'https://www.instagram.com/reel/De7aochhik3/?dlrf=MTgwc3JkdmNjbGRraQ=='
URL = 'https://www.instagram.com/reel/De7aochhik3/'
FOLLOWUP = 'Remind me to verify this in my website'
QUESTION = 'what can i use to create videos with AI?'
SUBJECT = 'Example source: website accessibility checks'  # Synthetic evidence, not a claim about the live reel.


@pytest.fixture
def paths(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 2, 18, tzinfo=ZoneInfo('Europe/Lisbon')).timestamp()]
    monkeypatch.setattr(storage.time, 'time', lambda: now[0])
    monkeypatch.setattr(rag, 'VECTOR_DIR', str(tmp_path / 'vectors'))
    monkeypatch.setattr(rag, '_embed_passages', lambda texts: [[1.] + [0.] * 383 for _ in texts])
    monkeypatch.setattr(rag, '_embed_query', lambda q: [1.] + [0.] * 383)
    monkeypatch.setenv('RAG_JEV_ENABLED', 'false')
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('unnecessary interpretation'))
    monkeypatch.setattr(reminders, 'deliver', lambda *a: pytest.fail('unexpected delivery'))
    return lambda op, p=None: storage.dispatch(op, p, root=tmp_path), now, tmp_path


def registry(paths):
    return json.loads((paths[2] / 'private/registry.json').read_text(encoding='utf-8'))


def accept(paths, url=URL, message=1, **extra):
    return paths[0]('accept', {'url': url, 'chat_id': 7, 'user_id': 7,
        'message_id': message, 'date': paths[1][0], **extra})['job']


def finish(paths, job, title=SUBJECT, **metadata):
    identity = {k: job[k] for k in ('job_id', 'attempt')}
    paths[0]('claim', identity)
    return paths[0]('save', {**identity, 'metadata': {'title': title, 'category': 'coding', **metadata},
        'content': '# ' + title + '\n\nExampleTool makes AI videos from a script. This is offline fixture evidence.'})


def add(paths):
    paths[0]('reminder', {'command': 'add', 'text': FOLLOWUP, 'source_text': FOLLOWUP,
        'source_url': URL, 'chat_id': 7, 'user_id': 7, 'message_id': 2, 'date': paths[1][0]})
    return next(iter(registry(paths)['reminders'].values()))


def listing(paths):
    return paths[0]('reminder', {'command': 'list', 'chat_id': 7})


def test_exact_link_followup_persists_before_note_and_duplicate_webhook(endpoint, paths, monkeypatch):
    webhook, calls, app = endpoint
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=paths[0]))
    def dated(text, update=1):
        original = request(text, update=update)
        async def body():
            payload = await original.json()
            payload['message']['date'] = int(paths[1][0])
            return payload
        return SimpleNamespace(headers=original.headers, json=body)
    asyncio.run(webhook(dated(LINK)))
    asyncio.run(webhook(dated(FOLLOWUP, update=2)))
    asyncio.run(webhook(dated(FOLLOWUP, update=2)))
    stored = registry(paths)
    task = next(iter(stored['reminders'].values()))
    assert len(stored['reminders']) == len(stored['jobs']) == len(calls['spawn']) == 1
    assert task['source_id'] == 'De7aochhik3' and task['source_url'] == URL
    assert task['source_text'] == FOLLOWUP and task['text'] == 'Verify this in my website'
    assert task['user_id'] == 7 and task['message_id'] == 13 and task['date'] == int(paths[1][0])
    due = reminders.local_now(task['due'])
    assert (due.year, due.month, due.day, due.hour) == (2026, 11, 3, 9)
    pending = calls['send'][-1][1]
    assert 'linked reel' in pending and 'on my website' in pending and 'still processing' in pending
    assert SUBJECT not in pending and 'note not available' not in task['text']
    job = next(iter(stored['jobs'].values()))
    finish(paths, job, review_status='needs_review')
    assert SUBJECT in listing(paths) and 'review is incomplete' in listing(paths)
    assert 'still processing' not in listing(paths) and URL in listing(paths)
    assert registry(paths)['reminders'][task['id']]['text'] == task['text']


@pytest.mark.parametrize('change', ['done', 'reschedule', 'cancel'])
def test_resolution_after_save_preserves_task_changes_and_redo(paths, change):
    job = accept(paths)
    task = add(paths)
    payload = {'command': change, 'chat_id': 7, 'target': task['id'], 'date': paths[1][0]}
    if change == 'reschedule':
        payload['timing'] = 'on Friday'
    paths[0]('reminder', payload)
    before = registry(paths)['reminders'][task['id']]
    first = finish(paths, job)
    revised = accept(paths, instructions='Use bullet points', followup=True, message=3)
    current = finish(paths, revised, title='Updated grounded subject', review_status='unverified')
    assert current['name'] != first['name']
    assert registry(paths)['reminders'][task['id']] == before
    label = reminders.label(before, registry(paths))
    assert 'Updated grounded subject' in label and URL in label and 'on my website' in label
    assert len(registry(paths)['reminders']) == 1
    # Replayed worker completion cannot save another version or mutate the task.
    assert paths[0]('save', {'job_id': revised['job_id'], 'attempt': revised['attempt']})['status'] == 'stale'
    assert registry(paths)['reminders'][task['id']] == before


def test_failed_extraction_retry_missing_deleted_and_out_of_order_sources(paths, monkeypatch):
    a = accept(paths)
    task = add(paths)
    b = accept(paths, 'https://www.instagram.com/p/OtherSource/', message=3)
    finish(paths, b, title='Unrelated source B')
    assert 'Unrelated source B' not in listing(paths) and 'still processing' in listing(paths)
    identity = {k: a[k] for k in ('job_id', 'attempt')}
    paths[0]('claim', identity)
    failure = paths[0]('fail', {**identity, 'error_kind': 'media', 'error': 'Fixture extraction failure'})
    assert 'processing failed' in listing(paths) and URL in listing(paths)
    retry = accept(paths, explicit=True, previous='failed/' + failure['name'], message=4)
    assert 'still processing' in listing(paths)
    note = finish(paths, retry)
    assert SUBJECT in listing(paths) and 'processing failed' not in listing(paths)
    assert registry(paths)['reminders'][task['id']]['due'] == task['due']
    monkeypatch.setattr(rag, 'remove_from_index', lambda *a: None)
    paths[0]('delete', {'records': [{'section': 'notes', 'name': note['name']}]})
    assert 'missing or was deleted' in listing(paths) and URL in listing(paths)


def test_current_note_resolution_and_brief_need_no_ai_or_extraction(paths, monkeypatch):
    job = accept(paths)
    task = add(paths)
    finish(paths, job, review_status='needs_review')
    monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **k: pytest.fail('title resolution called AI'))
    monkeypatch.setattr('core.workflow.prepare', lambda *a, **k: pytest.fail('title resolution extracted media'))
    paths[1][0] = task['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or 'sent')
    assert paths[0]('reminder_tick')['count'] == 1
    assert SUBJECT in sent[0] and URL in sent[0] and 'on my website' in sent[0]
    paths[1][0] += 86400
    assert paths[0]('reminder_tick')['count'] == 1  # Delivery remains independent of completion.


def test_existing_placeholder_repair_preview_does_not_reset_state(paths):
    from core.storage import atomic
    accept(paths)
    task = add(paths)
    stored = registry(paths)
    item = stored['reminders'][task['id']]
    item.update(text='Verify the linked Instagram source (note not available) in my website',
        status='notified', delivery={'history': [{'date': paths[1][0], 'outcome': 'sent'}]})
    atomic(paths[2] / 'private/registry.json', stored)
    preview = paths[0]('repair_personal_data')
    assert preview['findings'][0]['action'] == 'restore_source_reference'
    assert registry(paths) == stored
    paths[0]('repair_personal_data', {'apply': True})
    repaired = registry(paths)['reminders'][task['id']]
    assert repaired['text'] == 'Verify this in my website' and repaired['source_text'] == FOLLOWUP
    assert all(repaired[k] == item[k] for k in ('status', 'due', 'delivery', 'source_id', 'message_id'))
    assert repaired['repair_history'][0]['previous_text'] == item['text']
    assert not paths[0]('repair_personal_data')['changed']


def model_answer(monkeypatch, *, invalid=False):
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    def model(chain, messages, **kw):
        notes = json.loads(messages[1]['content'])['untrusted_notes']
        note_id = next((k for k, v in notes.items() if 'ExampleTool' in str(v)), 'S1')
        if invalid:
            note_id = 'S999'
        return SimpleNamespace(response_metadata={}, content=json.dumps({'answer': f'ExampleTool makes script-based videos [{note_id}].',
            'used_sources': [note_id], 'general_knowledge': ''}))
    monkeypatch.setattr(rag, 'invoke_with_retry', model)


def test_ai_video_question_real_index_memory_recall_and_citations(paths, monkeypatch):
    note = finish(paths, accept(paths), title='AI video creation fixture')
    paths[0]('memory', {'command': 'remember', 'text': 'I prefer concise notes.'})
    paths[0]('recall', {'source_url': URL, 'topic': 'AI video creation', 'source_text': 'Bring this up when I ask about AI video creation'})
    model_answer(monkeypatch)
    result = rag.grounded_answer(QUESTION, paths[0], allow_general=True)
    assert result['sources'] == [note['name']] and result['source_labels'] == {'S1': note['name']}
    assert 'ExampleTool' in result['answer'] and result['correlation_id']


def test_real_retrieval_no_relevant_evidence_is_not_failure(paths, monkeypatch):
    finish(paths, accept(paths))
    monkeypatch.setattr(rag, '_embed_query', lambda q: [0., 1.] + [0.] * 382)
    monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **k: pytest.fail('no evidence requires no AI'))
    result = rag.grounded_answer(QUESTION, paths[0])
    assert result['sources'] == [] and "couldn't find" in result['answer']


@pytest.mark.parametrize('operation,stage', [('recall_get', 'topic_recall'), ('retrieve', 'retrieval'),
    ('memory_get', 'personal_memory'), ('list', 'source_visibility'), ('memory_revision', 'state_freshness')])
def test_private_stage_diagnostics_never_log_private_error_text(paths, monkeypatch, caplog, operation, stage):
    finish(paths, accept(paths))
    model_answer(monkeypatch)
    def call(op, payload=None):
        if op == operation:
            raise OSError('SECRET-PROMPT website.example credential-private')
        return paths[0](op, payload)
    with pytest.raises(AnswerPathError) as caught:
        rag.grounded_answer(QUESTION, call)
    error = caught.value
    assert error.stage == stage and error.error_type == 'OSError'
    assert error.correlation_id in caplog.text and 'SECRET-PROMPT' not in caplog.text
    assert 'website.example' not in caplog.text and 'credential-private' not in str(error)


@pytest.mark.parametrize('failure,expected', [('provider', 'TimeoutError'), ('invalid', 'AnswerValidationError')])
def test_answer_failure_is_not_no_evidence(paths, monkeypatch, failure, expected):
    finish(paths, accept(paths))
    model_answer(monkeypatch, invalid=True)
    if failure == 'provider':
        monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError('private provider input')))
    with pytest.raises(AnswerPathError) as caught:
        rag.grounded_answer(QUESTION, paths[0])
    assert caught.value.stage == 'answer_generation' and caught.value.error_type == expected


def test_missing_index_for_saved_notes_is_a_retrieval_failure(paths):
    from core.storage import atomic
    # Save normally, then point at a missing table without deleting any data.
    finish(paths, accept(paths))
    atomic(paths[2] / 'vectors/active.json', {'table': 'not-present'})
    with pytest.raises(AnswerPathError) as caught:
        rag.grounded_answer(QUESTION, paths[0])
    assert caught.value.stage == 'retrieval' and caught.value.error_type == 'IndexUnavailableError'


@pytest.mark.parametrize('operation,stage', [('source_context', 'linked_source'), ('memory_sources', 'linked_memory')])
def test_linked_evidence_failures_are_not_silent_empty_results(paths, monkeypatch, operation, stage):
    finish(paths, accept(paths))
    if operation == 'source_context':
        paths[0]('recall', {'source_url': URL, 'topic': 'AI videos', 'source_text': 'Use this when I ask about AI videos'})
    else:
        paths[0]('memory', {'command': 'remember', 'text': 'I want to use this',
            'source_url': URL, 'source_text': 'I want to use this', 'chat_id': 7, 'date': paths[1][0]})
    def call(op, payload=None):
        if op == operation:
            raise OSError('Fixture linked evidence failure')
        return paths[0](op, payload)
    with pytest.raises(AnswerPathError) as caught:
        rag.grounded_answer(QUESTION, call)
    assert caught.value.stage == stage


def test_client_configuration_failure_does_not_trigger_a_second_paid_chain(paths, monkeypatch):
    finish(paths, accept(paths))
    calls = []
    def client(*a):
        calls.append(1)
        raise ValueError('Fixture invalid client configuration')
    monkeypatch.setattr(rag, 'get_llm', client)
    with pytest.raises(AnswerPathError) as caught:
        rag.grounded_answer(QUESTION, paths[0])
    assert caught.value.error_type == 'ValueError' and calls == [1]


def test_telegram_search_failure_has_private_correlation_and_no_raw_error(endpoint, paths, monkeypatch, caplog):
    webhook, calls, app = endpoint
    def call(op, payload=None):
        if op == 'retrieve':
            raise OSError('secret provider prompt')
        return paths[0](op, payload)
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=call))
    asyncio.run(webhook(request(QUESTION)))
    question, chat, context = calls['spawn'][0]
    calls['ask'](question, chat, context)
    assert "couldn't search your notes" in calls['send'][-1][1]
    assert 'stage=retrieval' in caplog.text and 'correlation=' in caplog.text
    assert 'secret provider prompt' not in caplog.text and 'OSError' not in calls['send'][-1][1]
