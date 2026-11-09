"""Offline comparison contracts; model fixtures do not establish live understanding."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from core import conversation, decisions, rag, storage, telegram, understanding, voice
from test_pending_reminders_and_search import paths, registry, accept
from test_webhook import endpoint, request

URL = 'https://www.instagram.com/p/DfMcWLZjz6c/'
QUESTION = 'Compare these tools with the other ones i have saved in my notes and tell me the best ones'


def turn(paths, text=QUESTION, number=11, reply=None):
    return conversation.prepare_message(text, {'text': text, 'reply_to_message': reply},
        {'chat_id': 7, 'user_id': 7, 'message_id': number, 'date': paths[1][0]}, number, paths[0])


def save(paths, job, title='GitHub Repos for Claude Video Editing', body=None, **metadata):
    identity = {k: job[k] for k in ('job_id', 'attempt')}
    paths[0]('claim', identity)
    return paths[0]('save', {**identity, 'metadata': {'title': title, 'category': 'coding', **metadata},
        'content': '# ' + title + '\n' + (body or 'ClipRepo is a repository for local video editing. Python setup is required. Cost is not documented.')})


@pytest.mark.parametrize('text', [QUESTION, 'Which of these is better than what I already saved?',
    'Contrast these repositories with my saved video tools', 'Compara estas ferramentas com as outras nas minhas notas'])
def test_clear_comparison_waits_with_original_source_without_interpretation(paths, text):
    job = accept(paths, URL, message=10)
    result = turn(paths, text)
    assert result['intent'] == 'reply' and 'once this post finishes' in result['reply']
    pending = next(iter(registry(paths)['deferred_answers'].values()))
    assert pending['source_id'] == job['source_id'] and pending['question'] == text
    assert pending['message_id'] == 11 and pending['date'] == paths[1][0] and pending['comparison']
    assert paths[0]('deferred_poll')['status'] == 'idle'
    turn(paths, text)  # Same message creates one logical pending question.
    assert len(registry(paths)['deferred_answers']) == 1


def test_saved_comparison_routes_directly_and_reply_overrides_latest_source(paths):
    first = accept(paths, URL, message=1)
    saved = save(paths, first)
    accept(paths, 'https://www.instagram.com/p/Newer/', message=5)
    for number, reply in [(11, {'message_id': 1}), (12, {'message_id': 77, 'from': {'is_bot': True}})]:
        if number == 12:
            paths[0]('source_register', {'url': URL, 'chat_id': 7, 'message_id': 77})
        result = turn(paths, number=number, reply=reply)
        assert result['intent'] == 'answer' and result['conversation']['source_url'] == URL
        assert result['conversation']['comparison']
    assert not registry(paths).get('deferred_answers')


def test_out_of_order_completion_does_not_steal_pending_target(paths):
    first = accept(paths, URL, message=10)
    turn(paths)
    second = accept(paths, 'https://www.instagram.com/p/Other/', message=20)
    save(paths, second, title='Other tool')
    assert paths[0]('deferred_poll')['status'] == 'idle'
    save(paths, first, review_status='needs_review')
    claimed = paths[0]('deferred_poll')
    assert claimed['status'] == 'ready' and claimed['job']['source_url'] == URL
    assert paths[0]('associate', {'chat_id': 7, 'message_id': 21})['source_id'] == 'Other'


def test_failed_source_retry_and_expiry_are_bounded(paths):
    first = accept(paths, URL, message=10)
    turn(paths)
    identity = {k: first[k] for k in ('job_id', 'attempt')}
    paths[0]('claim', identity)
    failed = paths[0]('fail', {**identity, 'error': 'fixture extraction failure', 'error_kind': 'media'})
    assert paths[0]('deferred_poll')['kind'] == 'source_failed'
    assert paths[0]('deferred_poll')['status'] == 'idle'
    retry = accept(paths, URL, message=12, explicit=True, previous='failed/' + failed['name'])
    save(paths, retry, review_status='unverified')
    assert paths[0]('deferred_poll')['status'] == 'ready'
    # A lost worker is not silently regenerated after a possibly charged call.
    paths[1][0] += 241
    assert paths[0]('deferred_poll')['kind'] == 'interrupted'
    assert paths[0]('deferred_poll')['status'] == 'idle'
    pending = next(iter(registry(paths)['deferred_answers'].values()))
    assert pending['attempts'] == 1 and pending['status'] == 'uncertain'


def test_pending_expiry_retains_question_and_link_without_expensive_work(paths):
    accept(paths, URL, message=10)
    turn(paths)
    paths[1][0] += 86401
    assert paths[0]('deferred_poll')['kind'] == 'expired'
    assert paths[0]('deferred_poll')['status'] == 'idle'
    job = next(iter(registry(paths)['deferred_answers'].values()))
    assert job['source_url'] == URL and job['question'] == QUESTION and job['attempts'] == 0


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'uncertain', 'retry'])
def test_scheduler_records_notice_delivery_and_only_retries_confirmed_rejection(endpoint, paths, monkeypatch, outcome):
    from core import reminders
    _, _, app = endpoint
    job = accept(paths, URL, message=10)
    turn(paths)
    paths[0]('claim', job)
    paths[0]('fail', {**job, 'error': 'fixture extraction failure'})
    sent = []
    def deliver(*args):
        # Claim is durable before HTTP, but success has not been recorded yet.
        current = next(iter(registry(paths)['deferred_answers'].values()))
        assert current['notice']['status'] == 'sending'
        sent.append(args)
        return outcome
    monkeypatch.setattr(reminders, 'deliver', deliver)
    app.reminder_tick.local()
    current = next(iter(registry(paths)['deferred_answers'].values()))
    assert current['notice']['status'] == outcome and current['attempts'] == 0
    assert sent[0][0] == 7 and URL in sent[0][1]
    app.reminder_tick.local()
    assert len(sent) == 1
    paths[1][0] += 301
    app.reminder_tick.local()
    assert len(sent) == (2 if outcome == 'retry' else 1)
    if outcome == 'retry':
        paths[1][0] += 301
        app.reminder_tick.local()
        paths[1][0] += 301
        app.reminder_tick.local()
        current = next(iter(registry(paths)['deferred_answers'].values()))
        assert len(sent) == 3 and current['notice']['status'] == 'failed'


def test_lost_notice_worker_is_uncertain_and_late_ack_cannot_overwrite_new_notice(paths):
    job = accept(paths, URL, message=10)
    turn(paths)
    paths[0]('claim', job)
    paths[0]('fail', {**job, 'error': 'fixture extraction failure'})
    first = paths[0]('deferred_poll')['job']
    assert first['notice']['status'] == 'sending'
    paths[1][0] += 241
    assert paths[0]('deferred_poll')['status'] == 'idle'
    assert next(iter(registry(paths)['deferred_answers'].values()))['notice']['status'] == 'uncertain'
    assert paths[0]('deferred_notice_done', {'id': first['id'],
        'notice_attempt': first['notice']['attempt'], 'outcome': 'sent'})['status'] == 'stale'
    paths[1][0] += 86401
    expired = paths[0]('deferred_poll')['job']
    assert expired['notice']['kind'] == 'expired'
    assert paths[0]('deferred_notice_done', {'id': first['id'],
        'notice_attempt': first['notice']['attempt'], 'outcome': 'sent'})['status'] == 'stale'
    assert paths[0]('deferred_notice_done', {'id': expired['id'],
        'notice_attempt': expired['notice']['attempt'], 'outcome': 'sent'})['status'] == 'sent'


def test_successful_source_retry_supersedes_pending_failure_notice(paths):
    job = accept(paths, URL, message=10)
    turn(paths)
    paths[0]('claim', job)
    failed = paths[0]('fail', {**job, 'error': 'fixture extraction failure'})
    notice = paths[0]('deferred_poll')['job']
    paths[0]('deferred_notice_done', {'id': notice['id'],
        'notice_attempt': notice['notice']['attempt'], 'outcome': 'retry'})
    retry = accept(paths, URL, message=12, explicit=True, previous='failed/' + failed['name'])
    saved = save(paths, retry)
    paths[1][0] += 301
    ready = paths[0]('deferred_poll')
    assert ready['status'] == 'ready' and ready['job']['focal_version'] == saved['version_id']
    assert ready['job']['notice']['status'] == 'obsolete' and ready['job']['attempts'] == 1


@pytest.mark.parametrize('deferred', [False, True])
def test_brief_delivery_does_not_discard_inflight_answers_but_completion_does(paths, monkeypatch, deferred):
    from core import reminders
    store = paths[0]
    store('reminder', {'command': 'add', 'text': 'Call dentist', 'chat_id': 7, 'message_id': 1})
    source = accept(paths, URL, message=10)
    if deferred:
        turn(paths)
        saved = save(paths, source)
        context = store('deferred_poll')['job']
        store('deferred_start', context)
    else:
        saved = save(paths, source)
        context = turn(paths)['conversation']
    revision = store('memory_revision')
    task = next(iter(registry(paths)['reminders'].values()))
    paths[1][0] = task['due']
    monkeypatch.setattr(reminders, 'deliver', lambda *a: 'sent')
    assert store('reminder_tick')['count'] == 1
    assert store('memory_revision') == revision
    result = {'answer': 'Grounded comparison.', 'memory_revision': revision, 'sources': [saved['name']]}
    operation = 'deferred_result' if deferred else 'chat_finish'
    payload = {**context, 'result': result} if deferred else {**context, **result}
    assert store(operation, payload)['status'] == ('deliverable' if deferred else 'ok')
    store('reminder', {'command': 'done', 'target': task['id'], 'chat_id': 7})
    assert store('memory_revision') > revision
    if not deferred:
        assert store(operation, payload)['status'] == 'memory_changed'


def comparison_models(paths, monkeypatch):
    comparator = save(paths, accept(paths, 'https://www.instagram.com/p/Hosted/', message=1),
        title='StudioWeb', body='StudioWeb is a hosted video editing product. It edits clips in a browser. Price is not documented.')
    unrelated = save(paths, accept(paths, 'https://www.instagram.com/p/Garden/', message=2),
        title='AI gardening', body='AI gardening forecasts soil moisture. No video editing features are documented.')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture')
    monkeypatch.setenv('RAG_JEV_ENABLED', 'true')
    def ranking(*args, **kwargs):
        supplied = kwargs['json']['state']
        assert 'video editing' in supplied['question']
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'answers': {
            k: {'noul': .95 if v['title'] == 'StudioWeb' else .1} for k, v in supplied['notes'].items()}})
    monkeypatch.setattr(decisions.requests, 'post', ranking)
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    calls = []
    def generate(chain, messages, **kwargs):
        calls.append(messages)
        evidence = json.loads(messages[1]['content'])['untrusted_notes']
        assert 'AI gardening' not in str(evidence) and 'StudioWeb' in str(evidence) and 'ClipRepo' in str(evidence)
        focal = next(k for k, v in evidence.items() if v.get('focal'))
        hosted = next(k for k, v in evidence.items() if v['title'] == 'StudioWeb')
        assert evidence[focal]['review_status'] == 'needs_review'
        prompt = messages[0]['content']
        assert 'documented cost' in prompt and 'unknown' in prompt and voice.GUIDANCE in prompt
        assert 'provisional best-for criteria' in prompt and 'setup' in prompt
        return SimpleNamespace(response_metadata={}, content=json.dumps({'answer':
            f'For browser editing, StudioWeb is the provisional choice [{hosted}].\n\n'
            f'| Tool | Setup | Cost |\n|---|---|---|\n| ClipRepo | Python required | Unknown [{focal}] |\n'
            f'| StudioWeb | Hosted browser product | Unknown [{hosted}] |\n\n'
            f'Choose ClipRepo for local setup control [{focal}]. Its source review is incomplete.',
            'used_sources': [focal, hosted]}))
    monkeypatch.setattr(rag, 'invoke_with_retry', generate)
    return comparator, unrelated, calls


def test_comparison_resumes_through_real_worker_and_cites_both_sources(endpoint, paths, monkeypatch):
    _, calls, app = endpoint
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=paths[0]))
    comparator, unrelated, generated = comparison_models(paths, monkeypatch)
    focal = accept(paths, URL, message=10)
    turn(paths)
    assert generated == []
    saved = save(paths, focal, review_status='needs_review')
    job = paths[0]('deferred_poll')['job']
    delivered = []
    monkeypatch.setattr(app, 'send_answer', lambda chat, text, sources, store: delivered.append((text, sources)) or True)
    calls['ask'](job['question'], 7, {**job, 'deferred': True})
    calls['ask'](job['question'], 7, {**job, 'deferred': True})  # duplicate background job
    assert len(generated) == len(delivered) == 1
    assert set(delivered[0][1].values()) == {saved['name'], comparator['name']}
    assert unrelated['name'] not in delivered[0][1].values()
    assert 'From your notes:' not in delivered[0][0] and 'Unknown' in delivered[0][0]
    assert registry(paths)['deferred_answers'][job['id']]['status'] == 'answered'
    payloads = []
    monkeypatch.setattr(telegram, 'call', lambda method, payload, **kw: payloads.append(payload) or {'message_id': 99})
    assert telegram.send_answer(7, *delivered[0], paths[0])
    assert payloads[0]['reply_markup']['inline_keyboard'][0][0]['text'] == 'Sources'
    assert len(paths[0]('answer_sources_get', {'chat_id': 7, 'message_id': 99})) == 2


@pytest.mark.parametrize('proposal', [{'intent': 'compare', 'confidence': .99, 'source_index': 0},
    {'intent': 'answer', 'comparison': True, 'confidence': .97, 'source_index': 0}])
def test_model_comparison_proposals_pass_actual_schema_and_context(paths, monkeypatch, proposal):
    accept(paths, URL, message=10)
    monkeypatch.setattr(decisions, 'message_intent', decisions.interpret_request)
    monkeypatch.setattr('core.llm.get_llm', lambda *a: [])
    def generate(chain, messages, **kwargs):
        context = json.loads(messages[1]['content'])
        assert context['resolved_source']['url'] == URL
        assert context['resolved_source']['source_status'] == 'accepted'
        assert context['sources'][0]['source_status'] == 'accepted'
        return SimpleNamespace(content=json.dumps(proposal), response_metadata={})
    monkeypatch.setattr('core.llm.invoke_with_retry', generate)
    result = turn(paths, 'Could you assess the strongest options here against my saved alternatives?')
    assert result['intent'] == 'reply' and 'once this post' in result['reply']
    assert next(iter(registry(paths)['deferred_answers'].values()))['comparison']


@pytest.mark.parametrize('mode,stage', [('timeout', 'interpretation_provider'), ('json', 'interpretation_json'),
    ('schema', 'interpretation_schema'), ('unknown_action', 'interpretation_schema')])
def test_interpretation_failures_have_distinct_safe_diagnostics(paths, monkeypatch, caplog, mode, stage):
    monkeypatch.setattr('core.llm.get_llm', lambda *a: [])
    def generate(*a, **kw):
        if mode == 'timeout':
            raise TimeoutError('secret raw provider prompt')
        content = '{bad' if mode == 'json' else json.dumps({'intent': 'delete_all' if mode == 'unknown_action' else 'compare',
            'confidence': .99, 'storage': 'arbitrary'})
        return SimpleNamespace(content=content, response_metadata={})
    monkeypatch.setattr('core.llm.invoke_with_retry', generate)
    result = decisions.interpret_request('Evaluate the available options', {'correlation_id': 'fixture-correlation'})
    assert result['intent'] == 'clarify' and 'stage=' + stage in caplog.text
    assert 'fixture-correlation' in caplog.text and 'secret raw' not in caplog.text
    if (paths[2] / 'private/registry.json').exists():
        assert not registry(paths).get('deferred_answers')


def test_shared_voice_defaults_reach_greeting_brief_ack_and_answer_prompt(paths):
    assert 'sir' in conversation.local_route('Hello', {})['reply']
    assert 'sir' not in voice.brief_header(True)  # Paginated briefs must not repeat the address.
    assert 'sir' not in voice.reminder_ack('tomorrow', True)
    assert 'current user style instructions' in voice.GUIDANCE
    assert voice.answer_text('Supported claim [S1].', 'Additional context.').startswith('Supported claim [S1].')


def test_exact_telegram_ingress_and_existing_scheduler_resume_once(endpoint, paths, monkeypatch):
    webhook, calls, app = endpoint
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=paths[0]))
    asyncio.run(webhook(request(URL + '?obrf=fixture')))
    asyncio.run(webhook(request(QUESTION, update=2)))
    asyncio.run(webhook(request(QUESTION, update=2)))
    assert 'once this post finishes' in calls['send'][-1][1]
    stored = registry(paths)
    assert len(stored['deferred_answers']) == len(calls['spawn']) == 1
    save(paths, next(iter(stored['jobs'].values())), review_status='needs_review')
    app.reminder_tick.local()
    app.reminder_tick.local()
    assert len(calls['spawn']) == 2
    question, chat, context = calls['spawn'][-1]
    assert question == QUESTION and context['source_url'] == URL and context['deferred']


def test_changed_source_or_memory_blocks_deferred_outdated_delivery(paths):
    job = accept(paths, URL, message=10)
    turn(paths)
    saved = save(paths, job)
    claimed = paths[0]('deferred_poll')['job']
    assert paths[0]('deferred_start', claimed)['status'] == 'started'
    paths[0]('memory', {'command': 'remember', 'text': 'I prefer concise notes.'})
    result = {'answer': 'outdated', 'memory_revision': 0, 'sources': [saved['name']]}
    assert paths[0]('deferred_result', {**claimed, 'result': result})['status'] == 'changed'
    assert paths[0]('deferred_poll')['status'] == 'idle'


def test_deferred_pruning_precedes_capacity_and_removes_old_private_results(paths):
    from core.storage import atomic
    accept(paths, URL, message=10)
    turn(paths)
    stored = registry(paths)
    template = next(iter(stored['deferred_answers'].values()))
    stored['deferred_answers'] = {str(n): {**template, 'id': str(n),
        'created': paths[1][0] - 15 * 86400, 'result': {'answer': 'old private answer'}} for n in range(100)}
    atomic(paths[2] / 'private/registry.json', stored)
    turn(paths, number=12)
    jobs = registry(paths)['deferred_answers']
    assert len(jobs) == 1 and all('result' not in j for j in jobs.values())
    # Retention also runs while idle, without needing another user request.
    paths[1][0] += 15 * 86400
    assert paths[0]('deferred_poll')['status'] == 'idle'
    assert registry(paths)['deferred_answers'] == {}


def test_late_worker_cannot_claim_or_claim_delivery_before_validation(paths):
    first = accept(paths, URL, message=10)
    turn(paths)
    save(paths, first)
    job = paths[0]('deferred_poll')['job']
    assert paths[0]('deferred_done', {**job, 'success': True})['status'] == 'stale'
    paths[1][0] += 241
    assert paths[0]('deferred_start', job)['status'] == 'stale'
    assert paths[0]('deferred_poll')['kind'] == 'interrupted'
    paths[1][0] += 86400
    assert paths[0]('deferred_poll')['status'] == 'idle'
    stored = registry(paths)['deferred_answers'][job['id']]
    assert stored['status'] == 'uncertain' and stored['attempts'] == 1


def test_expired_waiters_do_not_block_admission_before_the_tick_recovers(paths):
    from core.storage import atomic
    accept(paths, URL, message=10)
    turn(paths)
    stored = registry(paths)
    template = next(iter(stored['deferred_answers'].values()))
    stored['deferred_answers'] = {str(n): {**template, 'id': str(n),
        'created': paths[1][0] - 2 * 86400} for n in range(100)}
    atomic(paths[2] / 'private/registry.json', stored)
    assert 'once this post' in turn(paths, number=12)['reply']
    assert len(registry(paths)['deferred_answers']) == 101  # Old questions retained for expiry notices.


def test_evidence_merge_deduplicates_chunks_and_keeps_one_citation_per_note():
    focal = {'source': 'a.md', 'id': 'old', 'text': 'first chunk', 'focal': True}
    rows = rag.merge_evidence([focal], [dict(focal), {**focal, 'text': 'second chunk'},
        {'source': 'b.md', 'text': 'comparator'}])
    assert [r['id'] for r in rows] == ['S1', 'S1', 'S2']
    assert rows[0]['focal'] and len(rows) == 3


def test_topic_ack_and_answer_preserve_delivery_scope(endpoint, paths, monkeypatch):
    webhook, _, app = endpoint
    calls = []
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=paths[0]))
    monkeypatch.setattr(app, 'send', lambda chat, text, **kwargs: calls.append(kwargs) or False)
    async def body():
        payload = await request('Hello').json()
        payload['message']['message_thread_id'] = 123
        return payload
    asyncio.run(webhook(SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, json=body)))
    assert calls == [{'thread_id': 123}]  # Greeting needs no message-mapping RPC.
    payloads = []
    monkeypatch.setattr(telegram, 'call', lambda method, payload, **kw: payloads.append(payload) or {'message_id': 99})
    telegram.send(7, 'ack', thread_id=123)
    telegram.send_answer(7, 'answer', {}, paths[0], thread_id=123)
    assert all(p['message_thread_id'] == 123 for p in payloads)
