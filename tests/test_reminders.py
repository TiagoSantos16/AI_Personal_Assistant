"""Offline calendar, durable reminder, delivery and conversation regression tests."""
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core import conversation, decisions, reminders, storage


ZONE = ZoneInfo('Europe/Lisbon')


@pytest.fixture
def clock(monkeypatch):
    clock = [datetime(2026, 10, 24, 18, tzinfo=ZONE).timestamp()]
    monkeypatch.setattr(reminders.time, 'time', lambda: clock[0])
    monkeypatch.setattr(reminders, 'deliver', lambda *a: pytest.fail('unexpected Telegram call'))
    monkeypatch.setattr(decisions, 'message_intent', lambda *a: pytest.fail('unnecessary AI routing'))
    return clock


@pytest.fixture
def s(tmp_path, clock):
    return lambda op, p=None: storage.dispatch(op, p, root=tmp_path)


def add(s, text, chat=7, **extra):
    return s('reminder', {'command': 'add', 'text': text, 'chat_id': chat, 'message_id': 1, **extra})


def items(tmp_path):
    return json.loads((tmp_path / 'private/registry.json').read_text(encoding='utf-8')).get('reminders', {})


def natural(s, text, update=1, **message):
    return conversation.prepare_message(text, {'text': text, 'message_id': update, **message},
        {'chat_id': 7, 'message_id': 1000 + update}, update, s)


@pytest.mark.parametrize('day,expected_hours', [(datetime(2026, 10, 24, 18, tzinfo=ZONE), 16),
    (datetime(2026, 3, 28, 18, tzinfo=ZONE), 14), (datetime(2026, 10, 20, 7, tzinfo=ZONE), 26)])
def test_default_is_next_calendar_day_at_nine_even_before_nine(day, expected_hours):
    body, due, brief = reminders.schedule('Call the dentist', day)
    local = reminders.local_now(due)
    assert local.hour == 9 and local.minute == 0 and local.day == day.day + 1
    assert (due - day.timestamp()) / 3600 == expected_hours and brief
    assert body == 'Call the dentist'


@pytest.mark.parametrize('text,day,hour,minute,brief', [
    ('Call the dentist tomorrow at 18:30', 25, 18, 30, False),
    ('Call the dentist at 9am', 25, 9, 0, False),
    ('Call the dentist on 2026-11-03 at 6pm', 3, 18, 0, False),
    ('Call the dentist next monday', 26, 9, 0, True),
    ('Call the dentist tomorrow', 25, 9, 0, True)])
def test_explicit_calendar_times(clock, text, day, hour, minute, brief):
    body, due, grouped = reminders.schedule(text)
    local = reminders.local_now(due)
    assert (local.day, local.hour, local.minute, grouped) == (day, hour, minute, brief)
    assert body == 'Call the dentist'


@pytest.mark.parametrize('text', ['Call at 3', 'Call tonight', 'Call next month', 'Call every day',
    'Call tomorrow at 25:00', 'Call tomorrow at 09:75', 'Call on 2026-02-30',
    'Call today at 09:00', 'Call on 2026-10-25 at 01:30', 'Call on 2027-03-28 at 01:30',
    'Call on 03/11', 'Call Friday', 'Call at noon', 'Call at 18:00 UTC'])
def test_ambiguous_invalid_past_and_dst_times_are_not_silently_defaulted(clock, text):
    with pytest.raises(ValueError):
        reminders.schedule(text)


def test_relative_timing_uses_elapsed_minutes(clock):
    body, due, brief = reminders.schedule('Stretch in 10 minutes')
    assert due == clock[0] + 600 and not brief and body == 'Stretch'


def test_relative_days_without_clock_time_use_local_morning_brief(clock):
    _, due, brief = reminders.schedule('Call dentist in 2 days')
    local = reminders.local_now(due)
    assert (local.day, local.hour, local.minute) == (26, 9, 0) and brief


def test_grouped_brief_only_when_due_then_silent_and_persistent(s, clock, tmp_path, monkeypatch):
    add(s, 'Call the dentist')
    add(s, 'Pay the electricity bill')
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a) or 'sent')
    assert s('reminder_tick')['status'] == 'idle'
    due = next(iter(items(tmp_path).values()))['due']
    clock[0] = due - 1
    assert s('reminder_tick')['status'] == 'idle'
    clock[0] = due
    assert s('reminder_tick') == {'status': 'sent', 'count': 2}
    assert len(sent) == 1 and 'Good morning' in sent[0][1]
    assert 'dentist' in sent[0][1] and 'electricity' in sent[0][1]
    assert all(i['status'] == 'notified' for i in items(tmp_path).values())
    # A fresh dispatch keeps unfinished work in the following day's brief.
    clock[0] += 86400
    assert storage.dispatch('reminder_tick', root=tmp_path)['status'] == 'sent'
    assert len(sent) == 2


def test_explicit_times_are_separate_from_the_brief_and_chat_scoped(s, clock, tmp_path, monkeypatch):
    add(s, 'Call tomorrow at 09:00')
    add(s, 'Read a book')
    add(s, 'Read another book', chat=8)
    clock[0] = max(i['due'] for i in items(tmp_path).values())
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a) or 'sent')
    for _ in range(4):
        s('reminder_tick')
    assert len(sent) == 3
    assert sum('Good morning' in x[1] for x in sent) == 2
    assert {x[0] for x in sent} == {7, 8}


def test_equal_tasks_in_different_topics_are_not_deduplicated(s, tmp_path):
    add(s, 'Call dentist', thread_id=10)
    add(s, 'Call dentist', thread_id=11)
    assert len(items(tmp_path)) == 2


def test_large_brief_is_split_without_losing_items(s, clock, tmp_path, monkeypatch):
    for n in range(5):
        add(s, f'Read section {n}: ' + 'x' * 600)
    clock[0] = next(iter(items(tmp_path).values()))['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a) or 'sent')
    for _ in range(6):
        s('reminder_tick')
    combined = ''.join(x[1] for x in sent)
    assert all('Read section ' + str(n) in combined for n in range(5))
    assert all(len(x[1]) <= 3500 for x in sent)


def test_cancel_complete_and_reschedule_survive_dispatches(s, clock, tmp_path):
    add(s, 'Call the dentist')
    identifier = next(iter(items(tmp_path)))
    reply = s('reminder', {'command': 'reschedule', 'text': identifier + ' tomorrow at 18:00', 'chat_id': 7})
    assert '18:00' in reply and not items(tmp_path)[identifier]['brief']
    with pytest.raises(ValueError):
        s('reminder', {'command': 'reschedule', 'text': identifier + ' tonight', 'chat_id': 7})
    assert reminders.local_now(items(tmp_path)[identifier]['due']).hour == 18
    assert 'Which task' in s('reminder', {'command': 'cancel', 'text': identifier, 'chat_id': 8})
    assert 'done' in s('reminder', {'command': 'done', 'text': '', 'chat_id': 7})
    assert s('reminder', {'command': 'list', 'chat_id': 7}) == 'You have no open reminders.'
    assert s('reminder_tick')['status'] == 'idle'


def test_linked_intention_is_atomic_with_reminder_and_forget_cancels(s, clock, tmp_path):
    s('accept', {'url': 'https://instagram.com/reel/One/', 'chat_id': 7, 'message_id': 44})
    reply = natural(s, 'I need to try this')['reply']
    assert 'morning brief' in reply
    entry = s('memory_snapshot')['items'][0]
    reminder = next(iter(items(tmp_path).values()))
    assert reminder['source_id'] == 'One' and reminder['memory_id'] == entry['id']
    assert entry['text'] == 'I need to try this'
    natural(s, 'I need to try this', update=2)
    assert len(items(tmp_path)) == 1  # no duplicate schedule
    s('memory', {'command': 'forget', 'text': entry['id']})
    assert items(tmp_path)[reminder['id']]['status'] == 'cancelled'
    clock[0] = reminder['due']
    assert s('reminder_tick')['status'] == 'idle'


def test_negative_uncertain_and_preferences_are_not_automatic_tasks(s, tmp_path):
    for text in ['I prefer concise notes.', 'I might try Python today', "I don't want to try Python"]:
        natural(s, text, update=len(s('memory_snapshot')['items']) + 1)
    assert not items(tmp_path)


def test_correcting_intention_cancels_previous_task(s, tmp_path):
    s('accept', {'url': 'https://instagram.com/reel/One/', 'chat_id': 7, 'message_id': 44})
    natural(s, 'I need to try this')
    natural(s, "Actually I don't want to try this", update=2)
    assert all(i['status'] == 'cancelled' for i in items(tmp_path).values())


def test_plain_reminders_and_changes_need_no_model(s, tmp_path):
    reply = natural(s, 'Can you remind me tomorrow at 6pm to call the dentist?')['reply']
    assert '18:00' in reply and 'Call the dentist' in reply
    identifier = next(iter(items(tmp_path)))
    assert 'dentist' in natural(s, 'What do I need to do?', update=2)['reply']
    assert 'Cancelled:' in natural(s, 'cancel reminder ' + identifier, update=3)['reply']
    assert 'morning brief' in natural(s, 'I need to pay rent', update=4)['reply']
    assert 'morning brief' in natural(s, 'Remember to renew my passport', update=5)['reply']
    assert 'passport' in natural(s, 'done', update=6)['reply']  # the immediately preceding acknowledgement


def test_expired_and_ambiguous_references_do_not_schedule(s, tmp_path):
    result = natural(s, 'Remind me to try this')['reply']
    assert 'reply to its link' in result and not items(tmp_path)
    for n in range(2):
        s('accept', {'url': f'https://instagram.com/reel/One{n}/', 'chat_id': 7, 'message_id': 40 + n})
    assert 'morning brief' in natural(s, 'Remind me to try this', update=2)['reply']
    assert next(iter(items(tmp_path).values()))['source_id'] == 'One1'


def test_failed_storage_does_not_acknowledge_schedule(s, monkeypatch):
    monkeypatch.setattr(storage, 'atomic', lambda *a: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):
        add(s, 'Call dentist')


def test_stale_interpretation_cannot_create_a_reminder(s):
    context = s('chat_context', {'chat_id': 7, 'update_id': 1})
    s('chat_context', {'chat_id': 7, 'update_id': 2})
    reply = s('reminder', {'command': 'add', 'text': 'Call dentist', 'chat_id': 7, 'sequence': context['sequence']})
    assert 'newer message' in reply
    assert s('reminder', {'command': 'list', 'chat_id': 7}) == 'You have no open reminders.'


@pytest.mark.parametrize('outcome,status', [('uncertain', 'uncertain'), ('failed', 'failed'), ('retry', 'pending')])
def test_delivery_failures_do_not_claim_delivery(s, clock, tmp_path, monkeypatch, outcome, status):
    add(s, 'Call dentist')
    clock[0] = next(iter(items(tmp_path).values()))['due']
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a) or outcome)
    assert s('reminder_tick')['status'] == outcome
    assert next(iter(items(tmp_path).values()))['status'] == status
    assert s('reminder_tick')['status'] == 'idle' and len(sent) == 1
    clock[0] += 301
    if outcome != 'retry':
        assert s('reminder_tick')['status'] == 'idle'


def test_interrupted_send_is_not_automatically_resent(s, clock, tmp_path, monkeypatch):
    add(s, 'Call dentist')
    clock[0] = next(iter(items(tmp_path).values()))['due']
    monkeypatch.setattr(reminders, 'deliver', lambda *a: (_ for _ in ()).throw(RuntimeError('container stopped')))
    with pytest.raises(RuntimeError):
        s('reminder_tick')
    assert next(iter(items(tmp_path).values()))['status'] == 'sending'
    monkeypatch.setattr(reminders, 'deliver', lambda *a: pytest.fail('must not resend'))
    assert s('reminder_tick')['status'] == 'idle'
    assert next(iter(items(tmp_path).values()))['status'] == 'uncertain'


def test_task_grounding_is_current_and_cancelled_tasks_are_removed(s):
    add(s, 'Call dentist')
    current = s('memory_get', {'question': 'What are my reminders?'})
    assert current['items'][0]['kind'] == 'task' and 'dentist' in current['items'][0]['text']
    s('reminder', {'command': 'cancel', 'text': '', 'chat_id': 7})
    assert s('memory_revision') > current['revision']
    assert not s('memory_get', {'question': 'What are my reminders?'})['items']


@pytest.mark.parametrize('general', [False, True])
def test_tasks_ground_dashboard_and_telegram_without_saved_notes(s, monkeypatch, general):
    from core import rag
    add(s, 'Call dentist')
    monkeypatch.setattr(rag, 'get_llm', lambda *a: [])
    def model(chain, messages, **kwargs):
        notes = json.loads(messages[1]['content'])['untrusted_notes']
        identifier = next(iter(notes))
        assert 'Call dentist' in str(notes) and '09:00' in str(notes)
        return SimpleNamespace(response_metadata={}, content=json.dumps({'answer': f'Call the dentist [{identifier}].',
            'used_sources': [identifier], **({'general_knowledge': ''} if general else {})}))
    monkeypatch.setattr(rag, 'invoke_with_retry', model)
    def store(op, p=None):
        return [] if op == 'retrieve' else s(op, p)
    result = rag.grounded_answer('What do I need to do?', store, allow_general=general)
    assert 'dentist' in result['answer'] and result['sources'][0].startswith('memory:reminder:')


def test_reference_resolution_handles_other_verbs_without_replacing_relative_clauses(s):
    from core import personal
    add(s, 'Remind me to investigate this', source_url='https://instagram.com/reel/One/')
    item = {'text': 'I need to build a tool that uses this', 'source_id': 'One', 'source_url': 'https://instagram.com/reel/One/'}
    resolved = personal.contextualize(item, {'notes/one': {'title': 'API Anything'}}, {'One': {'active': 'notes/one'}})
    assert resolved['text'].startswith('I need to build a tool that uses API Anything')


def test_human_acknowledgements_keep_provenance_in_inspection():
    assert reminders.human_reply('Remembered: abc123 — I live in Lisbon.') == 'Noted: you live in Lisbon.'
    assert reminders.human_reply('Already remembered: abc123') == 'I already have that noted.'


@pytest.mark.parametrize('kind,expected', [('sent', 'sent'), ('rate_limit', 'retry'), ('forbidden', 'failed'),
    ('timeout', 'uncertain'), ('malformed', 'uncertain')])
def test_telegram_transport_is_one_attempt_and_distinguishes_unknown(monkeypatch, kind, expected):
    import requests
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake')
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        if kind == 'timeout':
            raise requests.Timeout()
        status = {'sent': 200, 'rate_limit': 429, 'forbidden': 403, 'malformed': 502}[kind]
        return SimpleNamespace(ok=status == 200, status_code=status,
            json=lambda: [] if kind == 'malformed' else {'ok': kind == 'sent'})
    monkeypatch.setattr(requests, 'post', post)
    assert reminders.deliver(7, 'A reminder for you', 12) == expected
    assert len(calls) == 1 and calls[0]['json']['message_thread_id'] == 12
    assert calls[0]['timeout'] == 10
