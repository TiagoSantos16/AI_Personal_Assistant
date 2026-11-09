"""Telegram intent routing. Local rules first; models never choose tool arguments."""
import re
from core.personal import interpret

CLARIFY = 'What would you like me to do with this? You can describe the result you want.'
SCOPES = {'general', 'notes', 'films', 'coding', 'travel', 'culinary', 'finance', 'career', 'entertainment'}


def quoted(message):
    return bool(any(message.get(k) for k in ('forward_origin', 'forward_from', 'forward_from_chat')) or
        any(e.get('type') in {'blockquote', 'expandable_blockquote'} for e in message.get('entities', [])))


def local_route(text, context):
    clean = text.strip()
    lower = clean.casefold().rstrip('.!')
    pending = context.get('pending')
    choices = {'answer': 'answer', 'answer it': 'answer', 'revise': 'revise', 'revise the note': 'revise',
        'your answer': 'answer', 'the note': 'revise', 'remember': 'remember', 'remember it': 'remember'}
    if pending and lower in choices:
        return {'intent': choices[lower], 'text': pending['text'], 'pending': pending}
    if pending and lower in {'yes', 'sim'}:
        return {'intent': 'clarify', 'text': pending['text'], 'pending': pending}
    if pending and lower in {'no', 'cancel', 'never mind', 'nevermind', 'não'}:
        return {'intent': 'reply', 'reply': "Okay, I won't change anything."}
    if lower in {'hi', 'hello', 'hey', 'olá', 'ola', 'bom dia', 'good morning'}:
        from core.voice import greeting
        return {'intent': 'reply', 'reply': greeting()}
    if lower in {'thanks', 'thank you', 'obrigado', 'obrigada', 'ok', 'okay'}:
        return {'intent': 'reply', 'reply': "You're welcome."}
    if lower in {'show my notes', 'list my notes', 'show notes', 'my notes'}:
        return {'intent': 'notes'}
    if lower in {'what do you remember about me', 'what do you remember about me?', 'show my memory', 'show memory'}:
        return {'intent': 'memory'}
    if lower in {'dashboard', 'dashboard link', 'open the dashboard', 'show the dashboard'}:
        return {'intent': 'link'}
    from core.understanding import contextual
    full_request = contextual(clean)
    if full_request:
        return full_request
    from core.reminders import request as reminder_request
    reminder = reminder_request(clean)
    if reminder:
        return {'intent': 'reminder', **reminder}
    if context.get('reminder_pending') and len(clean) <= 200 and '?' not in clean and not re.match(
            r'^(I\b|My\b|remember\b|forget\b|correct\b|what\b|how\b|why\b|show\b|list\b|cancel\b)', clean, re.I):
        return {'intent': 'reminder', 'command': 'resolve', 'text': clean}
    if re.match(r'^(set (?:a |an )?alarm|delete (?:the |my |a )?notes?)\b', clean, re.I):
        return {'intent': 'reply', 'reply': 'I can schedule a reminder, but alarms and automatic note deletion are not supported. For a reminder, say “remind me to…” followed by the task.'}
    request = re.fullmatch(r'(?:please\s+|(?:can|could|would) you\s+)(remember|forget|correct)\s+(.+)', clean, re.I | re.S)
    if request:
        action, argument = request[1].lower(), request[2].rstrip('?').strip()
        if action == 'correct' and re.search(r'\b(?:this|that|the|my) (?:note|post|video|link|reel)\b', argument, re.I):
            return {'intent': 'revise', 'text': clean}
        if action == 'remember':
            argument = re.sub(r'^that\s+', '', argument, flags=re.I)
            if re.match(r'^to\b', argument, re.I):
                return {'intent': 'reminder', 'command': 'add', 'text': 'Remember ' + argument}
        return {'intent': action, 'text': argument, 'explicit_memory': action == 'remember'}
    if re.match(r'^(forget|correct)\b', clean, re.I):
        action, _, argument = clean.partition(' ')
        if action.lower() == 'correct' and re.search(r'\b(?:this|that|the|my) (?:note|post|video|link|reel)\b', argument, re.I):
            return {'intent': 'revise', 'text': clean}
        return {'intent': action.lower(), 'text': argument.strip()}
    if re.match(r'^(actually\b|correction:)', clean, re.I):
        return {'intent': 'correct', 'text': re.sub(r'^(actually\s*,?\s*|correction:\s*)', '', clean, flags=re.I)}
    if re.match(r'^(I\b|My\b)', clean, re.I) and re.search(
            r'(?:\b(?:and|also)\b|\.\s+).*(?:recommend|suggest|can you|rewrite|forget|what|how)\b', clean, re.I):
        return {'intent': 'clarify', 'text': clean}
    source_focus = bool(re.search(r'\b(this|that) (post|note|video|link|reel)\b|\bfor this one\b', clean, re.I))
    personal_value = interpret(clean)
    temporary = re.search(r"\b(today|for now|this time|just this once|don't save|do not save|don't remember|do not remember)\b", clean, re.I)
    no_save = re.search(r"\b(don't save|do not save|don't remember|do not remember)\b", clean, re.I)
    if temporary and personal_value and (personal_value['kind'] != 'intention' or no_save):
        return {'intent': 'revise' if context.get('has_source') else 'answer', 'text': clean}
    revision = re.match(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:rewrite|revise|reformat|shorten|simplify|translate|'
        r'make (?:it|this|that|(?:this|that|the) note) (?:shorter|longer|simpler|more detailed)|use (?:bullet points|bullets|english|portuguese)|'
        r'focus on|include|more detail|less detail|write (?:it |the note )?in)\b', clean, re.I)
    value = interpret(clean)
    note_preference = source_focus and value and value['kind'] == 'preference' and re.search(
        r'\b(notes?|summary|summaries|format|detail|concise|brief|language|code)\b', clean, re.I)
    note_request = re.match(r'^I (?:want|need|would like)\b', clean, re.I) and re.search(
        r'\b(?:bullet points|(?:the|this|that) note|notes? (?:in|to be)|summary (?:in|to be))\b', clean, re.I)
    if revision or note_preference or note_request:
        if context.get('reply_is_assistant') or (context.get('history') and not source_focus and not context.get('has_source')):
            return {'intent': 'answer', 'text': clean}
        if context.get('history') and context.get('has_source') and not source_focus and not context.get('reply_to_source'):
            return {'intent': 'clarify', 'text': clean}
        return {'intent': 'revise', 'text': clean}
    if re.match(r'^(?:can|could|would) you\s+(?!(?:explain|recommend|suggest|give me|tell me about|find|answer)\b)', clean, re.I):
        return None  # Interpret an indirect action request before treating '?' as a question.
    if ('?' in clean or re.match(r'^(what|why|how|when|where|who|which|is|are|do|does|did|can|could|should|would|'
            r'compare|contrast|compara\w*|recommend|suggest|explain|find|tell me|give me|another|e |qual|quais|como|onde|quando|porqu[eê]|recomenda|sugere|diz-me|dá-me|mostra-me)\b', clean, re.I)):
        return {'intent': 'answer', 'text': clean}
    if value:
        return {'intent': 'remember', 'text': clean}
    if re.match(r'^I (?:need|have|want|plan|intend) to\b', clean, re.I):
        return {'intent': 'reminder', 'command': 'add', 'text': clean}
    remember = re.match(r'^remember(?:\s+that|:)?\s+(.+)$', clean, re.I | re.S)
    if remember and not re.match(r'to\b', remember[1], re.I):
        return {'intent': 'remember', 'text': remember[1], 'explicit_memory': True}
    return None


def route(text, context):
    result = local_route(text, context)
    if result is not None:
        return result
    from core.decisions import message_intent
    return {**message_intent(text, context), 'text': text}


def memory_request(intent, text, snapshot, provenance, scope='general'):
    """Resolve a memory target locally; uncertain targets never mutate storage."""
    payload = {**provenance, 'expected_revision': snapshot['revision'], 'source_text': provenance.get('source_text', text)}
    if intent == 'memory':
        return {**payload, 'command': 'memory'}
    if intent == 'remember':
        from core.personal import needs_source
        value = interpret(text)
        contextual_memory = value and value['scope'] != 'notes' and needs_source(text)
        ambiguous = ('?' in text or (re.search(r'\b(?:this|that) (?:post|note|video|link|reel)\b', text, re.I) and not contextual_memory) or
            re.search(r'\b(?:and|also)\b.*(?:recommend|suggest|can you|rewrite|forget|what|how)\b', text, re.I) or
            text.casefold().strip('.! ') in {'it', 'this', 'that'} or re.match(r'^to\b', text, re.I))
        personal = (re.match(r"^(I\b|I'm\b|My\b|Eu\b|O meu\b|A minha\b|Je\b)", text, re.I) and
            not re.search(r'\b(?:says|said|transcript)\b', text, re.I))
        if ambiguous or not (personal or provenance.get('explicit_memory')):
            return None
        from core.personal import scope_for
        scope = scope_for(text) if scope == 'general' else scope
        argument = text if interpret(text) or text.partition(': ')[0] in SCOPES else (scope if scope in SCOPES else 'general') + ': ' + text
        return {**payload, 'command': 'remember', 'text': argument}
    items = snapshot['items']
    identifier, _, replacement = text.partition(' ')
    exact = next((i for i in items if i['id'] == identifier), None)
    if exact:
        if intent == 'forget':
            return {**payload, 'command': 'forget', 'text': identifier} if not replacement else None
        if replacement:
            value = replacement if interpret(replacement) or replacement.partition(': ')[0] in SCOPES else exact['scope'] + ': ' + replacement
            return {**payload, 'command': 'correct', 'text': identifier + ' ' + value}
    value = interpret(text)
    if intent == 'correct' and value and value.get('kind') == 'intention' and provenance.get('source_url'):
        from core.personal import needs_source
        from core.requests import canonical_source
        if needs_source(text):
            value['key'] = 'intention:' + canonical_source(provenance['source_url'])['source_id'] + ':' + value['action']
    if intent == 'correct' and value and value['key']:
        matches = [i for i in items if i['key'] == value['key']]
    elif intent == 'forget':
        subject = re.sub(r'^(?:that\s+)?', '', text, flags=re.I).strip().rstrip('.').casefold()
        if subject in {'', 'it', 'that', 'this', 'everything', 'all'} or len(subject) < 3:
            return None
        matches = [i for i in items if subject in i['text'].casefold()]
    else:
        return None
    if len(matches) != 1:
        return None
    target = matches[0]
    return {**payload, 'command': intent, 'text': target['id'] if intent == 'forget' else target['id'] + ' ' + text}


def bounded_history(messages):
    kept, used = [], 0
    for message in reversed(messages[-12:]):
        if used + len(message['content']) > 12000:
            break
        kept.append(message)
        used += len(message['content'])
    return list(reversed(kept))


def prepare_message(text, message, provenance, update_id, storage, *, force_answer=False):
    """Plan one authorized turn, using the existing serialized storage boundary."""
    from core.requests import urls, canonical_source
    import uuid
    from core.errors import conversation_event
    correlation_id = uuid.uuid4().hex[:12]
    if not text or len(text) > 4000:
        return {'intent': 'reply', 'reply': 'Please send a text message of up to 4,000 characters.'}
    if quoted(message) and not force_answer:
        return {'intent': 'reply', 'reply': 'Quoted or forwarded text is not personal memory. Ask a question about it or explicitly tell me what you want remembered.'}
    context = storage('chat_context', {**provenance, 'update_id': update_id})
    if context['sequence'] != update_id:
        return {'intent': 'ignore'}
    identity = {**provenance, 'sequence': context['sequence']}
    reply = message.get('reply_to_message') or {}
    association = storage('associate', {**provenance, 'reply_id': reply.get('message_id'),
        'reply_text': reply.get('text') or reply.get('caption') or '', 'reference': text, 'update_id': update_id})
    source_url = association.get('url')
    conversation_event(correlation_id, 'source_resolution', association['status'], source_id=association.get('source_id'))
    if not source_url and len(urls(reply)) == 1:
        try:
            source_url = canonical_source(urls(reply)[0])['url']
        except ValueError:
            pass
    context.update(has_source=bool(source_url), reply_to_source=bool(reply and source_url),
        correlation_id=correlation_id, resolved_source={k: association.get(k) for k in ('source_id', 'url', 'source_status')},
        reply_is_assistant=bool(reply.get('from', {}).get('is_bot') and not source_url))
    decision = None
    source_pending = context.get('pending') or {}
    if source_pending.get('kind') == 'source' and len(text) <= 200 and not re.match(
            r'^(?:I\b|My\b|Remind\b|Remember\b|Tell\b|What\b|How\b|Eu\b)', text, re.I):
        from core.recall import tokens
        options = source_pending['candidates']
        ordinal = {'1': 0, 'first': 0, 'the first one': 0, '2': 1, 'second': 1, 'the second one': 1}.get(text.casefold())
        chosen = [options[ordinal]] if ordinal is not None and ordinal < len(options) else [
            c for c in options if tokens(text) and tokens(text) <= tokens(c['title'])]
        if reply and source_url:
            chosen = [c for c in options if c['url'] == source_url]
        if len(chosen) == 1:
            decision = {**source_pending['action'], 'source_url': chosen[0]['url'], 'pending': source_pending}
    decision = decision or ({'intent': 'answer', 'text': text} if force_answer else route(text, context))
    if not storage('chat_current', identity):
        if decision['intent'] in {'recall', 'reminder', 'remember', 'correct', 'forget', 'revise'}:
            return {'intent': 'reply', 'reply': 'A newer message arrived while I was interpreting this. Please repeat this request so I can apply it safely.'}
        return {'intent': 'ignore'}
    pending = decision.get('pending')
    if pending:
        source_url = source_url if reply and association.get('status') == 'resolved' else pending.get('source_url')
        context['memory_revision'] = pending['memory_revision']
    if decision.get('source_url') and not (reply and association.get('status') == 'resolved'):
        source_url = decision['source_url']
    wording = decision.get('text', text)
    intent = decision['intent']
    conversation_event(correlation_id, 'routing', intent, source_id=association.get('source_id'), action=intent)
    if intent == 'clarify':
        storage('chat_pending', {**identity, 'pending': {'text': wording, 'source_url': source_url,
            'memory_revision': context['memory_revision'], 'provenance': provenance}})
        return {'intent': 'reply', 'reply': decision.get('question') or CLARIFY}
    if context.get('pending'):
        storage('chat_pending', {**identity, 'pending': None})
    from core.personal import needs_source
    requires_source = intent in {'recall', 'revise'} or (intent == 'reminder' and decision.get('command') == 'add' and
        (decision.get('revisit') or needs_source(decision.get('description') or wording)))
    if requires_source and not source_url:
        from core.source_context import question
        storage('chat_pending', {**identity, 'pending': {'kind': 'source', 'text': text,
            'action': decision, 'candidates': association.get('candidates', context.get('sources', [])),
            'memory_revision': context['memory_revision'], 'provenance': provenance}})
        return {'intent': 'reply', 'reply': question(association)}
    origin = pending.get('provenance', provenance) if pending else provenance
    if intent == 'recall':
        return {'intent': 'reply', 'reply': storage('recall', {**identity, **origin, 'sequence': context['sequence'],
            'expected_revision': context['memory_revision'], 'source_url': source_url,
            'topic': decision['topic'], 'source_text': wording, 'update_id': update_id})}
    if intent == 'reminder':
        try:
            response = storage('reminder', {**identity, 'expected_revision': context['memory_revision'],
                'command': decision['command'], 'text': decision.get('text', ''),
                **{k: decision[k] for k in ('target', 'timing', 'description', 'task_kind', 'revisit') if k in decision},
                **{k: origin.get(k) for k in ('date', 'message_id', 'user_id')},
                'reply_id': reply.get('message_id'), 'source_url': source_url, 'source_text': wording,
                'bind_source': bool(decision.get('source_url')),
                'update_id': update_id})
        except ValueError as exc:
            response = str(exc)
        return {'intent': 'reply', 'reply': response, 'task_reply': True}
    if intent in {'remember', 'correct', 'forget', 'memory'}:
        snapshot = storage('memory_snapshot')
        snapshot['revision'] = context['memory_revision']
        payload = memory_request(intent, wording, snapshot, {**(pending.get('provenance', provenance) if pending else provenance),
            'source_text': pending['text'] if pending else text, 'source_url': source_url,
            'explicit_memory': decision.get('explicit_memory', False)})
        if not payload:
            return {'intent': 'reply', 'reply': 'What specifically should I remember or change? Please describe the fact, or reply to the relevant source.'}
        value = interpret(wording)
        return {'intent': 'reply', 'reply': storage('memory', payload),
            'task_reply': bool(value and value['kind'] == 'intention')}
    if intent == 'revise':
        if not source_url:
            return {'intent': 'reply', 'reply': 'Please reply to the intended Instagram link with those instructions so I revise the right note.'}
        return {'intent': 'revise', 'text': wording, 'source_url': source_url, 'sequence': context['sequence']}
    if intent == 'answer':
        from core.understanding import comparison_requested
        comparison = decision.get('comparison', False) or comparison_requested(wording)
        referential = bool(re.search(r'\b(?:this|that|the) (?:post|note|video|link|reel)\b|\b(?:it|that|these|this|isso|isto|estes|estas)\b', wording, re.I))
        explicit_source = bool(re.search(r'\b(?:this|that|the) (?:post|note|video|link|reel)\b', wording, re.I))
        target = source_url if (decision.get('source_url') or context.get('reply_to_source') or explicit_source or (referential and not context.get('reply_is_assistant'))) else None
        if comparison and referential and not target:
            from core.source_context import question
            return {'intent': 'reply', 'reply': question(association)}
        if referential and association['status'] == 'ambiguous' and not pending:
            return {'intent': 'reply', 'reply': 'Which source do you mean? Please reply to its link with your question.'}
        turn = storage('chat_start', {**identity, 'question': wording, 'source_url': target,
            'comparison': comparison, 'correlation_id': correlation_id})
        if turn['status'] == 'deferred':
            from core.voice import pending_answer
            response = pending_answer(comparison) if turn['source_status'] == 'pending' else (
                "I've kept your comparison question. Retry the source from the dashboard; I'll resume once its note is available.")
            return {'intent': 'reply', 'reply': response}
        return {'intent': 'answer', 'text': wording, 'conversation': turn} if turn['status'] == 'started' else {'intent': 'ignore'}
    return decision
