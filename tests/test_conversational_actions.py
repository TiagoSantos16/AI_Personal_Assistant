"""Offline integration contracts; interpretation mocks are not live-model evaluations."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from core import conversation, decisions, graph, legacy_repair, rag, reminders, storage, understanding

ZONE = ZoneInfo('Europe/Lisbon')
URLS = [f'https://www.instagram.com/p/{code}/' for code in ('DeUpDUwDID-', 'De_-cj8jQjN', 'DefKpA4kWow')]


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 24, 18, tzinfo=ZONE).timestamp()]
    monkeypatch.setattr(storage.time, 'time', lambda: now[0])
    monkeypatch.setattr(rag, 'index_note', lambda *a: None)
    monkeypatch.setattr(rag, 'retrieve', lambda *a: [])
    monkeypatch.setattr(rag, 'VECTOR_DIR', str(tmp_path / 'no-vectors'))
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('unexpected interpretation'))
    monkeypatch.setattr(reminders, 'deliver', lambda *a: pytest.fail('unexpected delivery'))
    call = lambda op, p=None: storage.dispatch(op, p, root=tmp_path)
    return call, now, tmp_path


def turn(env, text, number=2, reply=None, date=None):
    call, now, _ = env
    return conversation.prepare_message(text, {'text': text, 'reply_to_message': reply},
        {'chat_id': 7, 'user_id': 7, 'message_id': number, 'date': date or now[0]}, number, call)


def link(env, index=0, number=1):
    call, now, _ = env
    return call('accept', {'url': URLS[index], 'chat_id': 7, 'user_id': 7, 'message_id': number,
        'date': now[0], 'update_id': number})['job']


def save(env, job, title):
    call = env[0]
    identity = {k: job[k] for k in ('job_id', 'attempt')}
    call('claim', identity)
    return call('save', {**identity, 'metadata': {'title': title, 'category': 'general'},
        'content': '# ' + title + '\n\nFaceless cooking channels are one niche idea.'})


def state(env):
    return json.loads((env[2] / 'private/registry.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('wording', ['Remind these when i ask for niche ideas',
    'Remember these when i ask for niches', 'Bring this up when I look for niche ideas',
    'Guarda isto para quando eu pedir ideias de nichos'])
def test_a_recall_before_completion_and_actual_evidence_after(env, monkeypatch, wording):
    job = link(env)
    assert 'bring up' in turn(env, wording)['reply']
    assert not state(env)['memory']['items'] and not state(env).get('reminders')
    rule = next(iter(state(env)['recalls'].values()))
    assert rule['request'] == wording and rule['message_id'] == 2 and rule['user_id'] == 7
    note = save(env, job, 'Faceless YouTube Channel Ideas')
    seen = []
    def answer(q, rows, **kw):
        seen.extend(rows)
        return {'answer': 'Cooking channels [S1]', 'sources': [note['name']], 'source_labels': {'S1': note['name']}}
    monkeypatch.setattr(rag, 'answer', answer)
    result = rag.grounded_answer('Dá-me ideias de nichos', env[0])
    assert result['sources'] == [note['name']]
    assert any('cooking channels' in row['text'] for row in seen)
    assert all(row['source'] == note['name'] and row['recalled'] for row in seen)
    assert env[0]('recall_get', {'question': 'dinner recipes'})['rules'] == []


def test_b_task_bound_while_processing_and_due_when_failed(env, monkeypatch):
    job = link(env, 1)
    text = 'Remind me to search this to see if it is real'
    reply = turn(env, text)['reply']
    assert URLS[1] in reply and 'morning brief' in reply
    item = next(iter(state(env)['reminders'].values()))
    assert item['source_id'] == job['source_id'] and item['source_text'] == text
    save(env, job, 'Boomer ATM Machine side hustle offer')
    assert 'Boomer ATM' in env[0]('reminder', {'command': 'list', 'chat_id': 7})
    env[1][0] = item['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or 'sent')
    env[0]('reminder_tick')
    assert 'Search Boomer ATM' in sent[0] and URLS[1] in sent[0]
    assert 'Cancelled' in turn(env, 'Cancel the ATM investigation', 3)['reply']


@pytest.mark.parametrize('stamp', ['2026-03-28T23:58:00', '2026-10-24T23:58:00', '2026-10-25T00:02:00'])
def test_c_tomorrow_uses_message_date_not_worker_date(env, stamp):
    original = datetime.fromisoformat(stamp).replace(tzinfo=ZONE)
    env[1][0] = original.timestamp()
    job = link(env, 2)
    env[1][0] += 7200
    assert 'morning brief' in turn(env, 'Tell me that again tomorrow', date=original.timestamp())['reply']
    item = next(iter(state(env)['reminders'].values()))
    due = reminders.local_now(item['due'])
    assert due.date() == original.date() + timedelta(days=1) and due.hour == 9
    assert item['source_id'] == job['source_id'] and item['kind'] == 'notification'


def test_d_compound_work_stays_open_across_briefs_and_partial_completion(env, monkeypatch):
    text = 'Remind me to install skill inspector from nvidia (and test all skills i have installed), and security audit skill from cloudflare'
    turn(env, text)
    tasks = list(state(env)['reminders'].values())
    assert len(tasks) == 3 and all(i['source_text'] == text for i in tasks)
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or {'status': 'sent', 'message_id': 77 + len(sent)})
    env[1][0] = tasks[0]['due']
    assert env[0]('reminder_tick')['count'] == 3
    assert env[0]('reminder_tick')['status'] == 'idle'
    result = turn(env, "I installed it, but haven't tested the skills yet.", 3)['reply']
    assert 'Which reminder' in result  # Two different installations: never guess.
    result = turn(env, 'Done with the NVIDIA skill inspector task', 4)['reply']
    assert 'Marked as done' in result
    env[1][0] += 86400
    assert env[0]('reminder_tick')['count'] == 2
    assert 'test all skills' in sent[-1].lower() and 'cloudflare' in sent[-1] and 'nvidia' not in sent[-1]
    assert 'Cancelled' in turn(env, 'Cancel the cloudflare task', 5)['reply']


def test_latest_link_and_old_replies_survive_out_of_order_completion_and_redo(env):
    a = link(env, 0, 1)
    b = link(env, 1, 3)
    save(env, b, 'ATM offer')
    save(env, a, 'YouTube ideas')
    assert 'bring up' in turn(env, 'Remember these when I ask for niches', 4)['reply']
    assert next(iter(state(env)['recalls'].values()))['source_id'] == b['source_id']
    env[0]('source_register', {'url': URLS[0], 'chat_id': 7, 'message_id': 9})
    turn(env, 'Remember these when I ask about channels', 10, reply={'message_id': 9, 'from': {'is_bot': True}})
    assert list(state(env)['recalls'].values())[-1]['source_id'] == a['source_id']
    turn(env, 'Tell me that again tomorrow', 11, reply={'message_id': 1})
    assert next(iter(state(env)['reminders'].values()))['source_id'] == a['source_id']
    revised = env[0]('accept', {'url': URLS[0], 'instructions': 'Use bullets', 'followup': True, 'chat_id': 7, 'message_id': 12})['job']
    save(env, revised, 'Updated channel ideas')
    assert 'Updated channel ideas' in env[0]('memory', {'command': 'memory'})
    assert env[0]('associate', {'chat_id': 7, 'message_id': 13})['source_id'] == b['source_id']


def test_old_brief_number_mapping_snooze_and_notification_not_recurring(env, monkeypatch):
    turn(env, 'Remind me to call dentist')
    turn(env, 'Remind me to renew passport', 3)
    tasks = list(state(env)['reminders'].values())
    env[1][0] = tasks[0]['due']
    monkeypatch.setattr(reminders, 'deliver', lambda *a: {'status': 'sent', 'message_id': 77})
    env[0]('reminder_tick')
    mapping = state(env)['reminder_messages']['7:0:77']['numbers']
    turn(env, 'Remind me to buy milk', 4)
    env[1][0] += 2 * 86400
    assert 'Marked as done' in turn(env, 'Done with 1', 5, reply={'message_id': 77})['reply']
    assert state(env)['reminders'][mapping['1']]['status'] == 'done'
    assert 'morning brief' in turn(env, 'Remind me again on Friday', 6,
        reply={'message_id': 77})['reply']
    assert reminders.local_now(state(env)['reminders'][mapping['2']]['due']).weekday() == 4


def test_duplicates_failure_and_stale_interpretation_never_mutate_junk(env, monkeypatch):
    link(env)
    text = 'Remember these when I ask for niches'
    turn(env, text)
    turn(env, text)
    assert len(state(env)['recalls']) == 1
    monkeypatch.setattr(decisions, 'message_intent', decisions.interpret_request)
    monkeypatch.setattr('core.llm.get_llm', lambda models: models)
    monkeypatch.setattr('core.llm.invoke_with_retry', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    assert 'service is unavailable' in turn(env, 'Perhaps keep this around somehow', 3)['reply']
    assert not state(env)['memory']['items']
    with pytest.raises(ValueError):
        understanding.validated({'intent': 'task', 'confidence': .99, 'storage': 'delete'}, text, {})
    with pytest.raises(ValueError):
        understanding.validated({'intent': 'recall', 'confidence': .99, 'source_index': 5, 'topic': 'niches'}, text, {'sources': []})


def test_legacy_preview_only_uses_unambiguous_original_provenance(env):
    link(env)
    old = state(env)
    old['memory']['items'] = {'bad': {'id': 'bad', 'text': 'these when i ask for niches', 'kind': 'fact',
        'scope': 'general', 'chat_id': 7, 'message_id': 2, 'date': env[1][0]},
        'unknown': {'id': 'unknown', 'text': 'I need to try this', 'kind': 'fact', 'scope': 'general'}}
    before = json.dumps(old, sort_keys=True)
    report = legacy_repair.repair(old)
    assert json.dumps(old, sort_keys=True) == before
    assert {f['action'] for f in report['findings']} == {'migrated_recall', 'needs_clarification'}
    legacy_repair.repair(old, apply=True)
    assert old['memory']['items']['bad']['active'] is False
    assert old['memory']['items']['unknown']['repair_status'] == 'needs_clarification'
    assert len(old['recalls']) == 1


def test_review_failure_keeps_safe_diagnostic_and_fenced_json_works(monkeypatch):
    monkeypatch.setattr(graph, 'invoke', lambda *a: (_ for _ in ()).throw(TimeoutError('review deadline')))
    result = graph.critic_node({'final_note': 'Draft'})
    assert result['review_status'] == 'unverified' and 'TimeoutError' in result['critique_history'][0]
    monkeypatch.setattr(graph, 'invoke', lambda *a: ('\n```json\n{"verdict":"approved","feedback":"","media_ids":[]}\n```\n', {}))
    assert graph.critic_node({'final_note': 'Draft'})['review_status'] == 'approved'


def test_notifications_are_one_off_but_tasks_recur_after_unknown_delivery(env, monkeypatch):
    link(env)
    turn(env, 'Tell me that again tomorrow')
    turn(env, 'Remind me to call the dentist', 3)
    tasks = list(state(env)['reminders'].values())
    env[1][0] = tasks[0]['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or 'uncertain')
    env[0]('reminder_tick')
    assert env[0]('reminder_tick')['status'] == 'idle'
    env[1][0] += 86400
    assert env[0]('reminder_tick')['count'] == 1
    assert 'dentist' in sent[-1] and URLS[0] not in sent[-1]
    assert all(i['status'] != 'done' for i in state(env)['reminders'].values())


def test_unavailable_source_task_delivers_link_and_later_gains_title(env, monkeypatch):
    job = link(env, 1)
    turn(env, 'Remind me to search this to see if it is real')
    env[1][0] = next(iter(state(env)['reminders'].values()))['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or 'sent')
    env[0]('reminder_tick')
    assert URLS[1] in sent[-1] and 'Search this' not in sent[-1]
    save(env, job, 'Boomer ATM offer')
    env[1][0] += 86400
    env[0]('reminder_tick')
    assert 'Boomer ATM offer' in sent[-1]


def test_personal_fact_and_source_instruction_do_not_become_recall_or_tasks(env):
    job = link(env)
    assert 'Remembered' in turn(env, 'I watched Arrival')['reply']
    assert turn(env, 'Use bullet points for this post', 3)['intent'] == 'revise'
    assert not state(env).get('recalls') and not state(env).get('reminders')
    item = next(iter(state(env)['memory']['items'].values()))
    assert item['scope'] == 'films' and 'source_id' not in item
    assert env[0]('memory_get', {'question': 'Recommend a movie'})['items'][0]['id'] == item['id']


def test_targeted_missing_reference_and_explicit_topic_change(env):
    link(env)
    turn(env, 'What is a neutron?', 3)
    result = turn(env, 'Remember these when I ask for niches', 4)['reply']
    assert 'Which source' in result and URLS[0] in result
    assert not state(env).get('recalls')
    result = turn(env, 'the first one', 5)['reply']
    assert 'bring up' in result
    rule = next(iter(state(env)['recalls'].values()))
    assert rule['message_id'] == 4  # clarification preserves original provenance


def test_named_reference_beats_focus_and_expired_context_asks(env):
    a = link(env, 0, 1)
    save(env, a, 'YouTube channel niches')
    link(env, 1, 3)
    result = turn(env, 'Remember the YouTube channel post when I ask for niches', 4)['reply']
    assert 'YouTube' in result
    assert next(iter(state(env)['recalls'].values()))['source_id'] == a['source_id']
    env[1][0] += 86401
    result = turn(env, 'Tell me that again tomorrow', 5)['reply']
    assert 'Which source' in result and 'YouTube' in result
    assert not state(env).get('reminders')


def test_legacy_compound_and_notified_tasks_recover_without_reviving_done(env):
    turn(env, 'Remind me to install NVIDIA inspector and test all skills')
    old = state(env)
    tasks = list(old['reminders'].values())
    parent = tasks[0]
    parent.update(text='Install NVIDIA inspector and test all skills', status='notified', retry_after=env[1][0] + 300)
    old['reminders'] = {parent['id']: parent, 'closed': {**tasks[1], 'id': 'closed', 'status': 'done'}}
    report = legacy_repair.repair(old, apply=True)
    assert 'split_into_tasks' in [f['action'] for f in report['findings']]
    assert old['reminders']['closed']['status'] == 'done'
    assert len(old['reminders'][parent['id']]['child_ids']) == 2
    assert len(old['reminders']) == 4


def test_delayed_proposal_cannot_resurrect_forgotten_recall_or_attach_new_source(env, monkeypatch):
    link(env)
    turn(env, 'Remember these when I ask for niches')
    def interpret(*a):
        env[0]('recall_forget', {'text': 'niches'})
        return {'intent': 'recall', 'topic': 'niches'}
    monkeypatch.setattr(decisions, 'message_intent', interpret)
    result = turn(env, 'Bring this back for the same topic later', 3)['reply']
    assert 'changed' in result
    assert not any(r['active'] for r in state(env)['recalls'].values())


def test_semantic_recall_uses_existing_local_embeddings_without_exact_words(env, monkeypatch):
    link(env)
    turn(env, 'Remember these when I ask for niches')
    monkeypatch.setattr(rag, '_model', object())
    from core import recall
    monkeypatch.setattr(recall, '_vector', lambda text: (1., 0.))
    assert env[0]('recall_get', {'question': 'Suggest a specialized audience to build a channel for'})['rules']


def test_primary_interpretation_is_bounded_validated_and_no_paid_fallback(env, monkeypatch):
    link(env)
    monkeypatch.setattr(decisions, 'message_intent', decisions.interpret_request)
    seen = []
    from core.config import BASIC_MODELS
    monkeypatch.setattr('core.llm.get_llm', lambda models: seen.append(models) or models)
    def model(chain, messages, **kwargs):
        assert kwargs['output'] == 600 and kwargs['structured']
        data = json.loads(messages[1]['content'])
        assert data['sources'][0]['url'] == URLS[0]
        return SimpleNamespace(content=json.dumps({'intent': 'task', 'confidence': .99,
            'source_index': 0, 'description': 'Check whether this offer is legitimate'}), response_metadata={})
    monkeypatch.setattr('core.llm.invoke_with_retry', model)
    result = turn(env, 'Could you put checking this offer on my things to do?', 3)['reply']
    assert 'morning brief' in result and seen == [BASIC_MODELS[:1]]
    assert next(iter(state(env)['reminders'].values()))['source_id'] == 'DeUpDUwDID-'


def test_action_acknowledgement_reply_uses_that_tasks_mapping(env):
    turn(env, 'Remind me to call dentist')
    env[0]('action_message_register', {'chat_id': 7, 'origin_message_id': 2, 'message_id': 77})
    turn(env, 'Remind me to renew passport', 3)
    assert 'dentist' in turn(env, 'That one is finished', 4, reply={'message_id': 77})['reply']
    assert next(i for i in state(env)['reminders'].values() if 'passport' in i['text'])['status'] == 'pending'


def test_recall_evidence_citations_follow_note_redo(env, monkeypatch):
    job = link(env)
    turn(env, 'Remember these when I ask for niches')
    first = save(env, job, 'Original niches')
    revised = env[0]('accept', {'url': URLS[0], 'instructions': 'Use bullet points', 'explicit': True})['job']
    current = save(env, revised, 'Updated niches')
    monkeypatch.setenv('RAG_JEV_ENABLED', 'false')
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **k: SimpleNamespace(content=json.dumps({
        'answer': 'Cooking channels [S1]', 'used_sources': ['S1']}), response_metadata={}))
    result = rag.grounded_answer('Niche ideas?', env[0])
    assert result['sources'] == [current['name']] and first['name'] not in result['sources']
    assert result['source_labels'] == {'S1': current['name']}


def test_task_changes_do_not_invalidate_note_memory_fingerprint(env):
    job = link(env)
    fingerprint = env[0]('memory_get', {'question': 'Some evidence', 'scope': 'notes'})['fingerprint']
    turn(env, 'Remind me to call dentist')
    turn(env, 'Show my reminders', 3)
    assert fingerprint == env[0]('memory_get', {'question': 'Some evidence', 'scope': 'notes'})['fingerprint']
    identity = {k: job[k] for k in ('job_id', 'attempt')}
    env[0]('claim', identity)
    assert env[0]('save', {**identity, 'metadata': {'title': 'Saved', 'category': 'general'},
        'content': '# Saved', 'memory_query': 'Some evidence', 'memory_fingerprint': fingerprint})['status'] == 'saved'


def test_replayed_task_creation_cannot_duplicate_failed_or_resurrect_completed_work(env):
    from core.storage import atomic
    payload = {'command': 'add', 'text': 'Call dentist', 'source_text': 'Remind me to call dentist',
        'chat_id': 7, 'message_id': 2, 'date': env[1][0]}
    env[0]('reminder', payload)
    stored = state(env)
    item = next(iter(stored['reminders'].values()))
    item['status'] = 'failed'
    atomic(env[2] / 'private/registry.json', stored)
    env[0]('reminder', payload)
    assert len(state(env)['reminders']) == 1
    env[0]('reminder', {'command': 'done', 'target': item['id'], 'chat_id': 7})
    assert "haven't scheduled it again" in env[0]('reminder', payload)
    assert len(state(env)['reminders']) == 1 and state(env)['reminders'][item['id']]['status'] == 'done'


def test_restart_after_uncertain_yesterday_send_keeps_today_open_task_eligible(env, monkeypatch):
    from core.storage import atomic
    turn(env, 'Remind me to call dentist')
    stored = state(env)
    item = next(iter(stored['reminders'].values()))
    env[1][0] = item['due']
    item.update(status='sending', delivery={'day': reminders.local_now(item['due']).date().isoformat(), 'history': []})
    atomic(env[2] / 'private/registry.json', stored)
    env[1][0] += 86400
    monkeypatch.setattr(reminders, 'deliver', lambda *a: 'sent')
    assert env[0]('reminder_tick')['count'] == 1
    assert env[0]('reminder_tick')['status'] == 'idle'


def test_recall_punctuation_cannot_create_or_forget_an_association(env):
    link(env)
    turn(env, 'Remember these when I ask for niches')
    before = state(env)['recalls']
    assert 'Which topic' in env[0]('recall_forget', {'text': '!!!'})
    assert state(env)['recalls'] == before
    with pytest.raises(ValueError):
        env[0]('recall', {'source_url': URLS[0], 'topic': '!!!'})


def test_inactive_memory_cannot_be_used_as_linked_evidence(env):
    from core.storage import atomic
    job = link(env)
    turn(env, 'I need to try this')
    save(env, job, 'A linked tool')
    stored = state(env)
    item = next(iter(stored['memory']['items'].values()))
    item['active'] = False
    atomic(env[2] / 'private/registry.json', stored)
    assert env[0]('memory_sources', {'ids': [item['id']]}) == []


def test_repairing_an_already_flagged_ambiguous_item_does_not_revise_memory(env):
    from core.storage import atomic
    env[0]('memory', {'command': 'remember', 'text': 'I live in Lisbon.'})
    stored = state(env)
    item = next(iter(stored['memory']['items'].values()))
    item.update(text='I need to try this', repair_status='needs_clarification')
    atomic(env[2] / 'private/registry.json', stored)
    revision = env[0]('memory_revision')
    result = env[0]('repair_personal_data', {'apply': True})
    assert result['findings'][0]['action'] == 'needs_clarification'
    assert not result['changed'] and env[0]('memory_revision') == revision
