"""User-requested source resurfacing, separate from facts and scheduled tasks."""
import re
import time
import unicodedata
import uuid
from functools import lru_cache
from pathlib import Path

from core.requests import canonical_source


def tokens(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    aliases = {'nichos': 'niche', 'nicho': 'niche', 'niches': 'niche', 'ideas': 'idea', 'ideias': 'idea',
        'ideia': 'idea', 'channels': 'channel', 'canal': 'channel', 'canais': 'channel',
        'inspiration': 'idea', 'inspiracao': 'idea', 'suggestions': 'idea', 'sugestoes': 'idea',
        'opportunities': 'opportunity', 'oportunidades': 'opportunity', 'negocios': 'business',
        'negocio': 'business'}
    stop = {'the', 'a', 'an', 'i', 'me', 'my', 'some', 'give', 'for', 'about', 'of', 'to', 'when',
        'ask', 'please', 'o', 'os', 'as', 'de', 'do', 'da', 'para', 'por', 'me', 'umas', 'algumas', 'sobre'}
    return {aliases.get(w, w) for w in re.findall(r'\w+', text) if w not in stop}


def add(state, payload):
    source = canonical_source(payload['source_url'])
    topic = payload['topic'].strip().rstrip('.!?')
    if not 1 <= len(topic) <= 300 or not tokens(topic):
        raise ValueError('Please describe the topic you want this source brought up for.')
    rules = state.setdefault('recalls', {})
    meaning = tokens(topic) - {'idea', 'information'} or tokens(topic)
    existing = next((r for r in rules.values() if r['active'] and r['source_id'] == source['source_id'] and
        (tokens(r['topic']) - {'idea', 'information'} or tokens(r['topic'])) == meaning), None)
    if existing:
        return existing, False
    if sum(r['active'] for r in rules.values()) >= 200:
        raise ValueError('You have 200 active source recall requests. Remove some before adding more.')
    identifier = uuid.uuid4().hex[:12]
    rule = {**source, 'id': identifier, 'topic': topic, 'request': payload.get('source_text', ''),
        'active': True, 'created': time.time(), 'date': payload.get('date') or time.time(),
        **{k: payload.get(k) for k in ('chat_id', 'user_id', 'thread_id', 'message_id', 'update_id')}}
    rules[identifier] = rule
    return rule, True


def title(state, rule):
    note = state['records'].get(state['sources'].get(rule['source_id'], {}).get('active'), {})
    return note.get('title') or rule['url']


def inspect(state):
    active = [r for r in state.get('recalls', {}).values() if r['active']]
    return ('\n\nSources to bring up later:\n' + '\n'.join('• ' + title(state, r) + ' — when you ask about ' + r['topic'] for r in active)) if active else ''


@lru_cache(maxsize=256)
def _vector(text):
    from core.rag import _embed_passages
    return tuple(_embed_passages([text])[0])


def match(state, question):
    query = tokens(question)
    rules = [r for r in state.get('recalls', {}).values() if r['active']]
    scored = []
    for rule in rules:
        topic = tokens(rule['topic'])
        overlap = len(query & topic)
        # Generic 'ideas' alone must not bring every unrelated saved topic into a query.
        specific = (query & topic) - {'idea', 'help', 'information'}
        score = overlap / max(1, len(topic)) if specific else 0
        if score == 0 and query & (tokens(title(state, rule)) - {'idea', 'help', 'information'}):
            score = .6
        if score:
            scored.append((score, rule))
    if not scored and rules and query:
        # Reuse the existing local BGE model. No provider call, download or new service.
        # The deployed image already contains these weights; no optional model fallback.
        try:
            from core import rag
            # Avoid loading embeddings for an empty/unindexed installation.
            if rag._model is not None or Path(rag.VECTOR_DIR).exists():
                q = _vector(' '.join(sorted(query)))
                for rule in rules:
                    v = _vector(rule['topic'])
                    dot = sum(a * b for a, b in zip(q, v))
                    norm = (sum(a * a for a in q) * sum(b * b for b in v)) ** .5
                    similarity = dot / norm if norm else 0
                    if similarity >= .72:
                        scored.append((similarity, rule))
        except Exception:
            pass  # ordinary note retrieval remains available
    return [r for _, r in sorted(scored, key=lambda pair: pair[0], reverse=True)[:5]]
