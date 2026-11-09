"""Same-origin FastAPI dashboard. React assets are built separately with Vite."""
import logging
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from core.accounting import deduplicate, summary
from core.storage import identifier
from dashboard_auth import DashboardAuth

logger = logging.getLogger(__name__)

def remote_store(operation, payload=None):
    import modal
    return modal.Function.from_name('personal-assistant', 'storage').remote(operation, payload)


def remote_redo(name, section):
    import modal
    modal.Function.from_name('personal-assistant', 'redo_background').spawn(name, section)


def grounded(question, store, history):
    from core.rag import grounded_answer
    return grounded_answer(question, store, history=history)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Note(Input):
    name: str = Field(min_length=1, max_length=255)
    section: Literal['notes', 'failed'] = 'notes'


class Message(Input):
    role: Literal['user', 'assistant']
    content: str = Field(max_length=12000)


class Notes(Input):
    records: list[Note] = Field(min_length=1, max_length=200)


class Question(Input):
    question: str = Field(min_length=1, max_length=4000)
    history: list[Message] = Field(default_factory=list, max_length=12)


class TaskAction(Input):
    action: Literal['done', 'reschedule', 'cancel', 'retry']
    revision: int
    timing: str = Field(default='', max_length=100)


class MemoryAction(Input):
    action: Literal['forget', 'correct']
    revision: int
    text: str = Field(default='', max_length=1000)


def create_app(*, store=remote_store, redo=remote_redo, answer=grounded,
               dist=None, password=None, secure_cookies=True, preview=False):
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    build = Path(dist) if dist else Path(__file__).with_name('dashboard_web') / 'dist'
    auth = DashboardAuth(password, secure_cookies=secure_cookies, preview=preview)
    api.include_router(auth.router, prefix='/api')

    @api.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api/') else 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @api.exception_handler(Exception)
    async def unavailable(request, exc):
        logger.error('Dashboard service failure: path=%s type=%s', request.url.path, type(exc).__name__)
        return JSONResponse({'detail': 'The requested service is unavailable. Please try again shortly.'},
                            status_code=503, headers={'Cache-Control': 'no-store'})

    @api.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': 'The request could not be applied. Refresh and check the selected item or time.'}, status_code=400)

    @api.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': 'This item is no longer available. Refresh your workspace.'}, status_code=404)

    @api.get('/api/workspace', dependencies=[Depends(auth.credential)])
    def workspace():
        return store('dashboard_snapshot')

    def read_note(name, section):
        return store('read', {'name': identifier(name), 'section': section})

    @api.get('/api/note', dependencies=[Depends(auth.credential)])
    def note(name: str, section: Literal['notes', 'failed'] = 'notes'):
        result = read_note(name, section)
        records = result.get('accounting')
        return {**result, 'accounting': deduplicate(records) if records else records,
                'cost_summary': summary(records, result.get('compute'))}

    @api.get('/api/media/{index}', dependencies=[Depends(auth.credential)])
    def media(index: int, name: str, section: Literal['notes', 'failed'] = 'notes'):
        images = store('media', {'name': identifier(name), 'section': section})
        if not 0 <= index < len(images):
            raise HTTPException(404, 'Selected media is no longer available.')
        data = images[index]
        kind = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg'
        return Response(data, media_type=kind)

    @api.post('/api/notes/redo', dependencies=[Depends(auth.mutation)])
    def redo_note(body: Note):
        read_note(body.name, body.section)
        redo(body.name, body.section)
        return {'message': 'Processing requested. The current note stays available until replacement succeeds.'}

    @api.post('/api/notes/delete', dependencies=[Depends(auth.mutation)])
    def delete_notes(body: Notes):
        records = [r.model_dump() for r in body.records]
        for record in records:
            identifier(record['name'])
        store('delete', {'records': records})
        return {'message': 'Note deleted.' if len(records) == 1 else 'Selected notes deleted.'}

    @api.post('/api/reindex', dependencies=[Depends(auth.mutation)])
    def reindex():
        store('reindex')
        return {'message': 'Search index rebuilt.'}

    @api.post('/api/chat', dependencies=[Depends(auth.mutation)])
    def chat(body: Question):
        question = body.question.strip()
        if not question:
            raise HTTPException(422, 'Please enter a question.')
        history = [message.model_dump() for message in body.history]
        if sum(len(m['content']) for m in history) > 12000:
            raise HTTPException(422, 'This conversation is too long. Start a new conversation.')
        result = answer(question, store, history)
        records = {r['name']: r for r in store('list')['records'] if r['section'] == 'notes'}
        sources = [{'label': label, 'name': name, 'title': records[name]['title']}
                   for label, name in result.get('source_labels', {}).items() if name in records]
        return {'answer': result['answer'], 'sources': sources}

    @api.post('/api/tasks/{task_id}', dependencies=[Depends(auth.mutation)])
    def task_action(task_id: str, body: TaskAction):
        snapshot = store('dashboard_snapshot')
        task = next((t for t in snapshot['tasks'] if t['id'] == task_id), None)
        if not task or task['status'] in {'done', 'cancelled'}:
            raise HTTPException(409, 'This task changed. Refresh your workspace.')
        if snapshot['revision'] != body.revision:
            raise HTTPException(409, 'Your tasks changed. Refresh before applying this action.')
        reply = store('reminder', {'command': body.action, 'target': task['id'],
            'timing': body.timing, 'chat_id': task['chat_id'], 'thread_id': task.get('thread_id'),
            'expected_revision': body.revision, 'date': time.time()})
        return {'message': reply}

    @api.post('/api/memory/{memory_id}', dependencies=[Depends(auth.mutation)])
    def memory_action(memory_id: str, body: MemoryAction):
        snapshot = store('dashboard_snapshot')
        item = next((m for m in snapshot['memory'] if m['id'] == memory_id), None)
        if not item:
            raise HTTPException(404, 'Memory is no longer available.')
        if snapshot['revision'] != body.revision:
            raise HTTPException(409, 'Your memory changed. Refresh before applying this action.')
        text = memory_id if body.action == 'forget' else memory_id + ' ' + body.text
        reply = store('memory', {'command': body.action, 'text': text,
            'expected_revision': body.revision, 'chat_id': item.get('chat_id'),
            'thread_id': item.get('thread_id'), 'user_id': item.get('user_id')})
        return {'message': reply}

    @api.post('/api/recall/{recall_id}/forget', dependencies=[Depends(auth.mutation)])
    def forget_recall(recall_id: str):
        snapshot = store('dashboard_snapshot')
        if not any(r['id'] == recall_id for r in snapshot['recalls']):
            raise HTTPException(404, 'Topic recall is no longer available.')
        return {'message': store('recall_forget', {'text': recall_id})}

    if (build / 'assets').is_dir():
        api.mount('/assets', StaticFiles(directory=build / 'assets'), name='assets')

    @api.get('/')
    def index():
        if not (build / 'index.html').is_file():
            raise HTTPException(503, 'Build the dashboard frontend before starting this server.')
        return FileResponse(build / 'index.html')

    return api


app = create_app()
