"""Persistent tasks and separate notification delivery, using the existing registry."""
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = 'Europe/Lisbon'
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
STATUS = {'pending': 'scheduled', 'notified': 'reminded; still open', 'uncertain': 'delivery not confirmed',
    'failed': 'delivery failed; retry or reschedule', 'sending': 'delivery in progress', 'done': 'completed', 'cancelled': 'cancelled'}


def effective_tasks(state):
    """Only task meaning/status changes invalidate answers, never read/focus metadata."""
    return {k: {field: v.get(field) for field in ('text', 'due', 'brief', 'kind', 'source_id', 'repair_status')} |
        {'closed': v['status'] if v['status'] in {'done', 'cancelled'} else 'open'}
        for k, v in state.get('reminders', {}).items()}


def message_now(provenance):
    stamp = provenance.get('date')
    return local_now(stamp if isinstance(stamp, (int, float)) and stamp >= 946684800 else None)


def request(text):
    clean = text.strip().rstrip('.!?').replace('’', "'")
    clean = re.sub(r'^(?:please\s+|(?:can|could|would) you\s+)', '', clean, flags=re.I)
    clean = re.sub(r',?\s+please$', '', clean, flags=re.I)
    lower = clean.casefold()
    if lower in {'show my reminders', 'show reminders', 'my reminders', 'list reminders',
            'what do i need to do', 'what are my reminders', 'my tasks', 'show my tasks'}:
        return {'command': 'list'}
    if lower in {'done', 'complete', 'cancel reminder'}:
        return {'command': 'cancel' if lower == 'cancel reminder' else 'done', 'text': ''}
    clean = clean.replace('’', "'")
    positive = re.split(r",?\s+but\s+(?:I\s+)?(?:haven't|have not|didn't|did not)\b", clean, flags=re.I)[0]
    completed = re.fullmatch(r"I(?:'ve| have)? (?:(?:already|just)\s+)*(called|paid|bought|sent|renewed|booked|tried|finished|completed|installed|tested)\s+(.+)", positive, re.I)
    if completed:
        verbs = {'called': 'call', 'paid': 'pay', 'bought': 'buy', 'sent': 'send', 'renewed': 'renew',
            'booked': 'book', 'tried': 'try', 'installed': 'install', 'tested': 'test', 'finished': '', 'completed': ''}
        return {'command': 'done', 'target': (verbs[completed[1].lower()] + ' ' + completed[2]).strip()}
    if lower in {"i'm done", 'i am done', "that's done", 'that is done', 'done with that', 'that one is finished',
            'i did it', 'i did that', "i've done it", 'i have done it'}:
        return {'command': 'done', 'target': 'that'}
    cancelled = re.fullmatch(r"I (?:don't|do not|no longer) need (?:the |my )?(.+?) reminder(?: any ?more)?", clean, re.I)
    if cancelled:
        return {'command': 'cancel', 'target': cancelled[1]}
    again = re.fullmatch(r'remind me again\s+(.+)', clean, re.I)
    if again:
        return {'command': 'reschedule', 'target': 'that', 'timing': again[1]}
    moved = re.fullmatch(r'(?:move|postpone|push back|reschedule|snooze)\s+(.+?)\s+(?:to |until |for )?'
        r'((?:today|tomorrow|next\b|on\b|at\b|in\b).*)', clean, re.I)
    if moved:
        return {'command': 'reschedule', 'target': moved[1], 'timing': moved[2]}
    if re.fullmatch(r'(tomorrow|next \w+)(?: at .+)? instead', clean, re.I):
        return {'command': 'reschedule', 'target': 'that', 'timing': re.sub(r' instead$', '', clean, flags=re.I)}
    change = re.fullmatch(r'(done with|finished with|done|complete|cancel reminder|cancel|remove|drop|reschedule|snooze|retry reminder)\s+(.+)', clean, re.I | re.S)
    if change:
        commands = {'done with': 'done', 'finished with': 'done', 'done': 'done', 'complete': 'done',
            'remove': 'cancel', 'drop': 'cancel', 'cancel reminder': 'cancel', 'cancel': 'cancel',
            'reschedule': 'reschedule', 'snooze': 'reschedule', 'retry reminder': 'retry'}
        value = re.sub(r'\s+(?:until|to)\s+', ' ', change[2], count=1, flags=re.I)
        return {'command': commands[change[1].lower()], 'text': value, 'target': value}
    if re.match(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:remind me\b|remember to\b|set (?:a )?reminder\b)', clean, re.I):
        return {'command': 'add', 'text': clean}
    return None


def context_key(payload):
    return str(payload['chat_id']) + ':' + str(payload.get('thread_id') or 0)


def focus(state, payload, ids, pending=None):
    contexts = state.setdefault('reminder_contexts', {})
    if pending:
        pending = {**pending, 'versions': {i: [state['reminders'][i][k] for k in ('text', 'due', 'status')]
            for i in ids if i in state.get('reminders', {})}}
    contexts[context_key(payload)] = {'ids': ids, 'date': time.time(), 'pending': pending,
        'origin_message_id': payload.get('message_id')}
    state['reminder_contexts'] = dict(sorted(contexts.items(), key=lambda p: p[1]['date'])[-200:])


def words(text):
    irregular = {'called': 'call', 'paid': 'pay', 'bought': 'buy', 'sent': 'send', 'tried': 'try',
        'renewed': 'renew', 'booked': 'book', 'calling': 'call', 'paying': 'pay', 'trying': 'try',
        'renewal': 'renew', 'payment': 'pay', 'purchase': 'buy', 'installed': 'install', 'tested': 'test', 'skills': 'skill',
        'investigation': 'check', 'investigate': 'check', 'search': 'check', 'checking': 'check', 'verify': 'check'}
    ignored = {'i', 'have', 'already', 'just', 'the', 'a', 'an', 'my', 'to', 'for', 'of', 'with', 'about',
        'reminder', 'reminders', 'task', 'tasks', 'please', 'one', 'it', 'that', 'this', 'done', 'finished'}
    return {irregular.get(w, w) for w in re.findall(r'\w+', re.sub(r"['’]s\b", '', text.casefold())) if w not in ignored}


def candidates(state, payload, target, allowed=None):
    active = [i for i in state.get('reminders', {}).values() if i['chat_id'] == payload['chat_id'] and
        i.get('thread_id') == payload.get('thread_id') and i['status'] not in {'done', 'cancelled'} and
        (allowed is None or i['id'] in allowed)]
    target = re.sub(r'^(?:the |my )|\s+reminder$', '', target.strip(), flags=re.I)
    exact = next((i for i in active if i['id'] == target), None)
    if exact:
        return [exact]
    tokens = words(target)
    if allowed is None and tokens and tokens <= {'try', 'use', 'call', 'pay', 'buy', 'renew', 'book'} and re.search(r'\b(it|this|that)\b', target, re.I):
        ctx = state.get('reminder_contexts', {}).get(context_key(payload), {})
        if 'ids' in ctx and time.time() - ctx.get('date', 0) <= 600:
            active = [i for i in active if i['id'] in ctx['ids']]
    if tokens:
        matches = []
        for item in active:
            due = local_now(item['due'])
            relative = 'today' if due.date() == local_now().date() else 'tomorrow' if due.date() == local_now().date() + timedelta(days=1) else ''
            description = label(item, state) + ' ' + when(item) + ' ' + relative + ' ' + WEEKDAYS[due.weekday()] + ' ' + due.strftime('%I%p').lstrip('0')
            if tokens <= words(description):
                matches.append(item)
        return matches
    ctx = state.get('reminder_contexts', {}).get(context_key(payload), {})
    if allowed is None and time.time() - ctx.get('date', 0) <= 600:
        focused = [i for i in active if i['id'] in ctx.get('ids', [])]
        if 'ids' in ctx:
            return focused
    return active


def clarify(state, payload, options, pending):
    focus(state, payload, [i['id'] for i in options], pending)
    if not options:
        return "I couldn't find an open reminder matching that. Which task did you mean?", True
    names = '\n'.join('• ' + label(i, state) + ' — ' + when(i) for i in options[:6])
    return 'Which reminder do you mean?\n' + names + ('\nYou can describe the task.' if len(options) > 6 else ''), True


def human_reply(text):
    """Keep technical references in inspection views, not ordinary acknowledgements."""
    if text.startswith('Remembered: '):
        text = re.sub(r'^Remembered: [a-f0-9]+ — ', '', text)
        text = re.sub(r'^I am\b', 'you are', text)
        text = re.sub(r"^I'm\b", "you're", text)
        text = re.sub(r'^I\b', 'you', text)
        text = re.sub(r'^My\b', 'your', text)
        text = 'Noted: ' + text
    elif text.startswith('Already remembered: '):
        return 'I already have that noted.'
    return text


def local_now(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, ZoneInfo(ZONE))


def schedule(text, now=None):
    """Extract a bounded explicit date/time; never silently ignore an unknown time."""
    now = now or local_now()
    body = text.strip().rstrip('.!?')
    for original, translated in {'amanhã': 'tomorrow', 'amanha': 'tomorrow', 'hoje': 'today',
            'segunda-feira': 'next monday', 'terça-feira': 'next tuesday', 'quarta-feira': 'next wednesday',
            'quinta-feira': 'next thursday', 'sexta-feira': 'next friday', 'sábado': 'next saturday', 'domingo': 'next sunday'}.items():
        body = re.sub(r'\b' + original + r'\b', translated, body, flags=re.I)
    body = re.sub(r'\b(?:na|no)\s+(next\b)', r'\1', body, flags=re.I)
    body = re.sub(r'\bàs?\s+(\d{1,2})(?:h(\d{2})?)?\b', lambda m: 'at ' + m[1] + ':' + (m[2] or '00'), body, flags=re.I)
    body = re.sub(r'\bon (' + '|'.join(WEEKDAYS) + r')\b', r'next \1', body, flags=re.I)
    relative = re.search(r'\bin (\d+) (minutes?|hours?|days?)\b', body, re.I)
    if relative:
        count = int(relative[1])
        if not 1 <= count <= 365 or re.search(r'\b(today|tomorrow|on \d|at \d|every|daily|weekly)\b', body, re.I):
            raise ValueError('Please give one reminder time, for example “tomorrow at 18:00”.')
        seconds = count * (60 if relative[2].lower().startswith('minute') else 3600 if relative[2].lower().startswith('hour') else 86400)
        body = (body[:relative.start()] + body[relative.end():]).strip()
        if relative[2].lower().startswith('day'):
            date = now.date() + timedelta(days=count)
            due = datetime.combine(date, datetime.min.time(), ZoneInfo(ZONE)).replace(hour=9)
            return body, due.timestamp(), True
        return body, now.timestamp() + seconds, False
    date = now.date() + timedelta(days=1)
    dated = False
    match = re.search(r'\b(today|tomorrow|on \d{4}-\d{2}-\d{2}|next (?:' + '|'.join(WEEKDAYS) + r'))\b', body, re.I)
    if match:
        value = match[0].lower()
        dated = True
        if value == 'today':
            date = now.date()
        elif value.startswith('on '):
            try:
                date = datetime.strptime(value[3:], '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('That date is not valid. Please use YYYY-MM-DD.') from None
        elif value.startswith('next '):
            days = (WEEKDAYS.index(value[5:]) - now.weekday()) % 7 or 7
            date = now.date() + timedelta(days=days)
        body = (body[:match.start()] + body[match.end():]).strip()
    clock = re.search(r'\bat (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b', body, re.I)
    hour, minute = 9, 0
    if clock:
        hour, minute = int(clock[1]), int(clock[2] or 0)
        suffix = (clock[3] or '').lower()
        if not 0 <= minute <= 59 or not 0 <= hour <= 23 or (suffix and not 1 <= hour <= 12):
            raise ValueError('Please use a valid time, for example “at 09:00” or “at 6pm”.')
        if not suffix and clock[2] is None and hour < 13:
            raise ValueError('Do you mean morning or afternoon? Please use “9am” or a 24-hour time such as “21:00”.')
        if suffix:
            hour = hour % 12 + (12 if suffix == 'pm' else 0)
        if not dated:
            date = now.date()
        body = (body[:clock.start()] + body[clock.end():]).strip()
    if re.search(r'\b(today|tomorrow|tonight|yesterday|next|every|everyday|each|daily|weekly|monthly|yearly|weekdays|weekends|weekend|morning|afternoon|evening|noon|midnight|UTC|GMT|CET|EST|on \d|in \d|'
            + '|'.join(WEEKDAYS) + r'|january|february|march|april|may|june|july|august|september|october|november|december)\b|'
            r'\bat\s+(?!(?:home|work|the|my|this|that)\b)|\d{1,2}[:/]\d{2}|\d{1,2}-\d{1,2}|\d{4}-\d{2}|\d\s*(?:am|pm)\b', body, re.I):
        raise ValueError('Please give a clear time, for example “tomorrow at 18:00” or “on 2026-11-03 at 09:00”.')
    due = datetime.combine(date, datetime.min.time(), ZoneInfo(ZONE)).replace(hour=hour, minute=minute)
    if clock and not dated and due.timestamp() <= now.timestamp():
        due += timedelta(days=1)
    if due.timestamp() <= now.timestamp():
        raise ValueError('That time has passed. Please choose a future date and time.')
    # Reject ambiguous/nonexistent local times at daylight-saving transitions.
    valid = {candidate.timestamp() for fold in (0, 1)
        if (candidate := due.replace(fold=fold)).astimezone(timezone.utc).astimezone(ZoneInfo(ZONE)).replace(tzinfo=None) == due.replace(tzinfo=None)}
    if len(valid) != 1:
        raise ValueError('That local time is ambiguous or skipped by daylight saving. Please choose another time.')
    return re.sub(r'\s+', ' ', body).strip(' ,'), due.timestamp(), not bool(clock)


def automatic(text):
    return bool(re.match(r'^I (?:really |definitely |still )?(?:need|want|plan|intend|have) to\b|^I would like to\b', text, re.I)
        and not re.search(r"\b(don't|do not|no longer|not|never)\b", text, re.I))


def action(text):
    text = re.sub(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:remind me(?:\s+to)?|remember to|set (?:a )?reminder(?:\s+to)?)\s+', '', text, flags=re.I)
    text = re.sub(r'^I (?:(?:really |definitely |still )?(?:need|want|plan|intend|have) to|would like to)\s+', '', text, flags=re.I)
    text = re.sub(r'^to\s+', '', text.strip(), flags=re.I)
    return text[:1].upper() + text[1:]


def unfreeze_reference(text):
    """Recognize only our exact former display placeholder, not arbitrary wording."""
    return text.replace('the linked Instagram source (note not available)', 'this')


def label(item, state):
    from core.personal import source_reference
    from core.source_context import note_resolution
    if not item.get('source_id'):
        return item['text']
    resolved = note_resolution(state['records'], state['sources'], state.get('jobs', {}), item['source_id'])
    # Keep stored wording intact. Presentation resolves the subject from the current
    # registry pointer, so redo and completion cannot overwrite task state.
    text = unfreeze_reference(item['text'])
    span = source_reference(text)
    target = resolved.get('title') or ('the linked reel' if '/reel/' in item.get('source_url', '') else 'the linked post')
    if span:
        text = text[:span[0]] + target + text[span[1]:]
    elif resolved.get('title') and resolved['title'] not in text:
        # A model/older record may already contain a standalone description.
        # Keep that action intact and identify the current note separately.
        text += '\nSource note: ' + resolved['title']
    # A narrow grammatical normalization; the requested action and context remain.
    text = re.sub(r'\bin my website\b', 'on my website', text, flags=re.I)
    status = {'pending': 'The source is still processing.', 'failed': 'Source processing failed; the reminder is still saved.',
        'missing': 'The saved note is missing or was deleted.', 'unavailable': 'No saved note is available for this source.'}.get(resolved['state'])
    if resolved['state'] == 'saved' and resolved.get('review_status') in {'needs_review', 'unverified'}:
        status = 'Saved note; review is incomplete.'
    url = item.get('source_url')
    if not url:
        # Old records may retain only the stable identity. Do not guess a new source.
        url = (resolved.get('note') or {}).get('url')
    return text + ('\nLinked source: ' + url if url else '') + ('\n' + status if status else '')


def when(item):
    due = local_now(item['due'])
    return due.strftime('%d %b %Y at %H:%M') + ' (Lisbon)'


def acknowledgement(item):
    from core.voice import reminder_ack
    return reminder_ack(when(item), item['brief'])


def add(state, text, provenance, *, memory_id=None, now=None, description=None, timing_info=None):
    from core.personal import needs_source
    from core.requests import canonical_source
    if not isinstance(provenance.get('chat_id'), int):
        raise ValueError('Please ask me in Telegram so I know where to send the reminder.')
    body, due, brief = schedule(action(text), now or message_now(provenance))
    if description is not None:
        body = description
    if timing_info:
        due, brief = timing_info
    body = action(body)
    if not body or len(body) > 1000:
        raise ValueError('Please give a reminder of between 1 and 1,000 characters.')
    source = {}
    if needs_source(body) or needs_source(text) or provenance.get('revisit') or provenance.get('bind_source'):
        if not provenance.get('source_url'):
            raise ValueError('Please reply to the intended link so I know what the reminder is about.')
        ref = canonical_source(provenance['source_url'])
        source = {'source_url': ref['url'], 'source_id': ref['source_id']}
    items = state.setdefault('reminders', {})
    existing = next((i for i in items.values() if
        i['chat_id'] == provenance['chat_id'] and i.get('thread_id') == provenance.get('thread_id') and
        ((provenance.get('message_id') is not None and i.get('message_id') == provenance['message_id'] and
          i.get('source_text') == provenance.get('source_text') and i['text'] == body) or
        (i['status'] not in {'done', 'cancelled'} and ((memory_id and i.get('memory_id') == memory_id) or
        (i['text'] == body and i.get('source_id') == source.get('source_id') and i['due'] == due))))), None)
    if existing:
        return existing
    if sum(i['status'] not in {'done', 'cancelled'} for i in items.values()) >= 200:
        raise ValueError('You have 200 open reminders. Complete or cancel some before adding more.')
    closed = sorted((i for i in items.values() if i['status'] in {'done', 'cancelled'}),
        key=lambda i: i.get('updated', i['created']))
    for old in closed[:-200]:
        del items[old['id']]
    identifier = uuid.uuid4().hex[:10]
    item = {**{k: provenance.get(k) for k in ('chat_id', 'thread_id', 'message_id', 'source_text', 'date')},
        **source, 'id': identifier, 'text': body, 'due': due, 'brief': brief, 'status': 'pending',
        'memory_id': memory_id, 'created': time.time(), 'updated': time.time(),
        'user_id': provenance.get('user_id'), 'kind': provenance.get('task_kind', 'task'),
        'delivery': {'history': []}}
    items[identifier] = item
    return item


def split_work(body):
    """Expand explicit conjunct actions; retain the original request on every item."""
    tests = re.findall(r'\((?:and |e )?((?:test|testar)\b[^)]*)\)', body, re.I)
    base = re.sub(r'\((?:and |e )?(?:test|testar)\b[^)]*\)', '', body, flags=re.I)
    parts = re.split(r',?\s+(?:and|e)\s+(?=(?:install|test|call|pay|buy|check|investigate|instalar|testar)\b)', base, flags=re.I)
    if len(parts) == 1 and re.match(r'install\b', base, re.I):
        parts = re.split(r',\s*and\s+', base, flags=re.I)
        parts = [p if re.match(r'install\b', p.strip(), re.I) else 'Install ' + p.strip() for p in parts]
    return [re.sub(r'\s+', ' ', p).strip(' ,') for p in parts + tests if p.strip(' ,')]


def add_request(state, text, provenance):
    body, due, brief = schedule(action(text), message_now(provenance))
    if provenance.get('description'):
        body = provenance['description']
    provenance = {**provenance, 'source_text': provenance.get('source_text') or text}
    pieces = split_work(body)
    if len(pieces) > 8:
        raise ValueError('There are too many separate actions in one request. Please split it into smaller messages.')
    created = [add(state, text, provenance, description=part, timing_info=(due, brief)) for part in pieces]
    if len(pieces) > 1:
        for item in created:
            item['group_request'] = body
    return created


def sync_memory(state, previous, provenance):
    """Memory mutation and its associated reminder share one atomic registry commit."""
    current = state['memory']['items']
    messages = []
    for item in state.get('reminders', {}).values():
        mid = item.get('memory_id')
        if mid and (mid not in current or previous.get(mid) != current.get(mid)) and item['status'] not in {'cancelled', 'done'}:
            item['status'] = 'cancelled'
    for mid, item in current.items():
        if previous.get(mid) == item or item['kind'] != 'intention':
            continue
        old = [r for r in state.get('reminders', {}).values() if r.get('memory_id') == mid]
        for reminder in old:
            reminder['status'] = 'cancelled'
        if automatic(item['text']):
            try:
                reminder = add(state, item['text'], {**provenance, 'source_url': item.get('source_url')}, memory_id=mid)
                focus(state, provenance, [reminder['id']])
                messages.append(acknowledgement(reminder))
            except ValueError as exc:
                messages.append('Your intention is saved, but I could not schedule its reminder: ' + str(exc))
    return '\n'.join(messages)


def command(state, payload):
    items = state.setdefault('reminders', {})
    kind, text = payload['command'], payload.get('text', '').strip()
    chat = payload['chat_id']
    if kind == 'list':
        active = sorted((i for i in items.values() if i['chat_id'] == chat and i.get('thread_id') == payload.get('thread_id') and i['status'] not in {'done', 'cancelled'}), key=lambda i: i['due'])
        focus(state, payload, [i['id'] for i in active])
        return ('Here are your reminders:\n' + '\n\n'.join(f"• {label(i, state)}\n{when(i)} · {STATUS[i['status']]}" +
            (' · needs clarification; cancel and restate with a source reply' if i.get('repair_status') == 'needs_clarification' else '') for i in active)) if active else 'You have no open reminders.', True
    if kind == 'add':
        added = add_request(state, text, payload)
        added = [item for item in added if item['status'] not in {'done', 'cancelled'}]
        if not added:
            return "That task is already completed or cancelled. I haven't scheduled it again.", False
        focus(state, payload, [item['id'] for item in added])
        return acknowledgement(added[0]) + '\n' + '\n'.join('• ' + label(item, state) for item in added), True
    ctx = state.get('reminder_contexts', {}).get(context_key(payload), {})
    pending = ctx.get('pending') if time.time() - ctx.get('date', 0) <= 600 else None
    allowed = None
    resolving = kind == 'resolve'
    if resolving:
        if not pending:
            return 'Which reminder would you like to change?', False
        kind, timing, target = pending['command'], pending.get('timing', ''), text
        allowed = ctx['ids']
        if any(i not in items or [items[i][k] for k in ('text', 'due', 'status')] != version
                for i, version in pending.get('versions', {}).items()):
            focus(state, payload, [])
            return 'Those reminders have changed since I asked. What would you like to change now?', True
        if pending.get('needs_time') and len(allowed) == 1:
            target, timing = allowed[0], text
        ordinal = {'first': 0, 'first one': 0, '1': 0, 'second': 1, 'second one': 1, '2': 1,
            'third': 2, 'third one': 2, '3': 2}.get(re.sub(r'^the ', '', target.casefold()))
        if ordinal is not None and ordinal < len(allowed):
            target = allowed[ordinal]
    elif 'target' in payload:
        target, timing = payload['target'], payload.get('timing', '')
    else:
        identifier, _, timing = text.partition(' ')
        target = identifier if identifier in items or kind == 'reschedule' else text
    message = state.get('reminder_messages', {}).get(context_key(payload) + ':' + str(payload.get('reply_id')))
    if message:
        allowed = message['ids']
        number = re.fullmatch(r'(?:item |number |task )?(\d+)', target.strip(), re.I)
        if number:
            target = message.get('numbers', {str(n + 1): i for n, i in enumerate(allowed)}).get(number[1], '__missing__')
    options = candidates(state, payload, target, allowed)
    if len(options) != 1:
        if resolving and not options and allowed:
            options = candidates(state, payload, '', allowed)
        return clarify(state, payload, options, {'command': kind, 'timing': timing})
    item = options[0]
    identifier = item['id']
    if kind in {'done', 'cancel'}:
        if timing and 'target' not in payload and text.partition(' ')[0] in items:
            return 'I can cancel a reminder now, but scheduled cancellation is not supported.', False
        item['status'] = 'done' if kind == 'done' else 'cancelled'
        item['updated'] = time.time()
        item.setdefault('changes', []).append({'command': kind, 'date': payload.get('date'), 'message_id': payload.get('message_id')})
        focus(state, payload, [])
        return ('Marked as done: ' if kind == 'done' else 'Cancelled: ') + label(item, state), True
    if kind in {'reschedule', 'retry'}:
        if kind == 'reschedule':
            if not timing:
                focus(state, payload, [identifier], {'command': 'reschedule', 'timing': '', 'needs_time': True})
                return 'When would you like me to remind you about ' + label(item, state) + '?', True
            leftover, due, brief = schedule(timing, message_now(payload))
            if leftover.casefold() not in {'', 'soon', 'later'}:
                raise ValueError('Please give a date or time, for example “tomorrow at 18:00”.')
            item.update(due=due, brief=brief)
        else:
            item['due'] = time.time()
            item['brief'] = False
        item['status'] = 'pending'
        item['attempts'] = 0
        item['updated'] = time.time()
        item.setdefault('changes', []).append({'command': kind, 'date': payload.get('date'), 'message_id': payload.get('message_id')})
        item.setdefault('delivery', {}).pop('day', None)
        item.pop('retry_after', None)
        focus(state, payload, [identifier])
        return acknowledgement(item) + '\n' + label(item, state), True
    raise ValueError('Unsupported reminder operation')


def memory_items(state):
    return [{'id': 'reminder:' + i['id'], 'text': label(i, state) + '\nReminder: ' + when(i) + '; ' + i['status'],
        'date': i['created'], 'scope': 'general', 'kind': 'task', 'key': None,
        **{k: i[k] for k in ('source_id', 'source_url') if k in i}}
        for i in state.get('reminders', {}).values() if i['status'] not in {'done', 'cancelled'} and i.get('repair_status') != 'needs_clarification']


def deliver(chat_id, text, thread_id=None):
    """One attempt. Unknown delivery must not be automatically resent."""
    import requests
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        return 'failed'
    payload = {'chat_id': chat_id, 'text': text}
    if thread_id:
        payload['message_thread_id'] = thread_id
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload, timeout=10)
        body = response.json()
        if response.ok and isinstance(body, dict) and body.get('ok') is True:
            result = body.get('result')
            message_id = result.get('message_id') if isinstance(result, dict) else None
            return {'status': 'sent', 'message_id': message_id} if isinstance(message_id, int) else 'sent'
        if isinstance(body, dict) and body.get('ok') is False:
            return 'retry' if response.status_code == 429 or response.status_code >= 500 else 'failed'
    except (requests.RequestException, ValueError):
        pass
    return 'uncertain'


def due_items(state, now):
    """Select due work and normalize legacy state; never send or persist here."""
    from core.personal import needs_source
    today = local_now(now)
    day = today.date().isoformat()
    items = state.setdefault('reminders', {})
    changed = False
    due = []
    for item in items.values():
        if item['status'] in {'done', 'cancelled'}:
            continue
        delivery = item.setdefault('delivery', {'history': []})
        delivery.setdefault('history', [])
        if item['status'] == 'sending':
            item['status'] = 'uncertain'
            delivery.setdefault('day', day)
            delivery['outcome'] = 'uncertain'
            changed = True
        if needs_source(item['text']) and not item.get('source_id') and item.get('repair_status') != 'needs_clarification':
            item['repair_status'] = 'needs_clarification'
            changed = True
        if item.get('repair_status') == 'needs_clarification' or item['due'] > now:
            continue
        notification = item.get('kind') == 'notification'
        if notification and item['status'] in {'notified', 'uncertain', 'failed'}:
            continue
        # Legacy successful delivery was not completion. Its timestamp is retained
        # in retry_after in the old schema; avoid duplicating that day's brief.
        if not delivery.get('day') and item['status'] in {'notified', 'uncertain'}:
            delivery['day'] = local_now(item['retry_after'] - 300).date().isoformat() if item.get('retry_after') else day
            delivery['outcome'] = 'sent' if item['status'] == 'notified' else 'uncertain'
            changed = True
        if delivery.get('day') == day:
            if delivery.get('outcome') != 'retry' or delivery.get('attempts', 0) >= 3:
                continue
            if item.get('retry_after', 0) > now:
                continue
        elif delivery.get('day') and today.hour < 9:
            continue
        if item['brief'] and today.hour < 9:
            continue
        due.append(item)
    return due, changed


def tick(state, persist):
    """Serialized delivery attempts; open work recurs independently of delivery."""
    now = time.time()
    today = local_now(now)
    day = today.date().isoformat()
    due, changed = due_items(state, now)
    if changed:
        persist()
    due.sort(key=lambda i: (i['due'], i['id']))
    if not due:
        return {'status': 'idle'}
    first = due[0]
    is_brief = first['brief'] or bool(first['delivery'].get('day'))
    batch = [i for i in due if i['chat_id'] == first['chat_id'] and i.get('thread_id') == first.get('thread_id') and
        ((i['brief'] or bool(i['delivery'].get('day'))) if is_brief else i['id'] == first['id'])]
    from core.voice import brief_header
    header = brief_header(today.hour < 12) if is_brief else 'A reminder for you:'
    page, text = [], header
    for item in batch:
        value = label(item, state)
        if len(header + value) > 1750:
            from core.personal import source_reference
            span = source_reference(item['text'])
            record = state['records'].get(state['sources'].get(item.get('source_id'), {}).get('active'), {})
            subject = record.get('title', 'the linked source')[:300]
            value = item['text'][:span[0]] + subject + item['text'][span[1]:] if span else item['text']
            if item.get('source_url'):
                value += '\n' + item['source_url']
        line = '\n\n' + str(len(page) + 1) + '. ' + value
        if len(text + line) > 1800:
            break
        page.append(item)
        text += line
    for item in page:
        delivery = item['delivery']
        attempts = delivery.get('attempts', 0) if delivery.get('day') == day else 0
        delivery.update(day=day, outcome='sending', attempts=attempts + 1)
        item['status'] = 'sending'
    persist()  # Durable before HTTP. A timeout is not evidence of non-delivery.
    outcome = deliver(first['chat_id'], text, first.get('thread_id'))
    message_id = outcome.get('message_id') if isinstance(outcome, dict) else None
    outcome = outcome['status'] if isinstance(outcome, dict) else outcome
    for item in page:
        delivery = item['delivery']
        delivery['outcome'] = outcome
        delivery['history'].append({'date': now, 'outcome': outcome, 'message_id': message_id})
        delivery['history'] = delivery['history'][-90:]
        item.update(status='notified' if outcome == 'sent' else 'pending' if outcome == 'retry' and
            delivery['attempts'] < 3 else 'uncertain' if outcome == 'uncertain' else 'failed',
            attempts=delivery['attempts'], retry_after=now + 300)
    if outcome == 'sent':
        ids = [i['id'] for i in page]
        focus(state, first, ids)
        if message_id is not None:
            messages = state.setdefault('reminder_messages', {})
            messages[context_key(first) + ':' + str(message_id)] = {'ids': ids,
                'numbers': {str(n + 1): i for n, i in enumerate(ids)}, 'date': now}
            state['reminder_messages'] = dict(sorted(messages.items(), key=lambda p: p[1]['date'])[-1000:])
    persist()
    return {'status': outcome, 'count': len(page)}
