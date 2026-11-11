"""A worker's activity, read from the session files the worker writes for its Ticket.

Read-only. A Pi worker writes `<state_dir>/sessions/<timestamp>_<ticket-id>.jsonl` (one JSON entry per line); a
retried Ticket may have several files. A Copilot worker's session id is in `<state_dir>/copilot-sessions/<ticket-id>`,
and Copilot writes its events to `<state_dir>/copilot-home/session-state/<id>/events.jsonl`. This module only
turns those entries into steps a person can scan.
"""

import glob
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

MASK = '***'


@dataclass
class Step:
    at: str
    kind: str  # request | reasoning | said | tool | result | error
    title: str
    text: str


def copilot_session_file(state_dir, ticket_id):
    return os.path.join(state_dir, 'copilot-sessions', ticket_id)


def copilot_home(state_dir):
    """COPILOT_HOME of Copilot workers: their own, so the user's ~/.copilot setup does not fill their context."""
    return os.path.join(state_dir, 'copilot-home')


def session_files(state_dir, ticket_id):
    pattern = os.path.join(glob.escape(os.path.join(state_dir, 'sessions')), f'*_{glob.escape(ticket_id)}.jsonl')
    found = sorted(glob.glob(pattern))  # the timestamp prefix sorts oldest first
    try:
        with open(copilot_session_file(state_dir, ticket_id), encoding='utf-8') as f:
            sid = f.read().strip()
    except OSError:
        return found
    events = os.path.join(copilot_home(state_dir), 'session-state', sid, 'events.jsonl')
    return [*found, events] if sid and os.path.isfile(events) else found


def _texts(content):
    if isinstance(content, str):
        return content
    return '\n'.join(c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') == 'text')


def _call_summary(name, args):
    if not isinstance(args, dict):
        return str(args)
    for key in ('command', 'path'):
        if isinstance(args.get(key), str):
            return args[key]
    return json.dumps(args)


def _copilot_steps(e):
    d, at = e.get('data'), e.get('timestamp', '')
    if not isinstance(d, dict):
        return
    kind = e.get('type')
    if kind == 'user.message':
        yield Step(at, 'request', 'Assignment', str(d.get('content', '')))
    elif kind == 'assistant.reasoning':
        yield Step(at, 'reasoning', 'Reasoning', str(d.get('content', '')))
    elif kind == 'assistant.message':
        if d.get('content'):
            yield Step(at, 'said', 'Agent', str(d['content']))
        for call in d.get('toolRequests') or []:
            if isinstance(call, dict):
                name = call.get('name', 'tool')
                yield Step(at, 'tool', name, _call_summary(name, call.get('arguments')))
    elif kind == 'tool.execution_complete':
        failed = not d.get('success', True)
        result, error = d.get('result'), d.get('error')
        text = result.get('content', '') if isinstance(result, dict) else ''
        if failed and not text and isinstance(error, dict):
            text = error.get('message', '')
        yield Step(at, 'error' if failed else 'result', 'tool failed' if failed else 'tool result', str(text or ''))


def _entry_steps(e):
    if '.' in str(e.get('type')):  # a Copilot event: user.message, assistant.message, ...
        yield from _copilot_steps(e)
        return
    m = e.get('message')
    if e.get('type') != 'message' or not isinstance(m, dict):
        return
    at, role = e.get('timestamp', ''), m.get('role')
    if role == 'user':
        yield Step(at, 'request', 'Assignment', _texts(m.get('content')))
    elif role == 'toolResult':
        name = m.get('toolName', 'tool')
        failed = m.get('isError')
        yield Step(at, 'error' if failed else 'result', f'{name} failed' if failed else f'{name} result',
                   _texts(m.get('content')))
    elif role == 'assistant':
        for c in m.get('content') or []:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'thinking':
                yield Step(at, 'reasoning', 'Reasoning', c.get('thinking', ''))
            elif c.get('type') == 'text' and c.get('text'):
                yield Step(at, 'said', 'Agent', c['text'])
            elif c.get('type') == 'toolCall':
                name = c.get('name', 'tool')
                yield Step(at, 'tool', name, _call_summary(name, c.get('arguments')))


def _entries(paths):
    """Every JSON entry of the sessions, in order; a half-written last line is skipped."""
    for path in paths:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    yield e


USAGE_KEYS = ('input', 'output', 'cacheRead', 'cacheWrite')


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _usage_of(e):
    """(timestamp, {input, output, cacheRead, cacheWrite, cost}) of a model call's entry, else None. Copilot bills
    premium requests, not dollars: its calls cost 0 here."""
    if e.get('type') == 'session.usage_record':
        u = (e.get('data') or {}).get('usage')
        if isinstance(u, dict):
            return e.get('timestamp'), {'input': u.get('inputTokens'), 'output': u.get('outputTokens'),
                                        'cacheRead': u.get('cacheReadTokens'), 'cacheWrite': u.get('cacheWriteTokens')}
        return None
    m = e.get('message')
    u = m.get('usage') if e.get('type') == 'message' and isinstance(m, dict) else None
    return (e.get('timestamp'), u) if isinstance(u, dict) else None


def daily_tokens(paths):
    """{'YYYY-MM-DD' (UTC): tokens} of the sessions' model calls."""
    days = {}
    for e in _entries(paths):
        found = _usage_of(e)
        if found is None:
            continue
        stamp, u = found
        day = str(stamp or '')[:10]
        if len(day) == 10:
            days[day] = days.get(day, 0) + sum(_number(u.get(k)) for k in USAGE_KEYS)
    return days


def usage(paths, since=None):
    """The tokens and cost (USD) the sessions' model calls used, summed: {input, output, cacheRead, cacheWrite, tokens,
    cost}. The cost is Pi's figure, an estimate from its price table for some providers. `since`: an aware datetime;
    only calls at or after it count."""
    total = dict.fromkeys(USAGE_KEYS, 0) | {'cost': 0.0}
    for e in _entries(paths):
        found = _usage_of(e)
        if found is None:
            continue
        stamp, u = found
        if since is not None:
            try:
                at = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
            except ValueError:
                continue
            if (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) < since:
                continue
        for k in USAGE_KEYS:
            total[k] += _number(u.get(k))
        cost = u.get('cost')
        total['cost'] += _number(cost.get('total')) if isinstance(cost, dict) else 0
    total['tokens'] = sum(total[k] for k in USAGE_KEYS)
    return total


def steps(paths, secrets=()):
    """Every step of the sessions, in order; secrets are masked. A half-written last line is skipped."""
    secrets = [s for s in secrets if s]
    out = []
    for e in _entries(paths):
        out.extend(_entry_steps(e))
    for s in out:
        for secret in secrets:
            s.text = s.text.replace(secret, MASK)
    return out
