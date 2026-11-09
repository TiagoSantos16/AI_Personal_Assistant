import pytest
from fastapi.testclient import TestClient

from dashboard_api_preview import create_preview
from dashboard_server import create_app
from dashboard_auth import SESSION
from core.auth import COOKIE, issue
from dashboard_store import snapshot


@pytest.fixture
def client():
    app = create_preview()
    with TestClient(app, base_url='http://testserver') as client:
        yield client
    app.state.fixture_directory.cleanup()


def login(client, remember=True):
    response = client.post('/api/login', json={'password': 'local-preview', 'remember': remember}, headers={'Origin': 'http://testserver'})
    assert response.status_code == 200
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf']}


def test_empty_workspace(tmp_path):
    data = snapshot(root=tmp_path)
    assert not any(data[key] for key in ('records', 'tasks', 'memory', 'recalls', 'jobs'))
    assert data['brief']['task_ids'] == []


def test_workspace_keeps_note_details_out_of_list_payload(client):
    login(client)
    workspace = client.get('/api/workspace').json()
    research = next(note for note in workspace['records'] if note['name'] == 'research.md')
    assert 'generation' not in research and 'accounting' not in research and 'media' not in research
    detail = client.get('/api/note?name=research.md').json()
    assert detail['generation']['transcript'] and detail['accounting'] and detail['media']


@pytest.mark.parametrize('path', ['/api/workspace', '/api/note?name=research.md', '/api/media/0?name=research.md'])
def test_reads_require_authorization(client, path):
    assert client.get(path).status_code == 401
    assert not client.app.state.fixture_calls


@pytest.mark.parametrize('path,body', [
    ('/api/notes/redo', {'name': 'research.md'}),
    ('/api/notes/delete', {'records': [{'name': 'research.md'}]}),
    ('/api/tasks/task-research', {'action': 'done', 'revision': 1}),
    ('/api/memory/fact', {'action': 'forget', 'revision': 1}),
    ('/api/recall/recall-workflow/forget', {}),
    ('/api/reindex', {}),
    ('/api/chat', {'question': 'hello'}),
])
def test_actions_require_authorization(client, path, body):
    assert client.post(path, json=body, headers={'Origin': 'http://testserver'}).status_code == 401
    assert not client.app.state.fixture_calls


def test_same_origin_production_assets_and_telegram_link(tmp_path):
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/app.js').write_text('/* fixture build */', encoding='utf-8')
    (tmp_path / 'index.html').write_text('<script type="module" src="/assets/app.js"></script>', encoding='utf-8')
    with TestClient(create_app(dist=tmp_path, password='owner')) as client:
        response = client.get('/?note=research.md')
        assert response.status_code == 200 and 'type="module"' in response.text
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/api/note?name=research.md').status_code == 401


def test_cookie_security_expiry_rotation_and_session_only(monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD', 'owner')
    with TestClient(create_app(), base_url='https://testserver') as client:
        response = client.post('/api/login', json={'password': 'owner'}, headers={'Origin': 'https://testserver'})
        cookies = response.headers.get_list('set-cookie')
        assert all('HttpOnly' in c and 'Secure' in c and 'SameSite=strict' in c for c in cookies)
        assert any(COOKIE in c and 'Max-Age=2592000' in c for c in cookies)
        assert client.get('/api/session').status_code == 200
        monkeypatch.setenv('DASHBOARD_PASSWORD', 'rotated')
        assert client.get('/api/session').status_code == 401
    with TestClient(create_app(password='owner'), base_url='https://testserver') as client:
        client.cookies.set(COOKIE, issue('owner'))
        assert client.get('/api/session').status_code == 200


def test_session_cookie_remember_and_logout(client):
    headers = login(client, remember=False)
    assert SESSION in client.cookies and COOKIE not in client.cookies
    assert client.get('/api/session').status_code == 200
    assert client.post('/api/logout', json={}, headers=headers).status_code == 200
    assert client.get('/api/workspace').status_code == 401


def test_origin_csrf_and_login_throttling(client):
    assert client.post('/api/login', json={'password': 'local-preview'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    headers = login(client)
    assert client.post('/api/reindex', json={}).status_code == 403
    assert client.post('/api/reindex', json={}, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/reindex', json={}, headers={**headers, 'X-CSRF-Token': 'invalid'}).status_code == 403
    assert not client.app.state.fixture_calls
    for _ in range(5):
        assert client.post('/api/login', json={'password': 'bad'}, headers={'Origin': 'http://testserver'}).status_code == 401
    assert client.post('/api/login', json={'password': 'local-preview'}, headers={'Origin': 'http://testserver'}).status_code == 429


def test_workspace_task_transitions_and_stale_revision(client):
    headers = login(client)
    data = client.get('/api/workspace').json()
    assert len(data['tasks']) == 4 and data['recalls'][0]['linked_note']['name'] == 'research.md'
    assert data['brief']['task_ids'] == ['task-research', 'task-desk']
    assert client.post('/api/tasks/task-research', json={'action': 'done', 'revision': data['revision'] - 1}, headers=headers).status_code == 409
    assert client.post('/api/tasks/task-research', json={'action': 'done', 'revision': data['revision']}, headers=headers).status_code == 200
    data = client.get('/api/workspace').json()
    assert next(t for t in data['tasks'] if t['id'] == 'task-research')['status'] == 'done'
    response = client.post('/api/tasks/task-desk', json={'action': 'reschedule', 'timing': 'tomorrow at 18:00', 'revision': data['revision']}, headers=headers)
    assert response.status_code == 200
    data = client.get('/api/workspace').json()
    task = next(t for t in data['tasks'] if t['id'] == 'task-desk')
    assert task['status'] == 'pending' and task['changes'][-1]['command'] == 'reschedule'


def test_note_actions_media_and_bulk_delete(client):
    headers = login(client)
    note = client.get('/api/note?name=research.md').json()
    assert 'Keep the idea' in note['content'] and 'Recorded AI cost $0' in note['cost_summary']
    assert client.get('/api/media/0?name=research.md').headers['content-type'] == 'image/png'
    assert client.get('/api/media/5?name=research.md').status_code == 404
    assert client.get('/api/note?name=..%2F.env').status_code == 400
    assert client.post('/api/notes/redo', json={'name': 'research.md'}, headers=headers).status_code == 200
    assert ('redo', {'name': 'research.md', 'section': 'notes'}) in client.app.state.fixture_calls
    assert client.post('/api/notes/redo', json={'name': 'desk.md'}, headers=headers).status_code == 200
    response = client.post('/api/notes/delete', json={'records': [{'name': 'research.md'}, {'name': 'desk.md'}]}, headers=headers)
    assert response.status_code == 200
    assert client.get('/api/note?name=research.md').status_code == 404
    assert not any(t['linked_note'] for t in client.get('/api/workspace').json()['tasks'])


def test_chat_filters_citations_and_keeps_history(client):
    headers = login(client)
    response = client.post('/api/chat', json={'question': 'What should I remember?', 'history': [{'role': 'user', 'content': 'I am reading the source.'}]}, headers=headers)
    assert response.status_code == 200
    assert response.json()['sources'] == [{'label': 'S1', 'name': 'research.md', 'title': 'A better way to keep research notes'}]
    call = next(payload for op, payload in client.app.state.fixture_calls if op == 'answer')
    assert call['history'][0]['content'] == 'I am reading the source.'
    assert client.post('/api/chat', json={'question': ' ', 'history': []}, headers=headers).status_code == 422


def test_memory_forget_correction_recall_and_revision(client):
    headers = login(client)
    data = client.get('/api/workspace').json()
    assert client.post('/api/memory/fact', json={'action': 'correct', 'text': 'I live in Porto.', 'revision': data['revision']}, headers=headers).status_code == 200
    data = client.get('/api/workspace').json()
    assert next(m for m in data['memory'] if m['id'] == 'fact')['text'] == 'I live in Porto.'
    assert client.post('/api/memory/preference', json={'action': 'forget', 'revision': data['revision']}, headers=headers).status_code == 200
    assert client.post('/api/recall/recall-workflow/forget', json={}, headers=headers).status_code == 200
    data = client.get('/api/workspace').json()
    assert len(data['memory']) == 1 and not data['recalls']


def test_no_streamlit_dependency_and_private_errors():
    def failed_store(*args):
        raise RuntimeError('secret credential should never reach the browser')
    with TestClient(create_app(store=failed_store, password='owner'), base_url='https://testserver', raise_server_exceptions=False) as client:
        client.cookies.set(COOKIE, issue('owner'))
        response = client.get('/api/workspace')
        assert response.status_code == 503 and 'secret credential' not in response.text
    import inspect
    import dashboard_server
    assert 'import streamlit' not in inspect.getsource(dashboard_server)


def test_modal_snapshot_uses_existing_serialized_volume(monkeypatch):
    import importlib
    from pathlib import Path
    from types import SimpleNamespace
    import core.storage
    import dashboard_store
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / 'modal_agent')
    entry = importlib.import_module('app')
    calls = []
    volume = SimpleNamespace(reload=lambda: calls.append('reload'))
    monkeypatch.setattr(entry, 'vol', volume)
    monkeypatch.setattr(dashboard_store, 'snapshot', lambda **kwargs: calls.append(('snapshot', kwargs)) or {'tasks': []})
    monkeypatch.setattr(core.storage, 'dispatch', lambda *args, **kwargs: calls.append((args, kwargs)) or 'existing')
    assert entry.storage.local('dashboard_snapshot') == {'tasks': []}
    assert calls == ['reload', ('snapshot', {'volume': volume})]
    calls.clear()
    assert entry.storage.local('memory_snapshot') == 'existing'
    assert calls == ['reload', (('memory_snapshot', None), {'volume': volume})]


def test_modal_launches_fastapi_and_packages_frontend(monkeypatch):
    import ast
    import importlib
    import subprocess
    import sys
    from pathlib import Path
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / 'modal_agent')
    entry = importlib.import_module('app')
    calls = []
    monkeypatch.setattr(subprocess, 'Popen', lambda args, **kwargs: calls.append((args, kwargs)))
    entry.ui.local()
    args, options = calls[0]
    assert args[:4] == [sys.executable, '-m', 'uvicorn', 'dashboard_server:app']
    assert args[args.index('--port') + 1] == '8000'
    assert '--proxy-headers' in args and options['cwd'] == '/root'
    tree = ast.parse(Path(entry.__file__).read_text(encoding='utf-8'))
    files = [node.args[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr in {'add_local_file', 'add_local_dir'}]
    assert 'dashboard_web/dist' in files and 'dashboard_store.py' in files and 'dashboard_auth.py' in files
    assert 'dashboard.py' not in files and '.streamlit/config.toml' not in files
