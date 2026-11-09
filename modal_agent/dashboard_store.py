"""Read model called inside the existing serialized Modal storage function.

No second registry or writes: mutations continue through core.storage.dispatch.
"""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.storage import dispatch


def snapshot(*, root='/data', volume=None):
    listing = dispatch('list', root=root, volume=volume)
    path = Path(root) / 'private/registry.json'
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    # List screens do not need transcripts, drafts, media paths or diagnostics.
    # Keep those behind the authenticated, on-demand note-detail read.
    columns = ('name', 'title', 'section', 'status', 'category', 'creator', 'saved',
               'created', 'review_status', 'coverage', 'index_pending', 'source_id', 'url')
    records = [{key: note[key] for key in columns if key in note} for note in listing['records']]
    active = {r.get('source_id'): r for r in records if r['section'] == 'notes'}

    def linked(item):
        note = active.get(item.get('source_id'))
        return {**item, 'linked_note': {'name': note['name'], 'title': note['title']} if note else None}

    tasks = [linked(t) for t in state.get('reminders', {}).values()]
    memory = [linked(m) for m in state.get('memory', {}).get('items', {}).values()]
    recalls = [linked(r) for r in state.get('recalls', {}).values() if r.get('active')]
    jobs = [{k: j.get(k) for k in ('job_id', 'status', 'created', 'title', 'url', 'source_id')}
            for j in state.get('jobs', {}).values() if j.get('status') in {'accepted', 'running'}]
    now = time.time()
    today = datetime.fromtimestamp(now, ZoneInfo('Europe/Lisbon')).date().isoformat()
    brief = [t['id'] for t in tasks if t['status'] not in {'done', 'cancelled'}
             and t.get('kind') != 'notification' and t.get('repair_status') != 'needs_clarification'
             and t['due'] <= now and (t.get('brief') or t.get('delivery', {}).get('day'))]
    return {'records': records, 'tasks': tasks, 'memory': memory, 'recalls': recalls,
            'jobs': jobs, 'revision': state.get('answer_revision', state.get('memory', {}).get('revision', 0)),
            'brief': {'task_ids': brief, 'date': today, 'time': '09:00', 'timezone': 'Europe/Lisbon'}}
