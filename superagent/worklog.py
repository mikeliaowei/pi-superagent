"""A Ticket's worker log for the panel: its Pi session as a timeline, and its lines of the service log.

Pure functions over files, no Streamlit. Everything is read from the end of the file, so a long session or log stays
fast, and nothing here raises on a missing, malformed or half-written file. Every title and text is passed through
mask_secrets before it is returned.
"""

import glob
import json
import os
import re
from dataclasses import dataclass

TAIL_BYTES = 5 * 1024 * 1024  # of a session file
LOG_TAIL_BYTES = 1024 * 1024  # of the service log
RESULT_CHARS = 2000  # a tool result keeps its head and tail within this
TEXT_CHARS = 20000  # assistant text, prompts and tool arguments
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
class Event:
    at: str  # the entry's timestamp as Pi wrote it
    kind: str  # prompt | assistant | thinking | tool_call | tool_result | error | info
    title: str  # one line
    text: str = ''
    id: str = ''


def session_files(state_dir, ticket_id):
    """The Ticket's Pi session files (`<state_dir>/sessions/<timestamp>_<ticket_id>.jsonl`), oldest first."""
    pattern = os.path.join(glob.escape(os.path.join(state_dir, 'sessions')), f'*_{glob.escape(ticket_id)}.jsonl')
    return sorted(glob.glob(pattern), key=os.path.basename)


def _tail_lines(path, max_bytes):
    """The complete lines in the last `max_bytes` of the file; [] when it cannot be read."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', errors='replace').splitlines()
    return lines[1:] if start and lines else lines  # the first line of a window starts mid-line


def _clip(text, limit):
    text = mask_secrets(text)  # before cutting, so a token is never cut in half and missed
    return text if len(text) <= limit else text[:limit] + f'\n... ({len(text) - limit} more characters)'


def _trim(text, limit=RESULT_CHARS):
    """Head and tail of a long output, where the command and the verdict usually are."""
    text = mask_secrets(text)
    if len(text) <= limit:
        return text
    half = limit // 2
    return f'{text[:half]}\n... ({len(text) - 2 * half} characters trimmed) ...\n{text[-half:]}'


def _one_line(text):
    line = ' '.join(mask_secrets(str(text)).split())
    return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS - 1] + '…'


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def _call_title(name, args):
    key = {'bash': 'command', 'read': 'path', 'edit': 'path', 'write': 'path', 'ls': 'path', 'grep': 'pattern',
           'find': 'pattern'}.get(name)
    if key and isinstance(args.get(key), str):
        return f'{name}: {_one_line(args[key])}'
    return f'{name}: {_one_line(json.dumps(args, ensure_ascii=False))}' if args else name


def _events(entry):
    at, eid, typ = str(entry.get('timestamp', '')), str(entry.get('id', '')), entry.get('type')

    def ev(kind, title, text=''):
        return Event(at, kind, title, text, eid)

    if typ == 'session':
        return [ev('info', 'session started', _clip(f"cwd {entry.get('cwd', '')}", TEXT_CHARS))]
    if typ == 'model_change':
        return [ev('info', f"model {entry.get('provider', '')}/{entry.get('modelId', '')}")]
    if typ == 'compaction':
        return [ev('info', 'context compacted', _clip(str(entry.get('summary', '')), TEXT_CHARS))]
    if typ == 'branch_summary':
        return [ev('info', 'branch summary', _clip(str(entry.get('summary', '')), TEXT_CHARS))]
    if typ != 'message' or not isinstance(entry.get('message'), dict):
        return []
    m = entry['message']
    role = m.get('role')
    if role == 'system':
        return [ev('info', 'system prompt', _clip(_text(m.get('content')), TEXT_CHARS))]
    if role == 'user':
        return [ev('prompt', 'prompt', _clip(_text(m.get('content')), TEXT_CHARS))]
    if role == 'toolResult':
        name = m.get('toolName') or 'tool'
        return [ev('tool_result', f"{name} {'error' if m.get('isError') else 'result'}", _trim(_text(m.get('content'))))]
    if role != 'assistant':
        return []
    out = []
    for p in m.get('content') if isinstance(m.get('content'), list) else []:
        if not isinstance(p, dict):
            continue
        if p.get('type') == 'text' and p.get('text', '').strip():
            out.append(ev('assistant', 'assistant', _clip(p['text'], TEXT_CHARS)))
        elif p.get('type') == 'thinking' and str(p.get('thinking', '')).strip():
            out.append(ev('thinking', 'thinking', _clip(str(p['thinking']), TEXT_CHARS)))
        elif p.get('type') == 'toolCall':
            name, args = str(p.get('name', 'tool')), p.get('arguments') if isinstance(p.get('arguments'), dict) else {}
            body = args['command'] if name == 'bash' and isinstance(args.get('command'), str) \
                else json.dumps(args, indent=2, ensure_ascii=False)
            out.append(ev('tool_call', _call_title(name, args), _clip(body, TEXT_CHARS)))
    stop = m.get('stopReason')
    if stop in ('error', 'aborted'):
        out.append(ev('error', f'stopped: {stop}', _clip(str(m.get('errorMessage') or stop), TEXT_CHARS)))
    return out


def read_session(path, max_entries=500, max_bytes=TAIL_BYTES):
    """The last `max_entries` timeline events of a Pi session file, oldest first."""
    events = []
    for line in _tail_lines(path, max_bytes):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # malformed, or the half-written last line
        if isinstance(entry, dict):
            events.extend(_events(entry))
    return events[-max_entries:] if max_entries else events


def service_lines(log_path, ticket, limit=200):
    """The service log's lines about `ticket` (its `[name]` or its id), newest last; reads only the file's tail, and
    the rotated `.1` file when the current one has fewer than `limit` matches."""
    marks = (f'[{ticket.name}]', ticket.id)
    found = []
    for path in (log_path, log_path + '.1'):
        found = [ln for ln in _tail_lines(path, LOG_TAIL_BYTES) if any(m in ln for m in marks)] + found
        if len(found) >= limit:
            break
    return [mask_secrets(ln) for ln in found[-limit:]]
