from types import SimpleNamespace
from core import telegram, storage


def test_safe_formatting_unicode_and_code():
    text, entities = telegram.formatted('🎬 **A Visita** [S1]\n`x < 2`\n<script>literal</script>')
    assert text == '🎬 A Visita\nx < 2\n<script>literal</script>'
    assert entities[0] == {'type': 'bold', 'offset': 3, 'length': 8}
    assert entities[1]['type'] == 'code'


def test_answer_hides_sources_and_records_message_ids(monkeypatch, tmp_path):
    sent = []
    def call(method, payload, **kwargs):
        sent.append(payload)
        return {'message_id': len(sent)}
    monkeypatch.setattr(telegram, 'call', call)
    s = lambda op, p: storage.dispatch(op, p, root=tmp_path)
    assert telegram.send_answer(7, '**Movie** [S1] ' + '🎬' * 2000, {'S1': 'note.md'}, s)
    assert len(sent) == 2
    assert sent[0]['entities'][0]['type'] == 'bold'
    assert all('note.md' not in p['text'] and '[S1]' not in p['text'] for p in sent)
    assert 'tap Sources' in sent[-1]['text']
    assert all(p['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'answer_sources' for p in sent)
    assert s('answer_sources_get', {'chat_id': 7, 'message_id': 1}) == []
    assert s('answer_sources_get', {'chat_id': 8, 'message_id': 1}) is None


def test_telegram_result_and_failure(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake')
    monkeypatch.setattr(telegram.requests, 'post', lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {'ok': True, 'result': {'message_id': 42}}))
    assert telegram.call('sendMessage', {}) is True
    assert telegram.call('sendMessage', {}, result=True) == {'message_id': 42}
    monkeypatch.setattr(telegram, 'call', lambda *a, **k: False)
    assert not telegram.send_answer(7, 'text', {}, lambda *a: None)


def test_readable_canonical_sources(monkeypatch):
    monkeypatch.setenv('UI_URL', 'https://notes.example')
    text = telegram.source_text([{'label': 'S1', 'title': '?igsi=tracking', 'name': 'note.md',
        'url': 'https://instagram.com/p/ABC/?igsi=tracking'}])
    assert 'Saved Instagram note' in text and '?igsi' not in text
    assert 'https://www.instagram.com/p/ABC/' in text
    assert 'https://notes.example/?note=note.md' in text
