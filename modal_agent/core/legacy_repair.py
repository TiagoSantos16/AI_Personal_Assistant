"""Conservative, explicit repair of retained legacy records. Never reads remote data."""
import copy
import re
import uuid

from core.personal import needs_source
from core.requests import canonical_source
from core.understanding import conditional


def recover_source(state, item):
    if item.get('source_url'):
        return canonical_source(item['source_url'])
    if item.get('source_id') in state['sources']:
        candidates = [r for r in state['records'].values() if r['source_id'] == item['source_id']]
        if candidates:
            return canonical_source(candidates[0]['url'])
    entries = state.get('contexts', {}).get(str(item.get('chat_id')) + ':' + str(item.get('thread_id') or 0), [])
    # Only retained original provenance qualifies, never the current focus.
    exact = [e for e in entries if e['message_id'] == item.get('reply_id')]
    if not exact and item.get('message_id') and item.get('date'):
        exact = [e for e in entries if e.get('role', 'user') == 'user' and e['message_id'] < item['message_id'] and
            0 <= item['date'] - (e.get('date') or e.get('received', 0)) <= 600]
    identities = {e['source_id']: e for e in exact}
    return canonical_source(next(iter(identities.values()))['url']) if len(identities) == 1 else None


def repair(original, *, apply=False):
    state = original if apply else copy.deepcopy(original)
    before = copy.deepcopy((state['memory']['items'], state.get('reminders', {}), state.get('recalls', {})))
    findings = []
    for kind, entries in [('memory', state['memory']['items']), ('task', state.get('reminders', {}))]:
        for item in list(entries.values()):
            if not item.get('active', True) or item.get('status') in {'done', 'cancelled'}:
                continue
            text = item['text']
            if kind == 'task':
                from core.reminders import split_work, unfreeze_reference
                restored = unfreeze_reference(text)
                if restored != text:
                    source = recover_source(state, item)
                    if source:
                        item.update(text=restored, source_id=source['source_id'], source_url=source['url'])
                        item.setdefault('repair_history', []).append({'action': 'restore_source_reference', 'previous_text': text})
                        findings.append({'kind': kind, 'id': item['id'], 'action': 'restore_source_reference',
                            'text': text, 'replacement': restored})
                        text = restored
                    else:
                        item['repair_status'] = 'needs_clarification'
                        findings.append({'kind': kind, 'id': item['id'], 'action': 'needs_clarification', 'text': text})
                        continue
                pieces = split_work(text)
                if len(pieces) > 1:
                    children = []
                    for part in pieces:
                        identifier = uuid.uuid5(uuid.NAMESPACE_URL, item['id'] + ':' + part).hex[:10]
                        entries.setdefault(identifier, {**copy.deepcopy(item), 'id': identifier, 'text': part,
                            'group_request': text, 'parent_id': item['id']})
                        children.append(identifier)
                    item.update(status='cancelled', repair_status='split_into_tasks', child_ids=children)
                    findings.append({'kind': kind, 'id': item['id'], 'action': 'split_into_tasks', 'text': text})
                    continue
            request = item.get('source_text') or text
            rule = conditional(request) or conditional('Remember ' + text)
            dangling = needs_source(text) or bool(re.match(r'^(these|those|isto|isso)\b', text, re.I))
            if item.get('source_id') and not rule:
                continue
            if not (rule or dangling) or item.get('repair_status') == 'migrated_recall':
                continue
            source = recover_source(state, item)
            action = 'linked' if source else 'needs_clarification'
            if source:
                item.update(source_id=source['source_id'], source_url=source['url'])
                if rule and kind == 'memory':
                    from core.recall import add
                    saved, _ = add(state, {**item, 'source_text': request, 'topic': rule['topic']})
                    item.update(active=False, repair_status='migrated_recall', recall_id=saved['id'])
                    action = 'migrated_recall'
                else:
                    item.pop('repair_status', None)
            else:
                item['repair_status'] = 'needs_clarification'
            findings.append({'kind': kind, 'id': item['id'], 'action': action, 'text': text})
    # Previously delivered tasks are still open. Never revive explicit completion.
    recurring = sum(i['status'] == 'notified' and i.get('kind', 'task') == 'task'
        for i in state.get('reminders', {}).values())
    changed = before != (state['memory']['items'], state.get('reminders', {}), state.get('recalls', {}))
    if changed and apply:
        state['memory']['revision'] += 1
        state['answer_revision'] += 1
    return {'applied': apply, 'changed': changed, 'findings': findings,
        'delivered_tasks_still_open': recurring,
        'limits': 'Deleted records cannot be recovered. Completed/cancelled records are unchanged.'}
