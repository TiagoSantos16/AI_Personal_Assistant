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
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
    calls = {"spawn": [], "conversation": [], "reaction": [], "send": []}
    monkeypatch.setattr(app, "storage", SimpleNamespace(remote=lambda op, payload=None: dispatch(op, payload, root=tmp_path)))
    monkeypatch.setattr(app, "process_background", SimpleNamespace(spawn=lambda *a: calls["spawn"].append(a)))
    monkeypatch.setattr(app, "ask_background", SimpleNamespace(spawn=lambda *a: calls["spawn"].append(a)))
    monkeypatch.setattr(app, "conversation_background", SimpleNamespace(spawn=lambda *a: calls["conversation"].append(a)))
    monkeypatch.setattr(app, "react", lambda *a: calls["reaction"].append(a) and False)
    monkeypatch.setattr(app, "send", lambda *a: calls["send"].append(a) and False)
    return app.telegram_webhook.local, calls, app


def request(text, update=1, owner=7, header="secret", message_id=12, reply_id=None):
    async def json():
        return {"update_id": update, "message": {"from": {"id": owner}, "chat": {"id": 7}, "message_id": message_id,
            "text": text, **({"reply_to_message": {"message_id": reply_id}} if reply_id is not None else {})}}
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


def test_exact_command_does_not_ask(endpoint):
    function, calls, _ = endpoint
    asyncio.run(function(request("/asking x")))
    assert not calls["spawn"]
    asyncio.run(function(request("/ask@bot receita", update=2)))
    assert calls["spawn"] == [("receita", 7)]


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


def test_link_instructions_and_reply_revision(endpoint):
    function, calls, app = endpoint
    asyncio.run(function(request("Use bullets https://instagram.com/p/abc/")))
    asyncio.run(function(request("/revise Focus on implementation", update=2, message_id=13, reply_id=12)))
    assert len(calls["spawn"]) == 2 and not calls["conversation"]
    note_job = app.storage.remote("claim", {"job_id": calls["spawn"][-1][0], "attempt": calls["spawn"][-1][1]})["job"]
    assert [i["text"] for i in note_job["instructions"]] == ["Use bullets", "Focus on implementation"]


def test_plain_followup_captures_original_context(endpoint):
    function, calls, _ = endpoint
    asyncio.run(function(request("https://instagram.com/p/abc/")))
    asyncio.run(function(request("Focus on implementation", update=2, message_id=13)))
    args = calls["conversation"][0]
    assert args[0] == "Focus on implementation" and args[1]["source_id"] == "abc"
    assert len(calls["spawn"]) == 1
    asyncio.run(function(request("Focus on implementation", update=2, message_id=13)))
    assert len(calls["conversation"]) == 1


def test_memory_commands_are_persistent_and_need_no_model(endpoint):
    function, calls, app = endpoint
    asyncio.run(function(request("/remember fact:movies:watched:arrival = I watched Arrival")))
    item = app.storage.remote("memory_list")[0]
    assert item["scope"] == "movies"
    asyncio.run(function(request("/memory", update=2)))
    assert item["id"] in calls["send"][-1][1] and "Updated:" in calls["send"][-1][1]
    asyncio.run(function(request(f"/correct {item['id']} I have not watched Arrival", update=3)))
    assert app.storage.remote("memory_list")[0]["text"] == "I have not watched Arrival"
    asyncio.run(function(request(f"/forget {item['id']}", update=4)))
    assert app.storage.remote("memory_list") == []
    assert not calls["spawn"] and not calls["conversation"]


def test_memory_and_instructions_do_not_bypass_authorisation(endpoint):
    function, calls, app = endpoint
    asyncio.run(function(request("/remember location = Lisbon", owner=8)))
    asyncio.run(function(request("I watched Arrival", owner=8, update=2)))
    assert not app.storage.remote("memory_list") and not calls["conversation"]


def test_ambiguous_revision_asks_for_reply(endpoint):
    function, calls, _ = endpoint
    asyncio.run(function(request("https://instagram.com/p/one/", message_id=12)))
    asyncio.run(function(request("https://instagram.com/p/two/", update=2, message_id=13)))
    asyncio.run(function(request("/revise Use bullets", update=3, message_id=14)))
    assert len(calls["spawn"]) == 2
    assert "Reply to the original link" in calls["send"][-1][1]
