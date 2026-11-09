"""F02/F03 offline lifecycle tests; all external work is mocked."""
import json
from types import SimpleNamespace
import pytest
from core import storage, requests, personal, workflow, rag, graph


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, 'index_note', lambda *a: None)
    monkeypatch.setattr(rag, 'remove_from_index', lambda *a: None)
    monkeypatch.setattr(workflow, 'send', lambda *a, **k: True)
    monkeypatch.setattr(workflow, 'react', lambda *a, **k: True)
    return lambda op, p=None: storage.dispatch(op, p, root=tmp_path)


def accept(s, source='One', **kw):
    return s('accept', {'url': f'https://instagram.com/p/{source}/', 'chat_id': 7, 'message_id': 10, 'date': 100, **kw})


def identity(job):
    return {k: job[k] for k in ('job_id', 'attempt')}


@pytest.mark.parametrize('caption,hidden', [(False, False), (True, False), (False, True), (True, True)])
def test_unicode_link_instructions(caption, hidden):
    link = 'see this' if hidden else 'https://instagram.com/p/One/'
    text = '🎬 ' + link + '\nEm português: código completo.'
    msg = {'caption' if caption else 'text': text,
        'caption_entities' if caption else 'entities': [{'type': 'text_link' if hidden else 'url',
            'url': 'https://instagram.com/p/One/', 'offset': 3, 'length': len(link)}]}
    assert requests.urls(msg) == ['https://instagram.com/p/One/']
    assert requests.source_instructions(msg) == '🎬 \nEm português: código completo.'


def test_association_reply_ambiguity_expiry_and_scope(s, monkeypatch):
    accept(s)
    accept(s, 'Two', message_id=11)
    assert s('associate', {'chat_id': 7})['url'].endswith('/Two/')
    assert s('associate', {'chat_id': 8})['status'] == 'missing'
    assert s('associate', {'chat_id': 7, 'thread_id': 1})['status'] == 'missing'
    assert s('associate', {'chat_id': 7, 'reply_id': 10})['url'].endswith('/One/')
    assert s('associate', {'chat_id': 7, 'reply_id': 999})['status'] == 'missing'
    now = storage.time.time()
    monkeypatch.setattr(storage.time, 'time', lambda: now + 86401)
    assert s('associate', {'chat_id': 7})['status'] == 'missing'
    assert s('associate', {'chat_id': 7, 'reply_id': 10})['status'] == 'resolved'


def test_superseded_claim_cache_and_charges(s):
    first = accept(s, instructions='Detailed Portuguese')['job']
    old = identity(first)
    s('claim', old)
    s('cache_put', {**old, 'evidence': {'source': 'validated'}})
    second = accept(s, instructions='Concise English', message_id=11)['job']
    assert [i['text'] for i in second['instructions']] == ['Detailed Portuguese', 'Concise English']
    assert second['instructions'][0]['message_id'] == 10
    assert second['instructions'][0]['date'] == 100
    for op, extra in [('cache_put', {'evidence': {'bad': True}}), ('save', {'metadata': {}, 'content': 'outdated'}), ('fail', {})]:
        assert s(op, {**old, **extra})['status'] == 'stale'
    assert not s('current', old)
    s('ledger', {**old, 'records': [{'record_id': 'charged', 'cost': .1}]})
    previous = s('list')['records'][0]
    assert previous['accounting'][0]['cost'] == .1
    assert s('cache_get', {'source_id': 'One'}) == {'source': 'validated'}
    assert accept(s, instructions='Concise English')['status'] == 'running'
    assert accept(s)['status'] == 'running'


def run(s, job, root):
    workflow.process(job['job_id'], job['attempt'], s, lambda _: pytest.fail('transcription'),
        SimpleNamespace(reload=lambda: None), str(root))


def test_cached_revision_failure_keeps_current_and_duplicate(s, tmp_path, monkeypatch):
    downloads, inputs = [], []
    monkeypatch.setattr(workflow, 'prepare', lambda *a: downloads.append(1) or {
        'description': 'Arrival, film', 'transcript': '', 'images': [], 'coverage': []})
    def invoke(value):
        inputs.append(value)
        return {'title': 'Films', 'category': 'entertainment', 'final_note': 'Arrival', 'review_status': 'approved'}
    monkeypatch.setattr(workflow.reel_graph, 'invoke', invoke)
    s('memory', {'command': 'natural', 'text': 'I watched Arrival.', 'message_id': 3, 'date': 90})
    first = accept(s, instructions='Portuguese')['job']
    run(s, first, tmp_path)
    assert inputs[0]['personal_memory'][0]['text'] == 'I watched Arrival.'
    assert accept(s)['status'] == 'active'
    assert accept(s, instructions='Portuguese')['status'] == 'active'
    second = accept(s, instructions='English')['job']
    monkeypatch.setattr(workflow.reel_graph, 'invoke', lambda _: (_ for _ in ()).throw(RuntimeError('provider failed')))
    run(s, second, tmp_path)
    assert len(downloads) == 1
    records = s('list')['records']
    assert len([r for r in records if r['section'] == 'notes']) == 1
    assert any(r['version_id'] == second['version_id'] and r['status'] == 'failed' for r in records)


def test_saved_note_survives_notification_mapping_failure_without_regeneration(s, tmp_path, monkeypatch, caplog):
    generated = []
    monkeypatch.setattr(workflow, 'prepare', lambda *a: {'description': 'Source', 'images': [], 'coverage': []})
    monkeypatch.setattr(workflow.reel_graph, 'invoke', lambda _: generated.append(1) or {
        'title': 'Saved source', 'category': 'general', 'final_note': 'Evidence', 'review_status': 'approved'})
    monkeypatch.setattr(workflow, 'send', lambda *a, **kw: {'success': True, 'message_ids': [99]})
    job = accept(s)['job']
    def unavailable_mapping(operation, payload=None):
        if operation == 'source_register':
            raise OSError('private raw storage details')
        return s(operation, payload)
    run(unavailable_mapping, job, tmp_path)
    assert accept(s)['status'] == 'active' and generated == [1]
    assert len([r for r in s('list')['records'] if r['section'] == 'notes']) == 1
    assert 'private raw storage details' not in caplog.text


def test_followup_during_generation_cannot_save(s, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, 'prepare', lambda *a: {'description': 'Source', 'images': [], 'coverage': []})
    def invoke(value):
        accept(s, instructions='New instructions')
        return {'title': 'Stale', 'category': 'general', 'final_note': 'Old', 'review_status': 'approved'}
    monkeypatch.setattr(workflow.reel_graph, 'invoke', invoke)
    run(s, accept(s)['job'], tmp_path)
    assert not [r for r in s('list')['records'] if r['section'] == 'notes']


def test_memory_provenance_correction_forgetting_and_restart(s, tmp_path):
    s('memory', {'command': 'natural', 'text': 'I watched Arrival.', 'date': 123, 'message_id': 44})
    memory = s('memory_get', {'question': 'Recommend a movie'})
    item = memory['items'][0]
    assert item['date'] == 123 and item['message_id'] == 44
    assert storage.dispatch('memory_get', {'question': 'Arrival'}, root=tmp_path) == memory
    reply = s('memory', {'command': 'natural', 'text': 'I have not watched Arrival.'})
    assert '/correct' in reply
    s('memory', {'command': 'correct', 'text': item['id'] + ' I have not watched Arrival.'})
    current = s('memory_get', {'question': 'movie'})
    assert len(current['items']) == 1 and current['items'][0]['id'] == item['id']
    assert 'not watched' in current['items'][0]['text']
    s('memory', {'command': 'forget', 'text': item['id']})
    assert not s('memory_get', {'question': 'Arrival'})['items']
    assert 'Usage' in s('memory', {'command': 'correct', 'text': item['id'] + ' I watched Arrival.'})
    assert not s('memory_get', {'question': 'Arrival'})['items']


def test_scopes_uncertainty_and_separation(s):
    s('memory', {'command': 'natural', 'text': 'I prefer concise notes.'})
    s('memory', {'command': 'remember', 'text': 'coding: I prefer full code examples.'})
    s('memory', {'command': 'remember', 'text': 'general: I may move to Lisbon in 2027.'})
    accept(s, instructions='Focus on code in this post')
    assert s('memory', {'command': 'natural', 'text': 'Focus on code in this post'}) is None
    assert s('memory', {'command': 'natural', 'text': 'The post says "I watched Arrival."'}) is None
    assert not personal.interpret('Did I watch Arrival?')
    notes = s('memory_get', {'question': 'code', 'scope': 'notes'})['items']
    assert len(notes) == 2
    assert not s('memory_get', {'question': 'recipe'})['items']
    assert 'may move' in s('memory_get', {'question': 'Lisbon'})['items'][0]['text']


def test_explicit_duplicates_and_scoped_correction_keep_subject(s):
    payload = {'command': 'remember', 'text': 'coding: I prefer full examples.'}
    first = s('memory', payload)
    assert 'Already remembered' in s('memory', payload)
    assert len(s('memory_get', {'question': 'code'})['items']) == 1
    s('memory', {'command': 'remember', 'text': 'I prefer concise notes.'})
    note = s('memory_get', {'scope': 'notes'})['items'][0]
    s('memory', {'command': 'correct', 'text': note['id'] + ' notes: I prefer detailed notes.'})
    assert '/correct' in s('memory', {'command': 'natural', 'text': 'I prefer concise notes.'})
    assert len(s('memory_get', {'scope': 'notes'})['items']) == 1


def test_memory_answer_grounding_and_concurrent_forget(s, monkeypatch):
    monkeypatch.setenv('RAG_JEV_ENABLED', 'false')
    monkeypatch.setattr(rag, 'retrieve', lambda *a: [])
    monkeypatch.setattr(rag, 'get_llm', lambda _: [])
    s('memory', {'command': 'natural', 'text': 'I live in Lisbon.'})
    item = s('memory_get', {'question': 'Where do I live?'})['items'][0]
    def model(chain, messages, **kwargs):
        assert 'I live in Lisbon.' in messages[1]['content']
        assert 'Personal memory is valid evidence' in messages[0]['content']
        return SimpleNamespace(content=json.dumps({'answer': 'Personal memory: Lisbon [S100000]', 'used_sources': ['S100000']}), response_metadata={})
    monkeypatch.setattr(rag, 'invoke_with_retry', model)
    result = rag.grounded_answer('Where do I live?', s)
    assert 'Lisbon' in result['answer'] and not result['source_labels']
    def concurrent(*args, **kw):
        result = model(*args, **kw)
        s('memory', {'command': 'forget', 'text': item['id']})
        return result
    monkeypatch.setattr(rag, 'invoke_with_retry', concurrent)
    assert 'changed' in rag.grounded_answer('Where do I live?', s)['answer']


def test_writer_review_receive_instructions_separately(monkeypatch):
    captured = []
    def invoke(state, prompt, step, output):
        captured.append(prompt)
        return ('{"title":"Test","markdown_content":"Test"}' if step == 'writer' else
            '{"verdict":"approved"}'), {}
    monkeypatch.setattr(graph, 'invoke', invoke)
    value = {'description': 'Untrusted source', 'instructions': [{'text': 'Research deeply in Portuguese'}], 'final_note': 'Test'}
    graph.writer_node(value)
    graph.critic_node(value)
    for prompt in captured:
        assert 'Research deeply in Portuguese' in prompt and 'External research is unavailable' in prompt
        assert 'within the requested scope' in prompt and 'do not invent a trade recommendation or date' in prompt
    assert 'Research deeply' not in graph.evidence(value)


def test_memory_failure_does_not_claim_success(s, monkeypatch):
    original = storage.atomic
    def fail(path, value):
        if str(path).endswith('registry.json'):
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(storage, 'atomic', fail)
    with pytest.raises(OSError):
        s('memory', {'command': 'remember', 'text': 'I watched Arrival.'})
    monkeypatch.setattr(storage, 'atomic', original)
    assert not s('memory_get', {'question': 'Arrival'})['items']


def test_cache_validation_changed_missing_and_outside_media(tmp_path):
    import hashlib
    image = tmp_path / 'frame.jpg'
    image.write_bytes(b'original')
    cache = {'pipeline_version': workflow.PIPELINE_VERSION, 'originating_version': 'v1',
        'images': [str(image)], 'hashes': {str(image): hashlib.sha256(b'original').hexdigest()},
        'visual_chunks': [{'start': 0, 'text': 'Arrival', 'complete': True}]}
    assert workflow.valid_cache(cache, tmp_path)
    assert not workflow.valid_cache(cache, tmp_path / 'other')
    assert not workflow.valid_cache({**cache, 'visual_chunks': [{'start': -4, 'text': 'bad'}]}, tmp_path)
    image.write_bytes(b'changed')
    assert not workflow.valid_cache(cache, tmp_path)
    image.unlink()
    assert not workflow.valid_cache(cache, tmp_path)
    assert not workflow.valid_cache({'pipeline_version': workflow.PIPELINE_VERSION}, tmp_path)


def test_memory_change_during_note_generation_blocks_publication(s, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, 'prepare', lambda *a: {'description': 'Lisbon', 'images': [], 'coverage': []})
    def invoke(value):
        s('memory', {'command': 'remember', 'text': 'I prefer concise notes.'})
        return {'title': 'Outdated', 'category': 'travel', 'final_note': 'Lisbon', 'review_status': 'approved'}
    monkeypatch.setattr(workflow.reel_graph, 'invoke', invoke)
    run(s, accept(s)['job'], tmp_path)
    records = s('list')['records']
    assert len(records) == 1 and records[0]['status'] == 'failed'
    assert 'memory changed' in records[0]['diagnostic']


def test_relevant_constraints_never_silently_truncated(s):
    for n in range(10):
        s('memory', {'command': 'remember', 'text': 'films: I watched ' + str(n) + 'x' * 850})
    with pytest.raises(ValueError, match='exceeds context budget'):
        s('memory_get', {'question': 'Recommend a film'})


def test_no_model_call_after_supersession(monkeypatch):
    from core import llm
    def cancelled():
        raise RuntimeError('superseded')
    token = llm.CALL_CONTEXT.set({'check_current': cancelled})
    try:
        with pytest.raises(RuntimeError, match='superseded'):
            llm.invoke_with_retry([SimpleNamespace(model_name='fake', invoke=lambda *a, **kw: pytest.fail('model call'))], 'x')
    finally:
        llm.CALL_CONTEXT.reset(token)


def test_instruction_limits_reject_without_overwriting(s):
    accept(s, instructions='Original')
    assert accept(s, instructions='x' * 4001)['status'] == 'instruction_limit'
    assert accept(s, instructions='Original')['status'] == 'running'


def test_research_disclosure_is_saved(s, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, 'prepare', lambda *a: {'description': 'Original', 'images': [], 'coverage': []})
    monkeypatch.setattr(workflow.reel_graph, 'invoke', lambda _: {'title': 'Research', 'category': 'general',
        'final_note': 'Source facts', 'review_status': 'approved'})
    run(s, accept(s, instructions='Do deep research')['job'], tmp_path)
    item = s('list')['records'][0]
    assert 'External research was not performed' in s('read', {'section': 'notes', 'name': item['name']})['content']


def test_partial_visual_followup_is_saved_and_repairs_only_failed_chunk(s, tmp_path, monkeypatch):
    from core import llm
    images = []
    for n in range(12):
        path = tmp_path / f'frame{n}.jpg'
        path.write_bytes(str(n).encode())
        images.append(str(path))
    prepared, seen, notifications, inputs = [], [], [], []
    monkeypatch.setattr(workflow, 'prepare', lambda *a: prepared.append(1) or {
        'description': 'Stock analysis', 'transcript': 'Buy X only if condition Y is met.',
        'images': images, 'coverage': []})
    monkeypatch.setattr(workflow, 'get_llm', lambda *a: [])
    monkeypatch.setattr(workflow, 'build_vision_messages', lambda prompt, frames: frames)
    outage = [True]
    def extract(chain, frames, **kwargs):
        seen.append(list(frames))
        if frames == images[8:12] and outage[0]:
            raise llm.ProviderChainError([('mock', TimeoutError('provider unavailable'))])
        return SimpleNamespace(content='Source text', response_metadata={})
    monkeypatch.setattr(workflow, 'invoke_with_retry', extract)
    monkeypatch.setattr(workflow, 'send', lambda chat, text, **k: notifications.append(text) or True)
    def invoke(value):
        inputs.append(value)
        return {'title': 'Stock summary', 'category': 'finance', 'final_note': 'Buy X if Y; timing not specified.',
            'review_status': 'approved'}
    monkeypatch.setattr(workflow.reel_graph, 'invoke', invoke)
    job = accept(s, instructions='Simply what to buy or sell and when')['job']
    run(s, job, tmp_path)
    note = next(r for r in s('list')['records'] if r['section'] == 'notes')
    assert note['review_status'] == 'needs_review' and note['coverage']
    assert inputs[0]['missing_media_ids'] == [9, 10, 11, 12]
    assert not inputs[0].get('raw_media_ids')  # don't force another failed vision call over a useful partial draft
    assert note['generation']['visual_failures'][0]['media_ids'] == [9, 10, 11, 12]
    assert any('Saved in Notes as Needs review' in t for t in notifications)
    assert inputs[0]['instructions'][0]['text'] == 'Simply what to buy or sell and when'
    outage[0] = False
    retry = accept(s, explicit=True)['job']
    run(s, retry, tmp_path)
    latest = next(r for r in s('list')['records'] if r['section'] == 'notes')
    assert len(prepared) == 1 and len(seen) == 4
    assert seen[-1] == images[8:12]
    assert latest['review_status'] == 'approved' and not latest['coverage']
    assert not latest['generation']['visual_failures']
    assert latest['instructions'] == job['instructions']
    assert any(r['version_id'] == note['version_id'] and r['status'] == 'superseded' for r in s('list')['records'])


def test_missing_extraction_alone_does_not_buy_another_writer_round(monkeypatch):
    calls = []
    monkeypatch.setattr(graph, 'decide', lambda *a: {'category': 'finance'})
    def invoke(state, prompt, step, output):
        calls.append(step)
        return ('{"title":"Stocks","markdown_content":"Source says buy X if Y; timing not specified."}'
            if step == 'writer' else '{"verdict":"approved","feedback":""}'), {}
    monkeypatch.setattr(graph, 'invoke', invoke)
    result = graph.build_graph().invoke({'description': 'Source', 'images': [],
        'coverage': ['Missing media 9-12'], 'instructions': [{'text': 'Simply what to buy and when'}]})
    assert calls == ['writer', 'critic']
    assert result['review_status'] == 'needs_review' and result['drafts'] == 1
