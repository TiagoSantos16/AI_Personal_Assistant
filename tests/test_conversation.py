"""Offline conversational routing, memory safety and provenance tests."""
import json
from types import SimpleNamespace
import pytest
from core import conversation, decisions, storage, rag


@pytest.fixture
def s(tmp_path):
    return lambda op, p=None: storage.dispatch(op, p, root=tmp_path)


def message(text, **extra):
    return {'text': text, 'message_id': 10, 'chat': {'id': 7}, 'from': {'id': 7}, **extra}


def turn(s, text, update=1, **extra):
    msg = message(text, **extra)
    return conversation.prepare_message(text, msg, {'chat_id': 7, 'message_id': 1000 + update,
        'thread_id': msg.get('message_thread_id'), 'date': 123}, update, s)


@pytest.mark.parametrize('text,intent', [
    ('Hi!', 'reply'), ('Thanks', 'reply'), ('Show my notes', 'notes'),
    ('What do you remember about me?', 'memory'), ('Open the dashboard', 'link'),
    ('What is compound interest?', 'answer'), ('Recommend a film', 'answer'),
    ('Can you explain this video?', 'answer'), ('I watched Arrival.', 'remember'),
    ('I live in Lisbon.', 'remember'), ('My name is Alex.', 'remember'),
    ('Remember that I avoid dairy.', 'remember'), ('Actually I live in Porto.', 'correct'),
    ('Can you remember that I avoid dairy?', 'remember'), ('Please forget Arrival', 'forget'),
    ('Can you correct this note?', 'revise'), ('Correct the note spelling', 'revise'),
    ('Forget Arrival', 'forget'), ('Simplify this note', 'revise'),
    ('I prefer concise notes for this post', 'revise'),
    ('I watched Arrival and recommend another film', 'clarify'), ('Remind me tomorrow', 'reminder')])
def test_clear_intents_do_not_call_model(monkeypatch, text, intent):
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('unneeded router call'))
    assert conversation.route(text, {'has_source': True})['intent'] == intent


@pytest.mark.parametrize('confidence', [.1, float('nan'), float('inf'), True, 2])
def test_invalid_or_uncertain_decision_clarifies(monkeypatch, confidence):
    monkeypatch.setattr('core.llm.get_llm', lambda _: [])
    monkeypatch.setattr('core.llm.invoke_with_retry', lambda *a, **kw: SimpleNamespace(
        content=json.dumps({'intent': 'remember', 'confidence': confidence}), response_metadata={}))
    assert decisions.message_intent('I avoid dairy.', {})['intent'] == 'clarify'


def test_primary_interpretation_chain_and_disable_switch(monkeypatch):
    from core.config import BASIC_MODELS
    calls = []
    monkeypatch.setattr('core.llm.get_llm', lambda chain: calls.append(chain) or chain)
    monkeypatch.setattr('core.llm.invoke_with_retry', lambda *a, **kw: SimpleNamespace(
        content=json.dumps({'intent': 'remember', 'confidence': .99}), response_metadata={}))
    assert conversation.route('I avoid dairy.', {})['intent'] == 'remember'
    assert calls == [BASIC_MODELS[:1]]
    monkeypatch.setenv('TELEGRAM_ROUTER_ENABLED', 'false')
    monkeypatch.setattr('core.llm.get_llm', lambda *a: pytest.fail('disabled'))
    assert decisions.message_intent('unclear', {})['intent'] == 'clarify'


def test_personal_memory_without_commands_and_scoped_constraint_retrieval(s):
    assert 'Remembered' in turn(s, 'I watched Arrival.')['reply']
    assert 'Remembered' in turn(s, 'Remember that I avoid dairy.', update=2)['reply']
    assert 'I avoid dairy.' in turn(s, 'show my memory', update=3)['reply']
    assert s('memory_get', {'question': 'What should I have for dinner?'})['items'][0]['text'] == 'I avoid dairy.'
    assert 'Forgotten' in turn(s, 'forget Arrival', update=4)['reply']
    assert not s('memory_get', {'question': 'Arrival'})['items']
    assert 'Remembered' in turn(s, 'I live in Lisbon.', update=5)['reply']
    assert 'Remembered' in turn(s, 'Actually I now live in Porto.', update=6)['reply']
    facts = s('memory_get', {'question': 'Where is my home?'})['items']
    residence = next(i for i in facts if i['key'] == 'residence')
    assert residence['text'] == 'I now live in Porto.'
    assert residence['source_text'] == 'Actually I now live in Porto.'


def test_questions_never_become_source_instructions(s):
    s('accept', {'url': 'https://instagram.com/p/One/', 'chat_id': 7, 'message_id': 44})
    decision = turn(s, 'What does this video explain?', reply_to_message={'message_id': 44})
    assert decision['intent'] == 'answer'
    assert decision['conversation']['source_url'].endswith('/One/')
    assert s('accept', {'url': 'https://instagram.com/p/One/'})['status'] == 'running'
    assert not s('memory_snapshot')['items']


def test_ambiguous_source_revision_asks_instead_of_choosing(s):
    for name, identifier in [('One', 44), ('Two', 45)]:
        s('accept', {'url': f'https://instagram.com/p/{name}/', 'chat_id': 7, 'message_id': identifier})
    assert turn(s, 'Make it shorter')['source_url'].endswith('/Two/')
    decision = turn(s, 'Make this note shorter', update=2, reply_to_message={'message_id': 44})
    assert decision['intent'] == 'revise' and decision['source_url'].endswith('/One/')


def test_pending_clarification_uses_original_message_and_can_cancel(s, monkeypatch):
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: {'intent': 'clarify'})
    assert 'What would you like' in turn(s, 'Something ambiguous')['reply']
    resolved = turn(s, 'answer', update=2)
    assert resolved['intent'] == 'answer' and resolved['text'] == 'Something ambiguous'
    turn(s, 'Ambiguous again', update=3)
    assert "won't change" in turn(s, 'cancel', update=4)['reply']
    context = s('chat_context', {'chat_id': 7, 'update_id': 5})
    assert not context['pending']


def test_delayed_model_interpretation_cannot_restore_forgotten_fact(s, monkeypatch):
    turn(s, 'Remember that I avoid dairy.')
    identifier = s('memory_snapshot')['items'][0]['id']
    def classify(*args):
        s('memory', {'command': 'forget', 'text': identifier})
        return {'intent': 'remember'}
    monkeypatch.setattr(decisions, 'message_intent', classify)
    reply = turn(s, 'I avoid dairy.', update=2)['reply']
    assert 'memory changed' in reply
    assert not s('memory_snapshot')['items']


def test_model_cannot_derive_personal_fact_from_source_claims(s, monkeypatch):
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: {'intent': 'remember'})
    result = turn(s, 'The video says I watched Arrival.')
    assert result['intent'] == 'reply' and not s('memory_snapshot')['items']
    assert not s('memory_snapshot')['items']


def test_conversation_history_scoped_bounded_and_late_worker_rejected(s, monkeypatch):
    first = turn(s, 'Recommend a film')['conversation']
    assert s('chat_finish', {**first, 'memory_revision': 0, 'answer': 'A film recommendation'})['status'] == 'ok'
    second = turn(s, 'Another one', update=2)['conversation']
    assert len(second['history']) == 2
    third = turn(s, 'What is Python?', update=3)['conversation']
    assert s('chat_finish', {**second, 'memory_revision': 0, 'answer': 'late'})['status'] == 'stale'
    s('memory', {'command': 'remember', 'text': 'I live in Lisbon.'})
    assert s('chat_finish', {**third, 'memory_revision': 0, 'answer': 'outdated'})['status'] == 'memory_changed'
    assert not s('chat_context', {'chat_id': 8, 'update_id': 4})['history']
    assert not s('chat_context', {'chat_id': 7, 'thread_id': 99, 'update_id': 4})['history']
    now = storage.time.time()
    monkeypatch.setattr(storage.time, 'time', lambda: now + 86401)
    assert not s('chat_context', {'chat_id': 7, 'update_id': 5})['history']


def test_general_knowledge_is_separate_and_default_remains_grounded(monkeypatch):
    monkeypatch.setenv('RAG_JEV_ENABLED', 'false')
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    calls = []
    def model(chain, messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(content=json.dumps({'answer': 'The source lists Arrival [S1].', 'used_sources': ['S1'],
            'general_knowledge': 'A film can use an unreliable narrator.'}), response_metadata={})
    monkeypatch.setattr(rag, 'invoke_with_retry', model)
    rows = [{'id': 'S1', 'source': 'film.md', 'title': 'Films', 'text': 'Arrival'}]
    result = rag.answer('Explain', rows, allow_general=True)
    assert result['answer'].startswith('The source lists Arrival [S1].')
    assert 'Beyond the saved evidence,' in result['answer'] and '**From your' not in result['answer']
    assert result['sources'] == ['film.md']
    assert 'no web access' in calls[0][0]['content']
    assert rag.answer('Explain', [])['sources'] == [] and len(calls) == 1


def test_general_answer_without_notes_does_not_invent_citations(monkeypatch):
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **k: SimpleNamespace(response_metadata={},
        content=json.dumps({'answer': '', 'used_sources': [], 'general_knowledge': 'Python is a programming language.'})))
    result = rag.answer('What is Python?', [], allow_general=True)
    assert 'Beyond the saved evidence,' in result['answer'] and not result['sources']


def test_uncited_note_claim_is_repaired_instead_of_presented_as_sourced(monkeypatch):
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    calls = []
    def model(*a, **k):
        calls.append(1)
        return SimpleNamespace(response_metadata={}, content=json.dumps({
            'answer': 'Your notes say Python is easy.', 'used_sources': [], 'general_knowledge': ''}))
    monkeypatch.setattr(rag, 'invoke_with_retry', model)
    from core.errors import AnswerValidationError
    with pytest.raises(AnswerValidationError):
        rag.answer('Python?', [], allow_general=True)
    assert len(calls) == 2


def test_direct_source_question_reads_only_that_note_without_ranking(s, tmp_path, monkeypatch):
    (tmp_path / 'notes').mkdir(exist_ok=True)
    (tmp_path / 'notes' / 'films.md').write_text('# Film recommendations\n**Source URL:** https://instagram.com/p/One/\n\nArrival is listed.', encoding='utf-8')
    (tmp_path / 'notes' / 'other.md').write_text('# Other\n**Source URL:** https://instagram.com/p/Two/\n\nUnrelated material.', encoding='utf-8')
    s('list')
    monkeypatch.setattr(decisions, 'rank_sources', lambda *a: pytest.fail('direct source needs no ranking call'))
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    def model(chain, messages, **kwargs):
        data = json.loads(messages[1]['content'])
        assert 'Arrival' in str(data['untrusted_notes']) and 'Unrelated material' not in str(data['untrusted_notes'])
        return SimpleNamespace(response_metadata={}, content=json.dumps({
            'answer': 'The source lists Arrival [S1].', 'used_sources': ['S1'], 'general_knowledge': ''}))
    monkeypatch.setattr(rag, 'invoke_with_retry', model)
    result = rag.grounded_answer('What does this note say?', s, allow_general=True, source_url='https://instagram.com/p/One/')
    assert result['sources'] == ['films.md'] and '[S1]' in result['answer']


def test_unknown_source_is_not_explained_from_general_knowledge(s, monkeypatch):
    monkeypatch.setattr(rag, 'invoke_with_retry', lambda *a, **kw: pytest.fail('no source evidence'))
    result = rag.grounded_answer('Explain this video', s, allow_general=True, source_url='https://instagram.com/p/Unknown/')
    assert 'not available yet' in result['answer'] and not result['sources']


def test_shortening_an_answer_keeps_original_retrieval_topic(s):
    first = turn(s, 'Recommend a film with a plot twist')['conversation']
    s('chat_finish', {**first, 'memory_revision': 0, 'answer': 'A film recommendation'})
    followup = turn(s, 'Make it shorter', update=2)
    assert followup['intent'] == 'answer' and not followup['conversation']['source_url']
    assert 'plot twist' in rag.retrieval_question(followup['text'], followup['conversation']['history'])


def test_bounded_history_and_pending_expiry(s, monkeypatch):
    for update in range(1, 18):
        current = turn(s, 'What is Python?', update)['conversation']
        s('chat_finish', {**current, 'memory_revision': 0, 'answer': 'A' * 1200})
    context = s('chat_context', {'chat_id': 7, 'update_id': 18})
    assert len(context['history']) <= 12
    assert sum(len(m['content']) for m in context['history']) <= 12000
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: {'intent': 'clarify'})
    turn(s, 'ambiguous', update=19)
    now = storage.time.time()
    monkeypatch.setattr(storage.time, 'time', lambda: now + 601)
    assert not s('chat_context', {'chat_id': 7, 'update_id': 20})['pending']


def test_one_off_preferences_and_deictic_remembering_do_not_persist(s):
    assert turn(s, 'I prefer short answers today')['intent'] == 'answer'
    assert turn(s, 'Remember this', update=2)['intent'] == 'reply'
    assert turn(s, 'Remember that I prefer concise notes for this post', update=3)['intent'] == 'reply'
    assert not s('memory_snapshot')['items']


def test_delayed_natural_revision_cannot_overtake_newer_message(s):
    s('accept', {'url': 'https://instagram.com/p/One/', 'chat_id': 7, 'message_id': 44, 'update_id': 1})
    planned = turn(s, 'Make this note shorter', update=2, reply_to_message={'message_id': 44})
    s('accept', {'url': 'https://instagram.com/p/One/', 'chat_id': 7, 'message_id': 45,
        'instructions': 'Use full detail', 'update_id': 3})
    result = s('accept', {'url': planned['source_url'], 'chat_id': 7, 'update_id': 2,
        'instructions': planned['text'], 'expected_sequence': planned['sequence']})
    assert result['status'] == 'stale'


def test_changed_sources_are_not_delivered_as_current(s):
    current = turn(s, 'What is Python?')['conversation']
    assert s('chat_finish', {**current, 'memory_revision': 0, 'answer': 'Outdated',
        'sources': ['deleted.md']})['status'] == 'sources_changed'
