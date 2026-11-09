"""Source references follow user-message order, never worker completion order."""
import re
import time

from core.requests import canonical_source

CONTEXT_SECONDS = 86400


def note_resolution(records, sources, jobs, source_id):
    """Read the current visibility pointer; never mutate tasks or run extraction."""
    lookup = sources.get(source_id, {})
    note = records.get(lookup.get('active'))
    job = jobs.get(lookup.get('job'), {})
    if note and note.get('section') == 'notes':
        return {'state': 'saved', 'title': note.get('title'), 'name': note.get('name'),
            'review_status': note.get('review_status'), 'note': note}
    status = job.get('status')
    if status in {'accepted', 'running'}:
        return {'state': 'pending'}
    if status in {'failed', 'superseded'}:
        return {'state': 'failed'}
    if status in {'saved', 'cancelled'} or lookup.get('active'):
        return {'state': 'missing'}
    if lookup.get('failed') in records:
        return {'state': 'failed'}
    return {'state': 'unavailable'}


def key(payload):
    return str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)


def describe(state, entry):
    lookup = state['sources'].get(entry['source_id'], {})
    note = state['records'].get(lookup.get('active'), {})
    job = state['jobs'].get(lookup.get('job'), {})
    return {**entry, 'title': note.get('title') or entry.get('title') or entry['url'],
        'note_name': note.get('name'), 'job_id': job.get('job_id'),
        'source_status': 'saved' if note else job.get('status', 'unavailable')}


def register(state, payload, *, role='user'):
    if payload.get('chat_id') is None or payload.get('message_id') is None:
        return
    source = canonical_source(payload['url'])
    entries = state.setdefault('contexts', {}).setdefault(key(payload), [])
    entry = {**source, **{k: payload.get(k) for k in ('message_id', 'user_id', 'chat_id', 'thread_id', 'update_id')},
        'date': payload.get('date') or time.time(), 'received': time.time(), 'role': role}
    entries[:] = [e for e in entries if e['message_id'] != entry['message_id']]
    entries.append(entry)
    del entries[:-500]
    if role == 'user':
        chat = state.setdefault('chats', {}).setdefault(key(payload), {})
        old = chat.get('source_focus') or {}
        # Telegram message IDs give position even for delayed/reordered webhooks.
        if entry['message_id'] >= old.get('message_id', -1):
            chat['source_focus'] = entry


def candidates(state, payload):
    entries = state.get('contexts', {}).get(key(payload), [])
    position = payload.get('message_id')
    usable = [e for e in entries if e.get('role', 'user') == 'user' and
        (position is None or e['message_id'] < position)]
    latest = {}
    for entry in sorted(usable, key=lambda e: e['message_id'], reverse=True):
        latest.setdefault(entry['source_id'], describe(state, entry))
    return list(latest.values())[:12]


def associate(state, payload):
    entries = state.get('contexts', {}).get(key(payload), [])
    reply = payload.get('reply_id')
    if reply is not None:
        found = [e for e in entries if e['message_id'] == reply]
        if found:
            return {'status': 'resolved', **describe(state, found[-1])}
        # Replies to an older Saved message can still identify its current/archived note.
        text = payload.get('reply_text', '')
        names = re.findall(r'note=([^\s&#]+)', text)
        if len(names) == 1:
            from urllib.parse import unquote
            records = [r for r in state['records'].values() if r['name'] == unquote(names[0])]
            if len(records) == 1 and records[0].get('url'):
                return {'status': 'resolved', **describe(state, records[0])}
        answer = state.get('telegram_answers', {}).get(str(payload['chat_id']) + ':' + str(reply), {})
        records = [r for r in state['records'].values() if r['name'] in answer.get('sources', {}).values() and r.get('url')]
        sources = {r['source_id']: describe(state, r) for r in records}
        if len(sources) == 1:
            return {'status': 'resolved', **next(iter(sources.values()))}
        if sources:
            return {'status': 'ambiguous', 'candidates': list(sources.values())}
        return {'status': 'missing', 'candidates': candidates(state, payload)}
    options = candidates(state, payload)
    reference = payload.get('reference', '')
    named = re.search(r'\b(?:the|o|a)\s+(.+?)\s+(?:post|note|video|reel|nota|publicação)\b', reference, re.I)
    quoted = re.search(r'["“]([^"”]+)["”]', reference)
    reference = named[1] if named else quoted[1] if quoted else ''
    if reference:
        # Named references beat focus, but only use distinctive words from known titles.
        stop = {'the', 'this', 'that', 'these', 'those', 'post', 'note', 'video', 'reel', 'link',
            'me', 'i', 'to', 'for', 'about', 'with', 'when', 'ask', 'remember', 'remind',
            'tell', 'again', 'tomorrow', 'o', 'a', 'de', 'do', 'da', 'este', 'esta', 'isso', 'amanha'}
        words = set(re.findall(r'\w+', reference.casefold())) - stop
        scores = [(len(words & set(re.findall(r'\w+', c['title'].casefold()))), c) for c in options]
        best = max((score for score, _ in scores), default=0)
        matched = [c for score, c in scores if score == best and score]
        if len(matched) == 1:
            return {'status': 'resolved', **matched[0]}
        if len(matched) > 1:
            return {'status': 'ambiguous', 'candidates': matched}
        return {'status': 'missing', 'candidates': options}
    chat = state.get('chats', {}).get(key(payload), {})
    focus = chat.get('source_focus')
    if focus and options:
        # A delayed older message is resolved at its own position, not at current focus.
        latest = options[0]
    elif 'source_focus' in chat:
        return {'status': 'missing', 'candidates': options}
    else:
        latest = options[0] if options else None
    event_date = payload.get('date') or time.time()
    source_date = latest.get('date') or latest.get('received', 0) if latest else 0
    if latest and source_date < 946684800:
        source_date = latest.get('received', source_date)
    if event_date < source_date:
        event_date, source_date = time.time(), latest['received']
    if latest and 0 <= event_date - source_date <= CONTEXT_SECONDS:
        return {'status': 'resolved', **latest}
    return {'status': 'missing', 'candidates': options}


def question(association):
    choices = association.get('candidates', [])
    if choices:
        return 'Which source do you mean?\n' + '\n'.join('• ' + c['title'] for c in choices[:6]) + '\nYou can reply to its link or saved-note message.'
    return 'Which post do you mean? Please reply to its link or saved-note message.'
