"""Bounded delivery; callers persist outcomes independently."""
import os
import time
import requests
import re
from urllib.parse import urlsplit


def call(method, payload, *, result=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    for attempt in range(2):
        try:
            response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
            body = response.json()
            if not isinstance(body, dict):
                return False
            if response.status_code == 429 and attempt == 0:
                delay = body.get("parameters", {}).get("retry_after", 0)
                if isinstance(delay, (int, float)) and 0 < delay <= 5:
                    time.sleep(delay)
                    continue
            if response.ok and body.get('ok') is True:
                return body.get('result') if result else True
            return False
        except (requests.RequestException, ValueError):
            return False  # timeout is ambiguous; never regenerate
    return False


def send(chat_id, text, *, capture=False, thread_id=None):
    if chat_id is None:
        return True
    # 1800 Unicode codepoints stay below Telegram's UTF-16 limit even for emoji.
    ids = []
    success = True
    for start in range(0, len(text), 1800):
        payload = {"chat_id": chat_id, "text": text[start:start + 1800]}
        if thread_id is not None:
            payload['message_thread_id'] = thread_id
        result = call('sendMessage', payload, result=capture)
        if not result:
            success = False
            break
        if isinstance(result, dict) and isinstance(result.get('message_id'), int):
            ids.append(result['message_id'])
    return {'success': success, 'message_ids': ids} if capture else success


def react(chat_id, message_id, clear=False):
    if chat_id is None or message_id is None:
        return True
    return call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
        "reaction": [] if clear else [{"type": "emoji", "emoji": "👀"}]})


def formatted(text):
    """Render the answer's small Markdown subset as safe Telegram entities."""
    text = re.sub(r'\s*\[S\d+\]', '', text)
    pattern = re.compile(r'```(?:[^\n`]*\n)?([\s\S]*?)```|`([^`\n]+)`|\*\*(.+?)\*\*|\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(?m:^[#]{1,6} (.+)$)|(?<!\*)\*([^*\n]+)\*(?!\*)')
    pieces, entities, offset, position = [], [], 0, 0
    for match in pattern.finditer(text):
        prefix = text[position:match.start()]
        pieces.append(prefix)
        offset += len(prefix.encode('utf-16-le')) // 2
        groups = match.groups()
        if groups[0] is not None:
            value, kind = groups[0], 'pre'
        elif groups[1] is not None:
            value, kind = groups[1], 'code'
        elif groups[2] is not None:
            value, kind = groups[2], 'bold'
        elif groups[3] is not None:
            value, kind = groups[3], 'text_link'
        elif groups[5] is not None:
            value, kind = groups[5], 'bold'
        else:
            value, kind = groups[6], 'italic'
        length = len(value.encode('utf-16-le')) // 2
        entity = {'type': kind, 'offset': offset, 'length': length}
        if kind == 'text_link':
            entity['url'] = groups[4]
        if length:
            entities.append(entity)
        pieces.append(value)
        offset += length
        position = match.end()
    pieces.append(text[position:])
    return ''.join(pieces), entities


def send_answer(chat_id, text, sources, storage, *, thread_id=None):
    """Save source references against each delivered message, without model work."""
    if chat_id is None:
        return True
    text, entities = formatted(text)
    if sources:
        text += '\n\nReact to this message or tap Sources to see the sources.'
    offset = 0
    for start in range(0, len(text), 1800):
        chunk = text[start:start + 1800]
        end = offset + len(chunk.encode('utf-16-le')) // 2
        clipped = [{**e, 'offset': max(offset, e['offset']) - offset,
            'length': min(end, e['offset'] + e['length']) - max(offset, e['offset'])}
            for e in entities if e['offset'] < end and e['offset'] + e['length'] > offset]
        payload = {'chat_id': chat_id, 'text': chunk, 'entities': clipped}
        if thread_id is not None:
            payload['message_thread_id'] = thread_id
        if sources:
            payload['reply_markup'] = {'inline_keyboard': [[{'text': 'Sources', 'callback_data': 'answer_sources'}]]}
        message = call('sendMessage', payload, result=True)
        if not isinstance(message, dict) or not isinstance(message.get('message_id'), int):
            return False
        if sources:
            storage('answer_sources_put', {'chat_id': chat_id, 'message_id': message['message_id'], 'sources': sources})
        offset = end
    return True


def source_text(records):
    from core.requests import canonical_source
    from urllib.parse import urlencode
    if not records:
        return 'These sources are no longer available.'
    lines = ['Sources:']
    for row in records:
        title = row.get('title', '').strip()
        if not title or title.startswith(('?', 'http', '&')):
            title = 'Saved Instagram note'
        lines.append('\n' + row['label'] + ' — ' + title)
        try:
            lines.append(canonical_source(row['url'])['url'])
        except (ValueError, KeyError):
            pass
        ui = os.environ.get('UI_URL', '')
        if urlsplit(ui).scheme in {'http', 'https'}:
            lines.append(ui.rstrip('/') + '/?' + urlencode({'note': row['name']}))
    return '\n'.join(lines)
