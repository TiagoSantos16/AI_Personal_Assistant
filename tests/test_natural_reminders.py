"""Task descriptions, conversational references and safe named clarification."""
import pytest

from core import reminders
from test_reminders import s, clock, add, items, natural


def test_codes_never_appear_in_ack_list_brief_or_clarification(s, tmp_path, clock, monkeypatch):
    ack = add(s, 'Call the dentist')
    add(s, 'Book a dentist appointment')
    listing = natural(s, 'Show my reminders')['reply']
    question = natural(s, 'Cancel the dentist reminder', update=2)['reply']
    assert 'Which reminder' in question and 'Call the dentist' in question and 'Book a dentist' in question
    sent = []
    monkeypatch.setattr(reminders, 'deliver', lambda *a: sent.append(a[1]) or 'sent')
    clock[0] = max(i['due'] for i in items(tmp_path).values())
    s('reminder_tick')
    assert sent
    for identifier in items(tmp_path):
        assert all(identifier not in text for text in [ack, listing, question, *sent])
    assert all('Reference:' not in text for text in [ack, listing, question, *sent])


@pytest.mark.parametrize('text', ['I called the dentist', "I've already called the dentist", 'I have just called the dentist'])
def test_plain_completed_action_marks_matching_task_done(s, tmp_path, text):
    add(s, 'Call the dentist')
    add(s, 'Renew my passport')
    reply = natural(s, text)['reply']
    assert 'Marked as done: Call the dentist' == reply
    state = items(tmp_path)
    assert next(i for i in state.values() if 'dentist' in i['text'])['status'] == 'done'
    assert next(i for i in state.values() if 'passport' in i['text'])['status'] == 'pending'


def test_cancel_by_description_and_polite_rescheduling(s, tmp_path):
    add(s, 'Renew my passport')
    add(s, 'Call dentist')
    reply = natural(s, 'Could you move the passport reminder to tomorrow at 6pm please?')['reply']
    assert '18:00' in reply and 'passport' in reply
    assert natural(s, 'Please cancel the dentist reminder', update=2)['reply'] == 'Cancelled: Call dentist'


def test_that_refers_to_last_acknowledged_task_not_an_arbitrary_item(s, tmp_path):
    add(s, 'Renew passport')
    add(s, 'Call dentist')
    reply = natural(s, 'Move that to tomorrow at 6pm')['reply']
    assert 'dentist' in reply and '18:00' in reply
    assert reminders.local_now(next(i for i in items(tmp_path).values() if 'passport' in i['text'])['due']).hour == 9


def test_multiple_matching_tasks_ask_then_accept_named_answer(s, tmp_path):
    add(s, 'Call dentist')
    add(s, 'Book dentist appointment')
    assert 'Which reminder' in natural(s, 'Cancel the dentist reminder')['reply']
    assert all(i['status'] == 'pending' for i in items(tmp_path).values())
    assert natural(s, 'the appointment', update=2)['reply'] == 'Cancelled: Book dentist appointment'
    assert next(i for i in items(tmp_path).values() if i['text'] == 'Call dentist')['status'] == 'pending'


def test_clarification_keeps_requested_time_and_accepts_ordinal(s, tmp_path):
    add(s, 'Call dentist')
    add(s, 'Book dentist appointment')
    assert 'Which reminder' in natural(s, 'Move the dentist reminder to tomorrow at 6pm')['reply']
    assert '18:00' in natural(s, 'the second one', update=2)['reply']
    tasks = list(items(tmp_path).values())
    assert reminders.local_now(tasks[0]['due']).hour == 9
    assert reminders.local_now(tasks[1]['due']).hour == 18


def test_time_clarification_accepts_plain_time(s):
    add(s, 'Call dentist')
    assert 'When would you like' in natural(s, 'Reschedule the dentist reminder')['reply']
    assert '18:00' in natural(s, 'tomorrow at 6pm', update=2)['reply']


def test_done_twice_does_not_complete_a_different_task(s, tmp_path):
    add(s, 'Renew passport')
    add(s, 'Call dentist')
    assert 'Marked as done' in natural(s, 'done')['reply']
    assert 'Which task' in natural(s, 'done', update=2)['reply']
    assert next(i for i in items(tmp_path).values() if 'passport' in i['text'])['status'] == 'pending'


def test_reply_to_older_delivered_reminder_beats_recent_context(s, tmp_path, clock, monkeypatch):
    add(s, 'Call dentist')
    clock[0] = next(iter(items(tmp_path).values()))['due']
    monkeypatch.setattr(reminders, 'deliver', lambda *a: {'status': 'sent', 'message_id': 77})
    s('reminder_tick')
    add(s, 'Renew passport')
    reply = natural(s, 'Move that to tomorrow', reply_to_message={'message_id': 77, 'from': {'is_bot': True}})['reply']
    assert 'dentist' in reply and 'passport' not in reply


def test_expired_focus_asks_when_multiple_tasks_exist(s, tmp_path, clock):
    add(s, 'Call dentist')
    add(s, 'Renew passport')
    clock[0] += 601
    assert 'Which reminder' in natural(s, 'Move that to tomorrow')['reply']
    assert all(reminders.local_now(i['due']).hour == 9 for i in items(tmp_path).values())


def test_expired_clarification_and_changed_tasks_cannot_apply_old_action(s, tmp_path):
    add(s, 'Call dentist')
    add(s, 'Book dentist appointment')
    natural(s, 'Move the dentist reminder to tomorrow at 6pm')
    task = next(iter(items(tmp_path)))
    s('reminder', {'command': 'reschedule', 'text': task + ' tomorrow at 19:00', 'chat_id': 7})
    # The explicit new request replaced the pending choice. A direct delayed resolve cannot mutate.
    assert 'Which reminder' in s('reminder', {'command': 'resolve', 'text': 'first', 'chat_id': 7})
    assert reminders.local_now(items(tmp_path)[task]['due']).hour == 19


def test_quoted_completion_and_negation_do_not_complete_a_task(s, tmp_path):
    add(s, 'Call dentist')
    assert 'Quoted' in natural(s, 'I called the dentist', forward_origin={'type': 'user'})['reply']
    assert reminders.request("I haven't called the dentist") is None
    assert reminders.request('Did I call the dentist?') is None
    assert all(i['status'] == 'pending' for i in items(tmp_path).values())


def test_task_matching_is_thread_scoped(s, tmp_path):
    add(s, 'Call dentist', thread_id=2)
    reply = natural(s, 'I called the dentist')['reply']
    assert 'Which task' in reply
    assert next(iter(items(tmp_path).values()))['status'] == 'pending'


def test_clarification_distinguishes_equal_names_by_date(s, tmp_path):
    add(s, 'Call dentist tomorrow')
    add(s, 'Call dentist in 2 days')
    assert 'Which reminder' in natural(s, 'Cancel the dentist reminder')['reply']
    reply = natural(s, "tomorrow's one", update=2)['reply']
    assert 'Cancelled:' in reply
    assert [i['status'] for i in items(tmp_path).values()] == ['cancelled', 'pending']


def test_changed_candidate_is_not_overwritten_by_delayed_clarification(s, tmp_path):
    first = natural(s, 'I want to try Python')['reply']
    natural(s, 'I want to try Rust', update=2)
    assert 'morning brief' in first
    natural(s, 'Move the try reminder to tomorrow at 6pm', update=3)
    mid = s('memory_snapshot')['items'][0]['id']
    s('memory', {'command': 'forget', 'text': mid})
    reply = natural(s, 'the second one', update=4)['reply']
    assert 'have changed' in reply
    assert all(reminders.local_now(i['due']).hour == 9 for i in items(tmp_path).values())


def test_completed_pronoun_uses_recent_task_and_negative_need_cancels_by_name(s, tmp_path):
    add(s, 'Try Python')
    add(s, 'Try Rust')
    assert natural(s, 'I tried it')['reply'] == 'Marked as done: Try Rust'
    assert 'Cancelled:' in natural(s, "I don't need the Python reminder anymore", update=2)['reply']
    assert all(i['status'] in {'done', 'cancelled'} for i in items(tmp_path).values())


def test_storage_failure_does_not_acknowledge_completion(s, monkeypatch):
    from core import storage
    add(s, 'Call dentist')
    monkeypatch.setattr(storage, 'atomic', lambda *a: (_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):
        s('reminder', {'command': 'done', 'target': 'dentist', 'chat_id': 7})
