"""Bounded source-dependent questions in the existing serialized registry."""
import time
import uuid

from core.errors import conversation_event
from core.requests import canonical_source
from core.source_context import note_resolution

WAIT_SECONDS = 86400
LEASE_SECONDS = 240
RETENTION_SECONDS = 14 * WAIT_SECONDS


def notice_due(notice, now):
    return ((notice.get('status') == 'retry' and notice['attempts'] < 3 and now >= notice['retry_after']) or
        (notice.get('status') == 'sending' and now - notice['claimed'] >= LEASE_SECONDS))


def claim_notice(job, kind, now):
    previous = job.get('notice', {})
    job['notice'] = {'kind': kind, 'status': 'sending', 'attempt': uuid.uuid4().hex,
        'claimed': now, 'attempts': previous.get('attempts', 0) + 1 if previous.get('kind') == kind else 1}
    return {'status': 'notice', 'job': dict(job), 'kind': kind}


def is_open(job, now):
    if job['status'] in {'waiting', 'blocked'}:
        return now - job['created'] < WAIT_SECONDS
    return job['status'] in {'running', 'sending'} and now - job['claimed'] < LEASE_SECONDS


def prune(jobs, now):
    changed = False
    for identifier, job in list(jobs.items()):
        # Results are delivered by the claiming worker, never replayed from this
        # registry. Avoid retaining a redundant copy of private generated content.
        if 'result' in job:
            del job['result']
            changed = True
        if now - job['created'] > RETENTION_SECONDS and job['status'] not in {'running', 'sending'}:
            del jobs[identifier]
            changed = True
    return changed


def has_due_work(state, now):
    """Read-only preflight; actual claims and recovery stay in serialized storage."""
    for job in state.get('deferred_answers', {}).values():
        status, age = job['status'], now - job['created']
        if ('result' in job or job.get('notice_due') or notice_due(job.get('notice', {}), now) or
                (age > RETENTION_SECONDS and status not in {'running', 'sending'})):
            return True
        if status in {'running', 'sending'} and now - job['claimed'] >= LEASE_SECONDS:
            return True
        if status not in {'waiting', 'blocked'}:
            continue
        if age >= WAIT_SECONDS:
            return True
        resolved = note_resolution(state['records'], state['sources'], state['jobs'], job['source_id'])
        if ((resolved['state'] == 'saved' and job['attempts'] == 0) or
                (resolved['state'] in {'failed', 'missing', 'unavailable'} and not job.get('failure_notice'))):
            return True
    return False


def enqueue(state, payload):
    source = canonical_source(payload['source_url'])
    identity = ':'.join(str(payload.get(k) or 0) for k in ('chat_id', 'thread_id', 'message_id'))
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'source-answer:' + identity).hex
    jobs = state.setdefault('deferred_answers', {})
    now = time.time()
    prune(jobs, now)
    if identifier in jobs:
        return jobs[identifier]
    if sum(is_open(j, now) for j in jobs.values()) >= 100:
        raise ValueError('Too many questions are awaiting source evidence. Please try again later.')
    job = {**payload, **source, 'source_url': source['url'], 'id': identifier,
        'created': time.time(), 'status': 'waiting', 'attempts': 0}
    jobs[identifier] = job
    conversation_event(job['correlation_id'], 'evidence_readiness', 'waiting', source_id=source['source_id'], action='compare' if job.get('comparison') else 'answer')
    return job


def operate(state, operation, payload):
    jobs = state.setdefault('deferred_answers', {})
    now = time.time()
    if operation == 'deferred_poll':
        changed = prune(jobs, now)
        for job in jobs.values():
            notice = job.get('notice', {})
            if notice_due(notice, now):
                if notice['status'] == 'sending':
                    notice['status'] = 'uncertain'  # A lost delivery acknowledgement is not safe to retry.
                    changed = True
                elif notice['kind'] == 'source_failed' and note_resolution(
                        state['records'], state['sources'], state['jobs'], job['source_id'])['state'] == 'saved':
                    notice['status'] = 'obsolete'
                    changed = True
                else:
                    return claim_notice(job, notice['kind'], now)
            if job['status'] in {'running', 'sending'} and now - job['claimed'] >= LEASE_SECONDS:
                job['status'] = 'uncertain'  # A crashed charged call/send is not safe to repeat.
                job['notice_due'] = True
            if job.pop('notice_due', False):
                conversation_event(job['correlation_id'], 'evidence_readiness', 'interrupted', source_id=job['source_id'])
                return claim_notice(job, 'interrupted', now)
            if job['status'] not in {'waiting', 'blocked'}:
                continue
            resolved = note_resolution(state['records'], state['sources'], state['jobs'], job['source_id'])
            if now - job['created'] >= WAIT_SECONDS:
                kind = 'expired'
                job['status'] = 'expired'
            elif resolved['state'] == 'saved' and job['attempts'] == 0:
                job.update(status='running', claimed=now, attempt=uuid.uuid4().hex,
                    attempts=1, focal_version=resolved['note']['version_id'], worker_started=False)
                conversation_event(job['correlation_id'], 'evidence_readiness', 'ready', source_id=job['source_id'], action='answer')
                return {'status': 'ready', 'job': dict(job)}
            elif resolved['state'] in {'failed', 'missing', 'unavailable'} and not job.get('failure_notice'):
                kind = 'source_failed'
                job.update(failure_notice=True, status='blocked')
            else:
                continue
            conversation_event(job['correlation_id'], 'evidence_readiness', kind, source_id=job['source_id'])
            return claim_notice(job, kind, now)
        return {'status': 'idle', 'changed': True} if changed else {'status': 'idle'}
    job = jobs.get(payload.get('id'))
    if operation == 'deferred_notice_done':
        notice = job.get('notice', {}) if job else {}
        if notice.get('status') != 'sending' or notice.get('attempt') != payload.get('notice_attempt'):
            return {'status': 'stale'}
        outcome = payload.get('outcome')
        if outcome not in {'sent', 'retry', 'failed', 'uncertain'}:
            raise ValueError('Invalid notice delivery outcome')
        notice.update(status='failed' if outcome == 'retry' and notice['attempts'] >= 3 else outcome,
            finished=now, retry_after=now + 300)
        conversation_event(job['correlation_id'], 'deferred_notice', notice['status'], source_id=job['source_id'])
        return {'status': notice['status']}
    if not job or job.get('attempt') != payload.get('attempt'):
        return {'status': 'stale'}
    if operation == 'deferred_start':
        if job['status'] != 'running' or job.get('worker_started'):
            return {'status': 'stale'}
        if now - job['claimed'] >= LEASE_SECONDS:
            job.update(status='uncertain', notice_due=True)
            return {'status': 'stale'}
        job['worker_started'] = True
        return {'status': 'started'}
    if operation == 'deferred_result':
        if job['status'] != 'running':
            return {'status': 'stale'}
        result = payload['result']
        resolved = note_resolution(state['records'], state['sources'], state['jobs'], job['source_id'])
        active = {r['name'] for r in state['records'].values() if r['section'] == 'notes'}
        if (resolved.get('note', {}).get('version_id') != job['focal_version'] or
                result['memory_revision'] != state['answer_revision'] or
                any(s not in active and not s.startswith('memory:') for s in result.get('sources', []))):
            job['status'] = 'changed'
            return {'status': 'changed'}
        job['status'] = 'sending'
        return {'status': 'deliverable'}
    if operation == 'deferred_done':
        if job['status'] not in {'running', 'sending'}:
            return {'status': 'stale'}
        if payload.get('success') is True and job['status'] != 'sending':
            return {'status': 'stale'}
        job['status'] = 'answered' if payload.get('success') is True else 'failed' if job['status'] == 'running' else 'uncertain'
        conversation_event(job['correlation_id'], 'deferred_delivery', job['status'], source_id=job['source_id'])
        return {'status': job['status']}
    raise ValueError('Unknown deferred-answer operation')
