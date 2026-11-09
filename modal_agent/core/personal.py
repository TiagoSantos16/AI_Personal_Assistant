"""Bounded explicit memory; no model interpretation or source-derived facts."""
import re
from datetime import datetime, timezone


def intention(text):
    """An explicitly stated plan, including negation and uncertainty, not a task."""
    return re.fullmatch(r"I (?:(?:really |definitely |still )?(?:need|want|plan|intend|hope) to|"
        r"(?:don't|do not|no longer) (?:want|need) to|might|may|would like to) "
        r"(try|use|learn|visit|read|watch|buy|build|explore)\b([^\n?!]*)[.!]?", text.strip(), re.I)


def source_reference(text):
    patterns = (r'\b(?P<ref>(?:this|that|these|those)\s+(?:reel|post|video|link|tool|offer|source|ideas?))\b',
        r'\b(?:try|use|using|tried|learn|visit|read|watch|watched|buy|build|explore|like|love|hate|prefer|search|check|verify|review|evaluate|investigate|test|install|revisit)\s+'
        r'(?P<ref>this|that|it|these|those)\b',
        r'\b(?P<ref>this|that|it|these|those|isto|isso|estes|estas|esses|essas)\b(?=[.!]?$|\s+(?:to|when|soon|later|today|tomorrow|on|at|in|next|for|before|after|quando|para|amanhã)\b)')
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.span('ref')
    return None


def needs_source(text):
    return source_reference(text) is not None


def contextualize(item, records, sources):
    """Resolve references on read; source workers never rewrite personal memory."""
    result = dict(item)
    if item.get('source_id'):
        record = records.get(sources.get(item['source_id'], {}).get('active'), {})
        title = record.get('title')
        target = title or 'the linked Instagram source (note not available)'
        span = source_reference(item['text'])
        result['text'] = (item['text'][:span[0]] + target + item['text'][span[1]:] if span else item['text'])
        result['text'] += '\nLinked source: ' + item['source_url']
        result['source_name'] = record.get('name')
        result['source_title'] = title
        result['original_text'] = item['text']
    return result


def display(item):
    date = datetime.fromtimestamp(item['date'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return f"{item['id']} [{item['scope']}; {item['kind']}]: {item['text']} (saved: {date})"


def interpret(text):
    """Only whole, first-person statements. Preserve the complete original text."""
    text = text.strip()
    plan = intention(text)
    if plan:
        return {'key': None, 'scope': scope_for(text), 'text': text, 'kind': 'intention',
            'action': plan[1].lower()}
    film = re.fullmatch(r"I (have not watched|haven't watched|watched|have watched) ([^\n?!]+?)[.]?", text, re.I)
    if film:
        title = film[2].rstrip('.').strip()
        return {"key": "film:" + title.casefold(), "scope": "films", "text": text, "kind": "fact"}
    if re.fullmatch(r"I (?:live in|now live in|moved to) [^\n?!]+[.]?", text, re.I):
        return {"key": "residence", "scope": "general", "text": text, "kind": "fact"}
    usage = re.fullmatch(r"I (?:already |currently )?(use|tried|am using|don't use|do not use) ([^\n?!]+)[.]?", text, re.I)
    if usage:
        return {'key': None, 'scope': scope_for(text), 'text': text, 'kind': 'fact', 'action': 'use'}
    subject = re.fullmatch(r'My ([\w ]{1,50}?) (?:is|are) [^\n?!]+', text, re.I)
    if subject:
        return {'key': 'personal:' + subject[1].casefold(), 'scope': scope_for(text), 'text': text, 'kind': 'fact'}
    if re.fullmatch(r"I (?:prefer|like|love|hate) [^\n?!]+[.]?", text, re.I):
        scope = "notes" if re.search(r"\bnotes?\b", text, re.I) else "general"
        # Unrelated preferences must not conflict merely because they share a scope.
        return {"key": 'preference:notes' if scope == 'notes' else None, "scope": scope, "text": text, "kind": "preference"}
    return None


def scope_for(text):
    if re.search(r'\b(allerg\w*|diet\w*|vegan|vegetarian|meat|dairy|gluten|lactose|food)\b', text, re.I):
        return 'culinary'
    if re.search(r'\b(movie|film|watched|seen)\b', text, re.I):
        return 'films'
    return 'general'


def relevant(items, question, scope="answer"):
    words = set(re.findall(r"\w+", question.casefold()))
    films = bool(words & {"movie", "movies", "film", "films", "watch", "watched", "recommend", "recommendation", "filme", "filmes"})
    selected = []
    personal_question = scope != 'notes' and bool(words & {'me', 'my', 'mine', 'myself', 'eu', 'meu', 'minha'})
    task_question = bool(words & {'reminder', 'reminders', 'tasks', 'todo', 'scheduled', 'brief'}) or bool(
        re.search(r'\bwhat do i (?:need|have) to do\b', question, re.I))
    for item in items:
        overlap = words & set(re.findall(r"\w+", item['text'].casefold())) - {"i", "a", "the", "in", "is", "my", "do", "have"}
        personal_subject = ((item.get('key') == 'residence' and bool(words & {'where', 'live', 'home', 'location', 'onde', 'moro'})) or
            (item.get('key') == 'personal:name' and bool(words & {'name', 'called', 'nome'})))
        scopes = {'general', scope}
        for domain, cues in {'coding': {'code', 'coding', 'python'}, 'travel': {'travel', 'trip'},
                'culinary': {'recipe', 'cooking', 'meal', 'food', 'dinner', 'lunch', 'eat', 'diet', 'receita', 'comida'}, 'finance': {'finance', 'money'},
                'career': {'career', 'job'}, 'entertainment': {'film', 'movie', 'book', 'series'}}.items():
            if words & cues:
                scopes.add(domain)
        if personal_question or (task_question and item['kind'] == 'task') or (item['scope'] in scopes and (item['kind'] == 'preference' or item['scope'] != 'general')) or personal_subject or overlap or (films and item['scope'] == 'films'):
            selected.append(item)
    if sum(len(i['text']) for i in selected) > 8000:
        raise ValueError("Relevant memory exceeds context budget; narrow the question or inspect /memory.")
    return selected


def memory_command(state, name, argument, provenance):
    memory = state.setdefault('memory', {'revision': 0, 'items': {}})
    items = memory['items']
    if name == 'memory':
        lines = [display(contextualize(i, state['records'], state['sources'])) +
            (' [needs clarification]' if i.get('repair_status') == 'needs_clarification' else
             ' [moved to source recall]' if not i.get('active', True) else '') for i in items.values()]
        return '\n'.join(lines) or 'No personal memory saved.', False
    if name == 'forget':
        if argument not in items:
            return 'Unknown memory ID. Use /memory.', False
        del items[argument]
        memory['revision'] += 1
        return 'Forgotten from personal memory. Existing notes, answers and Telegram messages are unchanged.', True
    target = None
    if name == 'correct':
        identifier, separator, argument = argument.partition(' ')
        target = items.get(identifier)
        if not target or not separator:
            return 'Usage: /correct <memory ID> <replacement statement>', False
    value = interpret(argument)
    if not value and name in {'remember', 'correct'}:
        # Explicit commands can record arbitrary wording, including uncertainty.
        scope, separator, wording = argument.partition(': ')
        if scope not in {'general', 'notes', 'films', 'coding', 'travel', 'culinary', 'finance', 'career', 'entertainment'} or not separator:
            return 'Use a first-person statement or /remember <scope>: <exact fact or preference>. For ambiguous corrections use /correct <ID> <scope>: <replacement>.', False
        value = {'text': wording, 'scope': scope, 'kind': 'preference' if wording.lower().startswith('i prefer ') else 'fact', 'key': None}
    if not value:
        if name == 'natural' and re.search(r'\b(watched|watch|live|lived|prefer)\b', argument, re.I) and re.match(r'^(I\b|My\b)', argument, re.I):
            return 'Please clarify the personal fact with /remember <scope>: <exact wording>, or use /memory and /correct <ID> <replacement> for a correction.', False
        return None, False
    if not interpret(argument) and re.search(r'\b(this|that|these|those|it|isto|isso|estes|estas|esses|essas)\b', value['text'], re.I):
        return 'What specifically should I remember? Please reply to the relevant post and describe how you want to use it.', False
    if needs_source(value['text']):
        source_url = provenance.get('source_url') or (target or {}).get('source_url')
        if not source_url:
            return 'Which source do you mean? Please reply to its Instagram link so I can remember what you intend to try or use.', False
        from core.requests import canonical_source
        source = canonical_source(source_url)
        value.update(source_id=source['source_id'], source_url=source['url'],
            key=value['kind'] + ':' + source['source_id'] + ':' + value.get('action', 'preference'))
    import uuid
    if not target:
        target = next((i for i in items.values() if (value['key'] and i['key'] == value['key']) or
            (i['scope'] == value['scope'] and i['text'] == value['text'])), None)
    if not value['text'] or len(value['text']) > 1000 or (len(items) >= 200 and not target):
        return 'Memory limit reached (200 entries; 1,000 characters per entry). Nothing changed.', False
    if target and target['text'] == value['text'] and target['scope'] == value['scope']:
        return 'Already remembered: ' + target['id'], False
    identifier = target['id'] if target else uuid.uuid4().hex[:12]
    if target and name != 'correct':
        return 'This conflicts with existing memory. Use /correct ' + identifier + ' <replacement statement>.', False
    if any(i['id'] != identifier and value['key'] and i['key'] == value['key'] for i in items.values()):
        return 'Another memory has that subject. Correct its ID instead.', False
    if target and not value['key']:
        value['key'] = target['key']
    items[identifier] = {**value, **{k: v for k, v in provenance.items() if k != 'source_url'}, 'id': identifier}
    memory['revision'] += 1
    resolved = contextualize(items[identifier], state['records'], state['sources'])
    return 'Remembered: ' + identifier + ' — ' + resolved['text'], True
