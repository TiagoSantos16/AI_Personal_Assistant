"""Personal intentions retain their referent without copying source claims into memory."""
import json
from types import SimpleNamespace

import pytest

from core import conversation, decisions, personal, rag, storage


URL = 'https://instagram.com/reel/DeON0MOB5oi/'


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, 'index_note', lambda *a: None)
    return lambda op, p=None: storage.dispatch(op, p, root=tmp_path)


def turn(s, text, update=2, **extra):
    return conversation.prepare_message(text, {'text': text, 'message_id': update, **extra},
        {'chat_id': 7, 'message_id': update, 'date': 1792970728}, update, s)


def link(s, url=URL, message_id=1):
    return s('accept', {'url': url, 'chat_id': 7, 'message_id': message_id})['job']


def save(s, job, title='API Anything: Website to API Reverse Engineering'):
    identity = {k: job[k] for k in ('job_id', 'attempt')}
    s('claim', identity)
    return s('save', {**identity, 'metadata': {'title': title, 'category': 'coding'},
        'content': '# ' + title + '\n\nAPI Anything turns websites into APIs.'})


def test_intention_before_note_finishes_resolves_on_read_without_ai(s, monkeypatch):
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('no routing model needed'))
    job = link(s)
    reply = turn(s, 'I need to try this')['reply']
    assert 'Remembered:' in reply and URL.replace('instagram.com', 'www.instagram.com') in reply
    assert 'not available' in reply and 'morning brief' in reply
    before = s('memory_snapshot')
    item = before['items'][0]
    assert item['kind'] == 'intention' and item['action'] == 'try'
    assert item['text'] == item['source_text'] == 'I need to try this'
    assert item['message_id'] == 2 and item['date'] == 1792970728
    assert item['source_id'] == 'DeON0MOB5oi'
    saved = save(s, job)
    listing = s('memory', {'command': 'memory'})
    assert 'API Anything' in listing and '2026-' in listing and '1792970728' not in listing
    found = s('memory_get', {'question': 'What did I want to experiment with?'})['items']
    assert found[0]['source_name'] == saved['name']
    assert 'I need to try API Anything' in found[0]['text']
    assert s('memory_snapshot') == before  # no late background mutation or new personal inference


@pytest.mark.parametrize('scenario', ['missing', 'expired'])
def test_contextual_intention_requires_correct_source(s, monkeypatch, scenario):
    if scenario != 'missing':
        link(s)
    if scenario == 'expired':
        now = storage.time.time()
        monkeypatch.setattr(storage.time, 'time', lambda: now + 86401)
    assert 'reply to its Instagram link' in turn(s, 'I need to try this', update=3)['reply']
    assert not s('memory_snapshot')['items']
    if scenario != 'missing':
        result = turn(s, 'I need to try this', update=4, reply_to_message={'message_id': 1})
        assert 'Remembered:' in result['reply']


def test_pending_confirmation_preserves_original_reference_and_provenance(s, monkeypatch):
    link(s)
    # Exercise an earlier uncertain interpretation followed by explicit confirmation.
    monkeypatch.setattr(conversation, 'route', lambda *a: {'intent': 'clarify', 'text': 'I need to try this'})
    turn(s, 'I need to try this')
    monkeypatch.setattr(conversation, 'route', lambda text, ctx: conversation.local_route(text, ctx))
    reply = turn(s, 'Remember', update=3)['reply']
    assert 'Remembered:' in reply
    item = s('memory_snapshot')['items'][0]
    assert item['source_id'] == 'DeON0MOB5oi'
    assert item['source_text'] == 'I need to try this' and item['message_id'] == 2


def test_reply_beats_recent_context_and_memory_does_not_revise_note(s):
    first = link(s)
    link(s, 'https://instagram.com/p/Other/', 3)
    result = turn(s, 'I want to use this soon', update=4, reply_to_message={'message_id': 1})
    assert 'Remembered:' in result['reply']
    assert s('memory_snapshot')['items'][0]['source_id'] == first['source_id']
    assert s('accept', {'url': URL})['status'] == 'running'
    assert s('claim', {k: first[k] for k in ('job_id', 'attempt')})['job']['instructions'] == []
    revision = s('memory_revision')
    assert 'Already remembered' in turn(s, 'I want to use this soon', update=5,
        reply_to_message={'message_id': 1})['reply']
    assert s('memory_revision') == revision


def test_forget_during_processing_is_not_resurrected(s):
    job = link(s)
    turn(s, 'I need to try this')
    item = s('memory_snapshot')['items'][0]
    s('memory', {'command': 'forget', 'text': item['id']})
    save(s, job)
    assert not s('memory_get', {'question': 'What did I want to try?'})['items']
    assert not s('memory_sources', {'ids': [item['id']]})


def test_correction_preserves_negation_and_source(s):
    link(s)
    turn(s, 'I need to try this')
    identifier = s('memory_snapshot')['items'][0]['id']
    result = turn(s, "Actually I don't want to try this", update=3)
    assert 'Remembered:' in result['reply']
    items = s('memory_snapshot')['items']
    assert len(items) == 1 and items[0]['id'] == identifier
    assert items[0]['text'] == "I don't want to try this" and items[0]['source_id']


@pytest.mark.parametrize('general', [False, True])
def test_dashboard_and_telegram_join_personal_intent_and_note(s, monkeypatch, general):
    job = link(s)
    turn(s, 'I need to try this')
    saved = save(s, job)
    real_storage = s
    s = lambda op, p=None: [] if op == 'retrieve' else real_storage(op, p)
    monkeypatch.setenv('RAG_JEV_ENABLED', 'false')
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    def answer(chain, messages, **kwargs):
        data = json.loads(messages[1]['content'])['untrusted_notes']
        assert 'I need to try API Anything' in str(data)
        assert 'turns websites into APIs' in str(data)
        memory = next(k for k, v in data.items() if v['title'].startswith('Personal memory'))
        note = next(k for k, v in data.items() if v['title'].startswith('API Anything'))
        return SimpleNamespace(response_metadata={}, content=json.dumps({'answer': f'You intend to try API Anything [{memory}]. '
            f'It turns websites into APIs [{note}].', 'used_sources': [memory, note],
            **({'general_knowledge': ''} if general else {})}))
    monkeypatch.setattr(rag, 'invoke_with_retry', answer)
    result = rag.grounded_answer('What tool did I want to try and what does it do?', s, allow_general=general)
    assert saved['name'] in result['sources'] and any(i.startswith('memory:') for i in result['sources'])
    assert all(not i.startswith('memory:') for i in result['source_labels'].values())


def test_general_personal_questions_retrieve_facts_without_exact_word_overlap(s):
    s('memory', {'command': 'remember', 'text': 'career: I work as a backend engineer.'})
    items = s('memory_get', {'question': 'What do you know about me professionally?'})['items']
    assert items[0]['text'] == 'I work as a backend engineer.'
    # Independent preferences should coexist.
    turn(s, 'I like hiking')
    turn(s, 'I like jazz', update=3)
    assert len(s('memory_snapshot')['items']) == 3


def test_source_preference_and_usage_are_memory_but_format_is_instruction(s):
    link(s)
    assert 'Remembered:' in turn(s, 'I already use this tool')['reply']
    assert 'Remembered:' in turn(s, 'I like this reel', update=3)['reply']
    assert turn(s, 'I want the note in bullet points', update=4)['intent'] == 'revise'
    assert len(s('memory_snapshot')['items']) == 2
    # Relative pronouns are not unresolved source references.
    assert 'Remembered:' in turn(s, 'I prefer examples that include tests', update=5)['reply']


def test_intention_timing_and_uncertainty_are_not_note_instructions(s):
    link(s)
    assert 'Remembered:' in turn(s, 'I might try this today')['reply']
    item = s('memory_snapshot')['items'][0]
    assert item['text'] == 'I might try this today' and item['kind'] == 'intention'
    assert turn(s, 'I prefer shorter notes today', update=3)['intent'] == 'revise'
    assert len(s('memory_snapshot')['items']) == 1


def test_quoted_intention_and_storage_failure_do_not_claim_success(s):
    assert 'Quoted' in turn(s, 'I need to try this', forward_origin={'type': 'user'})['reply']
    link(s)
    def failed(op, p=None):
        if op == 'memory':
            raise OSError('disk unavailable')
        return s(op, p)
    with pytest.raises(OSError):
        turn(failed, 'I need to try this')
    assert not s('memory_snapshot')['items']


def test_personal_context_budget_fails_explicitly():
    items = [{'id': str(i), 'text': 'x' * 1000, 'scope': 'general', 'kind': 'fact', 'key': None} for i in range(9)]
    with pytest.raises(ValueError, match='context budget'):
        personal.relevant(items, 'What do you know about me?')
