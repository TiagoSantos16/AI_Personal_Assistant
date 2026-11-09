import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from core.storage import dispatch


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / "modal_agent")
    app = importlib.import_module("app")
    from core import storage as core_storage
    scheduled_work_due = core_storage.scheduled_work_due
    monkeypatch.setattr(core_storage, 'scheduled_work_due', lambda: scheduled_work_due(root=tmp_path))
    monkeypatch.setattr(app, 'vol', SimpleNamespace(reload=lambda: None))
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
    calls = {"spawn": [], "reaction": [], "send": [], 'ask': app.ask_background.local}
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=lambda op, payload=None: dispatch(op, payload, root=tmp_path)))
    monkeypatch.setattr(app, "process_background", SimpleNamespace(spawn=lambda *a: calls["spawn"].append(a)))
    monkeypatch.setattr(app, "ask_background", SimpleNamespace(spawn=lambda *a: calls["spawn"].append(a)))
    monkeypatch.setattr(app, "react", lambda *a: calls["reaction"].append(a) and False)
    monkeypatch.setattr(app, "send", lambda *a, **kw: calls["send"].append(a) and False)
    monkeypatch.setattr(app, 'send_answer', lambda chat, text, sources, storage: calls['send'].append((chat, text)))
    monkeypatch.setattr(app, 'telegram_call', lambda *a, **k: True)
    return app.telegram_webhook.local, calls, app


def request(text, update=1, owner=7, header="secret"):
    async def json():
        return {"update_id": update, "message": {"from": {"id": owner}, "chat": {"id": 7}, "message_id": 11 + update, "text": text}}
    return SimpleNamespace(headers={"X-Telegram-Bot-Api-Secret-Token": header}, json=json)


def test_actual_header_and_owner(endpoint):
    function, calls, _ = endpoint
    with pytest.raises(HTTPException) as exc:
        asyncio.run(function(request("https://instagram.com/p/abc/", header="wrong")))
    assert exc.value.status_code == 403
    assert asyncio.run(function(request("https://instagram.com/p/abc/", owner=8)))["status"] == "ignored"
    assert not calls["spawn"]


def test_duplicate_update_and_source_no_extra_worker(endpoint):
    function, calls, _ = endpoint
    asyncio.run(function(request("https://instagram.com/p/abc/")))
    asyncio.run(function(request("https://instagram.com/p/abc/")))
    asyncio.run(function(request("https://instagram.com/p/abc/?utm=1", update=2)))
    assert len(calls["spawn"]) == len(calls["reaction"]) == 1
    assert not any("Processing in background" in args[1] for args in calls["send"])
    assert any("still working" in args[1] for args in calls["send"])


@pytest.mark.parametrize('followup,operation,count', [
    ('Remind these when i ask for niche ideas', 'recall_get', 1),
    ('Remind me to search this to see if it is real', 'reminder', 1),
    ('Tell me that again tomorrow', 'reminder', 1)])
def test_source_actions_during_processing_and_duplicate_webhooks(endpoint, followup, operation, count):
    function, calls, app = endpoint
    asyncio.run(function(request('https://instagram.com/p/DeUpDUwDID-/')))
    asyncio.run(function(request(followup, update=2)))
    asyncio.run(function(request(followup, update=2)))
    assert len(calls['spawn']) == 1
    if operation == 'recall_get':
        assert len(app.storage.remote(operation, {'question': 'niche ideas'})['rules']) == count
    else:
        listing = app.storage.remote('reminder', {'command': 'list', 'chat_id': 7})
        assert 'https://www.instagram.com/p/DeUpDUwDID-/' in listing
        assert listing.count('Linked source:') == count


def test_exact_command_does_not_ask(endpoint):
    function, calls, _ = endpoint
    asyncio.run(function(request("/asking x")))
    assert not calls["spawn"]
    asyncio.run(function(request("/ask@bot receita", update=2)))
    assert len(calls['spawn']) == 1 and calls['spawn'][0][:2] == ('receita', 7)
    assert calls['spawn'][0][2]['history'] == []


def test_link_does_not_need_storage_or_workers(endpoint, monkeypatch):
    function, calls, app = endpoint
    monkeypatch.setenv("UI_URL", "https://dashboard.example")
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=lambda *a: pytest.fail("/link touched storage")))
    assert asyncio.run(function(request("/link@bot")))["status"] == "ok"
    assert calls["send"] == [(7, "https://dashboard.example")]
    assert not calls["spawn"] and not calls["reaction"]
    with pytest.raises(HTTPException):
        asyncio.run(function(request("/link", header="wrong")))
    asyncio.run(function(request("/link", owner=8)))
    assert len(calls["send"]) == 1


def test_find_uses_one_storage_search(endpoint, monkeypatch):
    function, calls, app = endpoint
    operations = []
    records = [{"name": "recipe.md", "section": "notes", "title": "Receita"}]
    def remote(operation, payload=None):
        operations.append(operation)
        if operation == "update":
            return True
        if operation == "list":
            return {"records": records}
        if operation == "search":
            assert payload == {"query": "ovos", "records": [{"section": "notes", "name": "recipe.md"}]}
            return records
        pytest.fail("Unexpected storage call")
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=remote))
    asyncio.run(function(request("/find ovos")))
    assert operations == ["update", "list", "search"]
    assert calls["send"] == [(7, "Receita")]


def test_enqueue_failure_is_retryable(endpoint):
    function, calls, app = endpoint
    app.process_background = SimpleNamespace(spawn=lambda *a: (_ for _ in ()).throw(TimeoutError("ambiguous enqueue")))
    asyncio.run(function(request("https://instagram.com/p/abc/")))
    records = app.storage.remote("list")["records"]
    assert len(records) == 1 and records[0]["status"] == "failed"


@pytest.mark.parametrize("missing", [("TELEGRAM_WEBHOOK_SECRET",), ("TELEGRAM_ALLOWED_USER_ID",),
                                     ("TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_ALLOWED_USER_ID")])
def test_missing_configuration(endpoint, monkeypatch, caplog, missing):
    function, calls, _ = endpoint
    for name in missing:
        monkeypatch.delenv(name)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(function(request("https://instagram.com/p/abc/")))
    assert exc.value.status_code == 503 and not calls["spawn"]
    assert all(name in caplog.text for name in missing)
    assert "personal-assistant-secrets" in caplog.text


def test_dashboard_server_settings(endpoint, monkeypatch):
    import ast
    import subprocess
    _, _, app = endpoint
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    app.ui.local()
    assert "--browser.gatherUsageStats=false" in calls[0]
    assert "--server.enableXsrfProtection=true" in calls[0]
    tree = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    ui = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "ui")
    settings = {decorator.func.attr: {kw.arg: ast.literal_eval(kw.value) for kw in decorator.keywords
                if kw.arg in {"timeout", "max_containers", "max_inputs"}}
                for decorator in ui.decorator_list}
    assert settings["function"] == {"timeout": 86400, "max_containers": 1}
    assert settings["concurrent"] == {"max_inputs": 32}


def test_malformed_telegram_body_is_delivery_failure(monkeypatch):
    from core import telegram
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: SimpleNamespace(json=lambda: [], status_code=200))
    assert not telegram.react(7, 12)


def test_memory_commands_authorization_and_storage_failure(endpoint, monkeypatch):
    function, calls, app = endpoint
    asyncio.run(function(request('I watched Arrival.', owner=8)))
    assert not calls['send']
    asyncio.run(function(request('I watched Arrival.')))
    assert 'Noted:' in calls['send'][-1][1] and not calls['spawn']
    asyncio.run(function(request('/memory', update=2)))
    assert 'I watched Arrival.' in calls['send'][-1][1]
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=lambda op, p=None: True if op == 'update' else
        (_ for _ in ()).throw(OSError('disk full'))))
    asyncio.run(function(request('/remember I live in Lisbon.', update=3)))
    assert "couldn't" in calls['send'][-1][1] and 'Remembered:' not in calls['send'][-1][1]


def test_followup_and_revise_use_reply_context(endpoint):
    function, calls, app = endpoint
    asyncio.run(function(request('https://instagram.com/p/abc/\nUse Portuguese')))
    asyncio.run(function(request('Use bullet points', update=2)))
    assert len(calls['spawn']) == 2
    async def other():
        return {'update_id': 3, 'message': {'from': {'id': 7}, 'chat': {'id': 7}, 'message_id': 13,
            'text': 'https://instagram.com/p/def/'}}
    asyncio.run(function(SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, json=other)))
    asyncio.run(function(request('More detail', update=4)))
    assert len(calls['spawn']) == 4
    async def body():
        return {'update_id': 5, 'message': {'from': {'id': 7}, 'chat': {'id': 7}, 'message_id': 99,
            'text': '/revise concise English', 'reply_to_message': {'message_id': 12}}}
    asyncio.run(function(SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, json=body)))
    assert len(calls['spawn']) == 5


def test_telegram_answer_uses_current_memory(endpoint, monkeypatch):
    from core import rag
    _, calls, app = endpoint
    app.storage.remote('memory', {'command': 'remember', 'text': 'I watched Arrival.'})
    monkeypatch.setattr(rag, 'retrieve', lambda *a: [])
    def answer(question, rows, history=None, personal_memory=None):
        assert personal_memory[0]['text'] == 'I watched Arrival.'
        return {'answer': 'Personal memory: you watched Arrival.', 'sources': []}
    monkeypatch.setattr(rag, 'answer', answer)
    calls['ask']('Recommend a movie', 7)
    assert calls['send'][-1] == (7, 'Personal memory: you watched Arrival.')


@pytest.mark.parametrize('kind', ['callback_query', 'message_reaction'])
def test_sources_on_request_authorized_only(endpoint, kind):
    function, calls, app = endpoint
    app.storage.remote('answer_sources_put', {'chat_id': 7, 'message_id': 42, 'sources': {'S1': 'missing.md'}})
    async def run(owner=7, update=100):
        event = {'message_id': 42, 'chat': {'id': 7}, 'user': {'id': owner},
            'new_reaction': [{'type': 'emoji', 'emoji': '👍'}], 'old_reaction': []}
        if kind == 'callback_query':
            event = {'id': 'callback', 'data': 'answer_sources', 'from': {'id': owner},
                'message': {'message_id': 42, 'chat': {'id': 7}}}
        async def body():
            return {'update_id': update, kind: event}
        return await function(SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, json=body))
    assert asyncio.run(run(owner=8))['status'] == 'ignored'
    assert not calls['send']
    assert asyncio.run(run())['status'] == 'ok'
    assert 'no longer available' in calls['send'][-1][1]
    assert asyncio.run(run())['status'] == 'duplicate'
    assert len(calls['send']) == 1 and not calls['spawn']


@pytest.mark.parametrize('quoted', [False, True])
def test_forwarded_fact_is_not_remembered(endpoint, quoted):
    function, calls, app = endpoint
    async def body():
        return {'update_id': 1, 'message': {'from': {'id': 7}, 'chat': {'id': 7}, 'message_id': 99,
            'text': 'I watched Arrival.', **({'entities': [{'type': 'blockquote', 'offset': 0, 'length': 18}]} if quoted else
                {'forward_origin': {'type': 'user'}})}}
    asyncio.run(function(SimpleNamespace(headers={'X-Telegram-Bot-Api-Secret-Token': 'secret'}, json=body)))
    assert app.storage.remote('memory') == 'No personal memory saved.'
    assert not calls['spawn']


def test_plain_question_after_link_is_an_answer_not_a_revision(endpoint):
    function, calls, app = endpoint
    asyncio.run(function(request('https://instagram.com/p/abc/')))
    asyncio.run(function(request('What is compound interest?', update=2)))
    assert len(calls['spawn']) == 2
    assert calls['spawn'][-1][:2] == ('What is compound interest?', 7)
    assert calls['spawn'][-1][2]['source_url'] is None
    assert app.storage.remote('accept', {'url': 'https://instagram.com/p/abc/'})['status'] == 'running'
    assert not app.storage.remote('memory_snapshot')['items']


def test_natural_chat_greeting_memory_and_context(endpoint, monkeypatch):
    from core import rag
    function, calls, app = endpoint
    asyncio.run(function(request('Hello')))
    assert not calls['spawn']
    asyncio.run(function(request('Recommend a film', update=2)))
    question, chat_id, conversation = calls['spawn'][-1]
    seen = []
    def grounded(question, storage, **kwargs):
        seen.append(kwargs)
        return {'answer': '**General knowledge (not from your notes):** A film suggestion.',
                'sources': [], 'source_labels': {}, 'memory_revision': 0}
    monkeypatch.setattr(rag, 'grounded_answer', grounded)
    calls['ask'](question, chat_id, conversation)
    assert seen[0]['allow_general'] is True
    asyncio.run(function(request('Another one', update=3)))
    assert len(calls['spawn'][-1][2]['history']) == 2
    asyncio.run(function(request('Remember that I avoid dairy.', update=4)))
    assert 'Noted:' in calls['send'][-1][1]
    assert app.storage.remote('memory_snapshot')['items'][0]['text'] == 'I avoid dairy.'


def test_natural_route_failure_clarifies_without_generation(endpoint, monkeypatch):
    from core import decisions
    function, calls, app = endpoint
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: {'intent': 'clarify'})
    asyncio.run(function(request('Something ambiguous')))
    assert not calls['spawn'] and not app.storage.remote('memory_snapshot')['items']
    assert 'What would you like' in calls['send'][-1][1]
    asyncio.run(function(request('answer', update=2)))
    assert calls['spawn'][-1][0] == 'Something ambiguous'


def test_natural_memory_commands_and_duplicates_use_no_generation(endpoint):
    function, calls, app = endpoint
    for update, text in enumerate(['I watched Arrival.', 'show my memory', 'forget Arrival'], 1):
        asyncio.run(function(request(text, update=update)))
    assert not app.storage.remote('memory_snapshot')['items'] and not calls['spawn']
    asyncio.run(function(request('forget Arrival', update=3)))
    assert len(calls['send']) == 3


@pytest.mark.parametrize('prefix', ['', '/remember '])
def test_link_then_contextual_intention_keeps_one_worker(endpoint, monkeypatch, prefix):
    from core import decisions
    function, calls, app = endpoint
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('unneeded model'))
    asyncio.run(function(request('https://instagram.com/reel/DeON0MOB5oi/')))
    asyncio.run(function(request(prefix + 'I need to try this', update=2)))
    assert len(calls['spawn']) == 1
    assert 'Noted:' in calls['send'][-1][1]
    item = app.storage.remote('memory_snapshot')['items'][0]
    assert item['kind'] == 'intention' and item['source_id'] == 'DeON0MOB5oi'
    assert item['text'] == 'I need to try this'


def test_reminders_authorization_duplicates_and_failures(endpoint, monkeypatch):
    function, calls, app = endpoint
    asyncio.run(function(request('Remind me to call the dentist', owner=8)))
    assert not calls['send']
    asyncio.run(function(request('Remind me to call the dentist', update=2)))
    assert 'morning brief' in calls['send'][-1][1] and not calls['spawn']
    count = len(calls['send'])
    asyncio.run(function(request('Remind me to call the dentist', update=2)))
    assert len(calls['send']) == count
    asyncio.run(function(request('/reminders', update=3)))
    assert 'dentist' in calls['send'][-1][1]
    asyncio.run(function(request('/cancel', update=4)))
    assert 'Cancelled:' in calls['send'][-1][1]
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=lambda op, p=None: True if op == 'update'
        else (_ for _ in ()).throw(OSError('disk unavailable'))))
    asyncio.run(function(request('/remind pay rent', update=5)))
    assert "couldn't save" in calls['send'][-1][1]


def test_idle_scheduler_exits_without_storage_or_sending(endpoint, monkeypatch):
    _, calls, app = endpoint
    operations = []
    monkeypatch.setattr(app, 'storage', SimpleNamespace(remote=lambda op: operations.append(op) or {'status': 'idle'}))
    assert app.reminder_tick.local()['status'] == 'idle'
    assert operations == [] and not calls['send']
