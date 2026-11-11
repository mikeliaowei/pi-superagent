"""A Ticket's worker log for the panel: what its worker did, read from the session files the worker writes, its usage,
and the service log's lines about it.

Read-only, pure functions over files, no Streamlit; nothing here raises on a missing, malformed or half-written file.
A Pi worker writes `<state_dir>/sessions/<timestamp>_<ticket-id>.jsonl` (one JSON entry per line); a retried Ticket
may have several files. A Copilot worker's session id is in `<state_dir>/copilot-sessions/<ticket-id>`, and Copilot
writes its events to `<state_dir>/copilot-home/session-state/<id>/events.jsonl`. `superagent run` writes the service
log to `Config.log_path` (rotated: .1, .2, .3).

Steps are read from the end of each session file (TAIL_BYTES) and the service log from the end of the log file, so a
long session or log stays fast. Every step's title and text, and every service log line, is masked: the exact secrets
the caller passes, and strings that look like tokens or private keys (mask_secrets), before anything is cut short.
"""

import glob
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

MASK = '***'
TAIL_BYTES = 5 * 1024 * 1024  # of a session file, for its steps
LOG_TAIL_BYTES = 1024 * 1024  # of the service log
RESULT_CHARS = 2000  # a tool result keeps its head and tail within this
TEXT_CHARS = 20000  # the assignment, agent text, reasoning and tool arguments
TITLE_CHARS = 200

_SECRETS = [
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S),
     '[masked private key]'),
    (re.compile(r'github_pat_[A-Za-z0-9_]{20,}'), 'github_pat_***'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'), 'gh*_***'),
    (re.compile(r'\bsk-ant-[A-Za-z0-9-]{10,}'), 'sk-ant-***'),
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}'), 'sk-***'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'AKIA***'),
    (re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]{20,}'), 'Bearer ***'),
    (re.compile(r"x-access-token:[^@\s\"']+"), 'x-access-token:***'),
]


def mask_secrets(text):
    """`text` with strings that look like tokens or private keys replaced by a mask."""
    for pattern, mask in _SECRETS:
        text = pattern.sub(mask, text)
    return text


@dataclass
class Step:
    at: str  # the entry's timestamp as the worker wrote it
    kind: str  # request | reasoning | said | tool | result | error | stopped
    title: str  # one line
    text: str


# Session files

def copilot_session_file(state_dir, ticket_id):
    return os.path.join(state_dir, 'copilot-sessions', ticket_id)


def copilot_home(state_dir):
    """COPILOT_HOME of Copilot workers: their own, so the user's ~/.copilot setup does not fill their context."""
    return os.path.join(state_dir, 'copilot-home')


def session_files(state_dir, ticket_id):
    """The Ticket's session files, oldest first: its Pi sessions, then its Copilot session's events, if any."""
    pattern = os.path.join(glob.escape(os.path.join(state_dir, 'sessions')), f'*_{glob.escape(ticket_id)}.jsonl')
    found = sorted(glob.glob(pattern), key=os.path.basename)  # the timestamp prefix sorts oldest first
    try:
        with open(copilot_session_file(state_dir, ticket_id), encoding='utf-8') as f:
            sid = f.read().strip()
    except OSError:
        return found
    events = os.path.join(copilot_home(state_dir), 'session-state', sid, 'events.jsonl')
    return [*found, events] if sid and os.path.isfile(events) else found


def _tail_lines(path, max_bytes):
    """The complete lines in the last `max_bytes` of the file (all of it: None); [] when it cannot be read."""
    try:
        with open(path, 'rb') as f:
            start = 0
            if max_bytes is not None:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - max_bytes)
                f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', errors='replace').splitlines()
    return lines[1:] if start and lines else lines  # the first line of a window starts mid-line


def _entries(paths, max_bytes=None):
    """Every JSON entry of the sessions, in order; a malformed or half-written line is skipped."""
    for path in paths:
        for line in _tail_lines(path, max_bytes):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e


# Steps

def _texts(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(str(c['text']) for c in content
                     if isinstance(c, dict) and c.get('type') == 'text' and c.get('text') is not None)


def _call_summary(name, args):
    """The command or path a tool call is about, then any other arguments as JSON."""
    if not isinstance(args, dict):
        return str(args)
    for key in ('command', 'path'):
        if isinstance(args.get(key), str):
            rest = {k: v for k, v in args.items() if k != key}
            return args[key] + (f'\n{json.dumps(rest, indent=2, ensure_ascii=False)}' if rest else '')
    return json.dumps(args, ensure_ascii=False)


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
        calls = d.get('toolRequests')
        for call in calls if isinstance(calls, list) else []:
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
        for c in m.get('content') if isinstance(m.get('content'), list) else []:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'thinking' and str(c.get('thinking', '')).strip():
                yield Step(at, 'reasoning', 'Reasoning', str(c['thinking']))
            elif c.get('type') == 'text' and c.get('text'):
                yield Step(at, 'said', 'Agent', c['text'])
            elif c.get('type') == 'toolCall':
                name = c.get('name', 'tool')
                yield Step(at, 'tool', name, _call_summary(name, c.get('arguments')))
        stop = m.get('stopReason')
        if stop in ('error', 'aborted'):  # a failed or aborted model call ends the run
            yield Step(at, 'stopped', f'stopped: {stop}', str(m.get('errorMessage') or stop))


def _clip(text, limit=TEXT_CHARS):
    return text if len(text) <= limit else text[:limit] + f'\n... ({len(text) - limit} more characters)'


def _trim(text, limit=RESULT_CHARS):
    """Head and tail of a long output, where the command and the verdict usually are."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f'{text[:half]}\n... ({len(text) - 2 * half} characters trimmed) ...\n{text[-half:]}'


def _one_line(text):
    line = ' '.join(text.split())
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 1] + '…'


def steps(paths, secrets=(), max_bytes=TAIL_BYTES):
    """Every step of the sessions (the last `max_bytes` of each file), in order. The exact `secrets`, then strings that
    look like tokens, are masked before a long text is cut: tool output keeps its head and tail, other text its head."""
    secrets = [s for s in secrets if s]

    def mask(text):
        for secret in secrets:
            text = text.replace(secret, MASK)
        return mask_secrets(text)

    out = []
    for e in _entries(paths, max_bytes):
        for s in _entry_steps(e):
            s.at, s.title, text = str(s.at or ''), _one_line(mask(str(s.title))), mask(str(s.text))
            s.text = _trim(text) if s.kind in ('result', 'error') else _clip(text)
            out.append(s)
    return out


# Usage

USAGE_KEYS = ('input', 'output', 'cacheRead', 'cacheWrite')


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _usage_of(e):
    """(timestamp, {input, output, cacheRead, cacheWrite, cost}) of a model call's entry, else None. Copilot bills
    premium requests, not dollars: its calls cost 0 here."""
    if e.get('type') == 'session.usage_record':
        d = e.get('data')
        u = d.get('usage') if isinstance(d, dict) else None
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


# The service log

def service_lines(log_path, ticket, limit=200, secrets=()):
    """The service log's lines about `ticket` (its `[name]` or its id), newest last, masked; reads only the file's
    tail, and the rotated `.1` file when the current one has fewer than `limit` matches."""
    marks = (f'[{ticket.name}]', ticket.id)
    secrets = [s for s in secrets if s]
    found = []
    for path in (log_path, log_path + '.1'):
        found = [ln for ln in _tail_lines(path, LOG_TAIL_BYTES) if any(m in ln for m in marks)] + found
        if len(found) >= limit:
            break
    out = []
    for ln in found[-limit:]:
        for secret in secrets:
            ln = ln.replace(secret, MASK)
        out.append(mask_secrets(ln))
    return out
