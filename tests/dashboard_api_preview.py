"""Explicit local fixture for the React dashboard. Never imported by Modal."""
import base64
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'modal_agent'))
from dashboard_server import create_app
from dashboard_store import snapshot
from core.storage import dispatch


def create_preview():
    directory = tempfile.TemporaryDirectory(prefix='frida-dashboard-preview-')
    root = Path(directory.name)
    for folder in ('private', 'notes', 'failed'):
        (root / folder).mkdir()
    now = time.time()
    notes = [
        {'name': 'research.md', 'title': 'A better way to keep research notes', 'category': 'knowledge',
         'creator': 'The Quiet Workspace', 'saved': now - 3600, 'section': 'notes', 'status': 'saved',
         'review_status': 'approved', 'version_id': 'fixture-v1', 'source_id': 'research',
         'url': 'https://www.instagram.com/p/ResearchPreview/', 'creator_url': 'https://www.instagram.com/example/',
         'media': ['fixture.png'], 'accounting': [{'record_id': 'fixture-writer', 'step': 'writer', 'model': 'local-fixture',
             'cost': 0, 'cost_status': 'reported', 'duration': 1.2, 'success': True}],
         'generation': {'instructions': [{'text': 'Keep the practical steps and explain the tradeoffs.', 'message_id': 42, 'date': now - 7200}],
             'description': 'A simple practice for keeping ideas connected.', 'transcript': 'Write one useful idea per note. Keep the original source.',
             'writer_history': ['Start with the question you want to answer. Keep each note focused on one useful idea.'],
             'critique_history': ['Approved. The practical steps are supported by the source.'],
             'model_log': [{'label': 'Writer', 'model': 'local-fixture', 'tried': []}]}},
        {'name': 'desk.md', 'title': 'A calmer desk, a clearer head', 'category': 'personal', 'creator': 'Everyday Systems',
         'saved': now - 18000, 'section': 'notes', 'status': 'saved', 'review_status': 'needs_review',
         'version_id': 'fixture-v2', 'source_id': 'desk', 'coverage': ['Visual text on one frame is incomplete.'],
         'url': 'https://www.instagram.com/p/DeskPreview/', 'accounting': None},
        {'name': 'walk.md', 'title': 'Make room for a daily walk', 'category': 'wellbeing', 'creator': 'Outside Notes',
         'saved': now - 86400, 'section': 'notes', 'status': 'saved', 'review_status': 'approved',
         'version_id': 'fixture-v3', 'source_id': 'walk', 'accounting': None},
        {'name': 'older.md', 'title': 'Research notes — earlier version', 'category': 'knowledge',
         'saved': now - 96000, 'section': 'failed', 'status': 'superseded', 'version_id': 'fixture-v0', 'source_id': 'research'},
        {'name': 'failed.md', 'title': 'A source that needs another attempt', 'category': 'general',
         'created': now - 300, 'section': 'failed', 'status': 'failed', 'version_id': 'fixture-f1', 'source_id': 'failed',
         'diagnostic': 'Local fixture: the source download was interrupted.', 'error_kind': 'download',
         'url': 'https://www.instagram.com/p/FailedPreview/'}]
    for note in notes:
        (root / note['section'] / note['name']).write_text(
            '# ' + note['title'] + '\n\n## Keep the idea, keep the context\n\nStart with the question you want to answer. '
            'Keep each note focused on one useful idea, and save the source so you can come back to it.\n\n'
            '### Put it into practice\n- Write the idea in your own words.\n- Connect it to a question or project.\n- Review it when the context is useful.\n\n'
            '<details><summary>Spoiler</summary>A hidden detail, revealed only when you choose.</details>\n', encoding='utf-8')
    tasks = {}
    for identifier, text, status, due in [
        ('task-research', 'Try the research note workflow', 'pending', now - 86400),
        ('task-desk', 'Review the desk setup ideas', 'notified', now - 3600),
        ('task-snoozed', 'Test the new reading routine', 'pending', now + 86400),
        ('task-completed', 'Organise the saved sources', 'done', now - 172800)]:
        tasks[identifier] = {'id': identifier, 'text': text, 'status': status, 'due': due, 'brief': True,
            'chat_id': 7, 'thread_id': None, 'user_id': 7, 'created': now - 172800, 'updated': now,
            'kind': 'task', 'source_id': 'research', 'source_url': notes[0]['url'], 'delivery': {'history': []},
            'changes': [{'command': 'reschedule', 'date': now}] if identifier == 'task-snoozed' else []}
    state = {'revision': 1, 'answer_revision': 1, 'records': {n['section'] + '/' + n['name']: n for n in notes},
        'sources': {n['source_id']: {'active': n['section'] + '/' + n['name']} for n in notes if n['section'] == 'notes'},
        'jobs': {}, 'updates': {}, 'reminders': tasks,
        'memory': {'revision': 1, 'items': {
            'preference': {'id': 'preference', 'text': 'I prefer concise notes with practical steps.', 'kind': 'preference', 'scope': 'notes', 'key': 'style', 'chat_id': 7, 'date': now, 'source_text': 'I prefer concise notes with practical steps.', 'message_id': 12},
            'fact': {'id': 'fact', 'text': 'I live in Lisbon.', 'kind': 'fact', 'scope': 'general', 'key': 'residence', 'chat_id': 7, 'date': now - 86400, 'message_id': 11}}},
        'recalls': {'recall-workflow': {'id': 'recall-workflow', 'topic': 'research workflows', 'source_id': 'research',
            'url': notes[0]['url'], 'active': True, 'date': now, 'message_id': 43,
            'request': 'Bring this up when I ask about research workflows.'}}}
    (root / 'private/registry.json').write_text(json.dumps(state), encoding='utf-8')
    calls = []

    def store(operation, payload=None):
        calls.append((operation, payload))
        if operation == 'dashboard_snapshot':
            return snapshot(root=root)
        if operation == 'media':
            return [base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=')]
        if operation == 'reindex':
            return True  # No embedding/index or provider calls in the fixture.
        if operation == 'delete':
            with patch('core.rag.remove_from_index'):
                return dispatch(operation, payload, root=root)
        return dispatch(operation, payload, root=root)

    def redo(name, section):
        calls.append(('redo', {'name': name, 'section': section}))

    def answer(question, store, history):
        calls.append(('answer', {'question': question, 'history': history}))
        return {'answer': 'Keep each note focused on one useful idea, and preserve the original source. [S1]',
                'source_labels': {'S1': 'research.md', 'S2': 'memory:fact', 'S3': 'deleted.md'}}

    app = create_app(store=store, redo=redo, answer=answer, password='local-preview', secure_cookies=False, preview=True)
    app.state.fixture_directory = directory
    app.state.fixture_root = root
    app.state.fixture_calls = calls
    return app
