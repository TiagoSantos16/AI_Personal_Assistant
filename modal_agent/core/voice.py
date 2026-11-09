"""Shared conversational defaults; explicit user instructions take precedence."""
from core.config import ASSISTANT_RESPONSE_STYLE as GUIDANCE

SEARCH_ERROR = "I couldn't search your notes right now. Please try again later."


def greeting():
    return 'Hello, sir. How can I help?'


def pending_answer(comparison=False):
    return "I'll compare them once this post finishes processing." if comparison else "I'll answer once this post finishes processing."


def brief_header(morning):
    return 'Good morning. Here are your outstanding tasks:' if morning else 'Here are your outstanding reminders:'


def answer_text(sourced, general):
    # Separate provenance in prose, without routine template headings.
    return '\n\n'.join(p for p in (sourced.strip(),
        ('Beyond the saved evidence, ' + general.strip()) if general.strip() else '') if p)


def reminder_ack(due, brief):
    return ("I'll include this in your morning brief on " if brief else "I'll remind you on ") + due + '.'
