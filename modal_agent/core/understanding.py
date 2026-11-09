"""Validated conversational proposals. Storage operations and IDs remain application-owned."""
import re
import os
import math
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    intent: Literal['answer', 'compare', 'revise', 'remember', 'recall', 'task', 'revisit', 'complete', 'cancel', 'snooze', 'clarify']
    comparison: bool = False
    confidence: float = Field(ge=0, le=1)
    source_index: int | None = Field(default=None, ge=0)
    topic: str = Field(default='', max_length=300)
    description: str = Field(default='', max_length=1000)
    timing: str = Field(default='', max_length=150)
    target: str = Field(default='', max_length=300)
    question: str = Field(default='', max_length=300)


def plain(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


def comparison_requested(text):
    text = plain(text)
    return bool(re.search(r'\b(compare|comparison|contrast|versus|vs|better|alternatives?|compara\w*|melhor\w*)\b', text) or
        (re.search(r'\bbest\b', text) and re.search(r'\b(tools|ones|options|choices|repositories|products)\b', text)))


def conditional(text):
    """A whole future-use clause, not everything after the word 'remember'."""
    condition = re.search(r"\b(?:when(?:ever)?|next time|quando|sempre que)\s+(?:i(?:'m| am)? |eu )?(?:ask(?:ing)?|look(?:ing)?|pedir|peco|peço|perguntar|pergunte|procurar|procuro)\s+(?:for |about |por |sobre |de )?(.+)", text, re.I)
    if not condition:
        return None
    if not re.search(r'\b(?:remember|remind|bring|keep|use|useful|consider|include|save|recall|lembra\w*|recorda\w*|guarda\w*|mostra\w*|traz\w*|usa\w*|considera\w*)\b', text, re.I):
        return None
    if re.search(r"\b(don't|do not|never|nao|não)\b", text[:condition.start()], re.I):
        return {'intent': 'reply', 'reply': "I won't add a source recall request."}
    return {'intent': 'recall', 'topic': condition[1].strip().rstrip('.!?'), 'text': text}


def contextual(text):
    recall = conditional(text)
    if recall:
        return recall
    value = plain(text)
    if re.match(r'^(tell me|show me|send me|repeat|revisit|mostra|mostre|diz|diga|envia|envie|volta a mostrar)', value) and re.search(
            r'\b(that|this|these|again|isto|isso|outra vez|novamente)\b', value) and re.search(
            r'\b(tomorrow|today|next|on|at|amanha|hoje|sexta|segunda|terca|quarta|quinta|sabado|domingo)\b', value):
        return {'intent': 'reminder', 'command': 'add', 'text': text,
            'description': 'Revisit this post', 'task_kind': 'notification', 'revisit': True}
    return None


def validated(value, text, context):
    plan = Proposal.model_validate(value)
    threshold = float(os.environ.get('TELEGRAM_ROUTER_CONFIDENCE', '.90'))
    if not math.isfinite(threshold) or not .8 <= threshold <= 1:
        raise ValueError('Invalid interpretation confidence threshold')
    if plan.confidence < threshold:
        return {'intent': 'clarify', 'question': plan.question, 'reason': 'low_confidence'}
    if plan.source_index is not None and plan.source_index >= len(context.get('sources', [])):
        raise ValueError('Source index is outside the supplied conversation context')
    result = {'intent': plan.intent, 'text': text, 'interpreted': True}
    if plan.intent in {'answer', 'compare'}:
        result.update(intent='answer', comparison=plan.comparison or plan.intent == 'compare' or comparison_requested(text))
    if plan.source_index is not None:
        result['source_url'] = context['sources'][plan.source_index]['url']
    if plan.intent == 'recall':
        if not plan.topic or not re.search(r'\b(when|whenever|next time|quando|se eu|sempre que)\b', text, re.I):
            raise ValueError('A recall proposal needs an explicit condition')
        result['topic'] = plan.topic
    if plan.intent in {'task', 'revisit'}:
        if not plan.description.strip():
            raise ValueError('A task proposal needs an actionable description')
        if re.search(r"\b(don't|do not|never|nao|não)\s+(?:remind|remember|schedule|lembres|lembre|agendes)\b", text, re.I):
            raise ValueError('A negated request cannot create a task')
        result.update(intent='reminder', command='add', task_kind='notification' if plan.intent == 'revisit' else 'task',
            description=plan.description, revisit=plan.intent == 'revisit')
    if plan.intent in {'complete', 'cancel', 'snooze'}:
        result.update(intent='reminder', command={'complete': 'done', 'cancel': 'cancel', 'snooze': 'reschedule'}[plan.intent],
            target=plan.target, timing=plan.timing)
    if plan.intent == 'remember':
        # The original whole first-person assertion is the only writable personal fact.
        if not re.match(r"^(I\b|I'm\b|My\b|Eu\b|O meu\b|A minha\b|Já vi\b)", text, re.I) or conditional(text):
            raise ValueError('No direct personal assertion to store')
    if plan.intent == 'clarify':
        result['question'] = plan.question
    # Calendar text must come from the user, never a model-invented deadline.
    if plan.timing and plain(plan.timing) not in plain(text):
        raise ValueError('Proposed time is not present in the user message')
    return result
