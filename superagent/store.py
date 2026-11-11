"""SQLite store: rows are JSON, side effects are written before they run (the effect queue).

One connection is shared by the loop thread, the report server's threads and polling threads, so every method
holds one lock for its whole body and returns plain values, never cursors.
"""

import functools
import hmac
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .model import Ticket, Trigger, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS trigger_reasons (id TEXT PRIMARY KEY, reason TEXT);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, thread_key TEXT, state TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS inputs (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, kind TEXT, text TEXT, trigger_id TEXT,
  delivered INTEGER DEFAULT 0, at TEXT
);
CREATE TABLE IF NOT EXISTS effects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, data TEXT, attempts INTEGER DEFAULT 0,
  next_at TEXT, done INTEGER DEFAULT 0, last_error TEXT DEFAULT '', UNIQUE (kind, key)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS local_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ticket_id TEXT, repos TEXT, text TEXT, actor TEXT, at TEXT,
  done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS local_messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, at TEXT, kind TEXT, body TEXT, key TEXT UNIQUE, actor TEXT
);
CREATE INDEX IF NOT EXISTS tickets_thread ON tickets (thread_key, state);
CREATE INDEX IF NOT EXISTS effects_due ON effects (done, next_at);
"""

BACKOFF_START, BACKOFF_MAX = 5, 300


def _ts(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


class Store:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        with self.lock:
            # Concurrent first opens (service plus `status` or the panel) wait for each other instead of failing.
            self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript(SCHEMA)
            self._keep_column_reasons()

    def _keep_column_reasons(self):
        """Tables are never altered, so older versions keep working on this database.

        One build (48f2ba4) stored reasons in a fifth `triggers` column. Such a database keeps that column; its
        reasons are copied into `trigger_reasons` (idempotent, so concurrent opens are safe).
        """
        if 'reason' in {r['name'] for r in self.db.execute('PRAGMA table_info(triggers)')}:
            self.db.execute("INSERT OR IGNORE INTO trigger_reasons SELECT id, reason FROM triggers "
                            "WHERE reason IS NOT NULL AND reason != ''")
            self.db.commit()

    # Triggers: the same id twice is a no-op.
    @_locked
    def add_trigger(self, t: Trigger, decision, reason=''):
        # Named columns: a database upgraded by 48f2ba4 has an extra `reason` column, which keeps its default.
        cur = self.db.execute('INSERT OR IGNORE INTO triggers (id, thread_key, decision, data) VALUES (?, ?, ?, ?)',
                              (t.id, t.thread_key, decision, json.dumps(t.__dict__)))
        added = cur.rowcount == 1
        if added:
            self.db.execute('INSERT OR REPLACE INTO trigger_reasons VALUES (?, ?)', (t.id, reason))
        self.db.commit()
        return added

    @_locked
    def recent_triggers(self, limit=300):
        """[(Trigger, decision, reason)], most recently received first."""
        rows = self.db.execute('SELECT t.data, t.decision, COALESCE(r.reason, \'\') AS reason FROM triggers t '
                               'LEFT JOIN trigger_reasons r ON r.id = t.id ORDER BY t.rowid DESC LIMIT ?', (limit,))
        return [(Trigger(**json.loads(r['data'])), r['decision'], r['reason']) for r in rows.fetchall()]

    @_locked
    def oldest_trigger(self):
        row = self.db.execute('SELECT data FROM triggers ORDER BY rowid LIMIT 1').fetchone()
        return Trigger(**json.loads(row['data'])) if row else None

    @_locked
    def has_trigger(self, trigger_id):
        return self.db.execute('SELECT 1 FROM triggers WHERE id = ?', (trigger_id,)).fetchone() is not None

    @_locked
    def trigger(self, trigger_id):
        row = self.db.execute('SELECT data FROM triggers WHERE id = ?', (trigger_id,)).fetchone()
        return Trigger(**json.loads(row['data'])) if row else None

    @_locked
    def trigger_decisions(self, prefix):
        """{id: decision} for every stored Trigger whose id starts with `prefix`."""
        rows = self.db.execute('SELECT id, decision FROM triggers WHERE substr(id, 1, ?) = ?', (len(prefix), prefix))
        return {r['id']: r['decision'] for r in rows}

    # Tickets
    @_locked
    def save_ticket(self, t: Ticket):
        self.db.execute('INSERT OR REPLACE INTO tickets VALUES (?, ?, ?, ?)',
                        (t.id, t.thread_key, t.state, json.dumps(t.to_dict())))
        self.db.commit()

    @_locked
    def ticket(self, ticket_id):
        row = self.db.execute('SELECT data FROM tickets WHERE id = ?', (ticket_id,)).fetchone()
        return Ticket.from_dict(json.loads(row['data'])) if row else None

    @_locked
    def tickets(self, state=None, thread_key=None):
        q, args = 'SELECT data FROM tickets WHERE 1=1', []
        if state:
            q, args = q + ' AND state = ?', args + [state]
        if thread_key:
            q, args = q + ' AND thread_key = ?', args + [thread_key]
        return [Ticket.from_dict(json.loads(r['data'])) for r in self.db.execute(q + ' ORDER BY rowid', args)]

    @_locked
    def ticket_by_token(self, token):
        if not token:
            return None
        given = token.encode()
        return next((t for t in self.tickets(state='open') if t.token and hmac.compare_digest(t.token.encode(), given)),
                    None)

    # Inputs: what the worker has not been told yet.
    @_locked
    def add_input(self, ticket_id, kind, text, trigger_id=None):
        self.db.execute('INSERT INTO inputs (ticket_id, kind, text, trigger_id, at) VALUES (?, ?, ?, ?, ?)',
                        (ticket_id, kind, text, trigger_id, now_iso()))
        self.db.commit()

    @_locked
    def pending_inputs(self, ticket_id):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM inputs WHERE ticket_id = ? AND delivered = 0 ORDER BY seq', (ticket_id,))]

    @_locked
    def inputs(self, ticket_id):
        """Every input for a Ticket, delivered or not, oldest first."""
        return [dict(r) for r in self.db.execute('SELECT * FROM inputs WHERE ticket_id = ? ORDER BY seq', (ticket_id,))]

    @_locked
    def mark_delivered(self, seqs):
        self.db.executemany('UPDATE inputs SET delivered = 1 WHERE seq = ?', [(s,) for s in seqs])
        self.db.commit()

    # Effects: one row per (kind, key); run after commit, retried with backoff.
    @_locked
    def enqueue(self, kind, key, data, now=None):
        cur = self.db.execute('INSERT OR IGNORE INTO effects (kind, key, data, next_at) VALUES (?, ?, ?, ?)',
                              (kind, key, json.dumps(data), _ts(now or datetime.now(timezone.utc))))
        self.db.commit()
        return cur.rowcount == 1

    @_locked
    def due_effects(self, now=None, kinds=None):
        rows = self.db.execute('SELECT * FROM effects WHERE done = 0 AND next_at <= ? ORDER BY id',
                               (_ts(now or datetime.now(timezone.utc)),)).fetchall()
        return [dict(r, data=json.loads(r['data'])) for r in rows if not kinds or r['kind'] in kinds]

    @_locked
    def effect_done(self, effect_id):
        self.db.execute('UPDATE effects SET done = 1 WHERE id = ?', (effect_id,))
        self.db.commit()

    @_locked
    def effect_failed(self, effect_id, error, now=None):
        attempts = self.db.execute('SELECT attempts FROM effects WHERE id = ?', (effect_id,)).fetchone()[0] + 1
        delay = min(BACKOFF_START * 2 ** (attempts - 1), BACKOFF_MAX)
        self.db.execute('UPDATE effects SET attempts = ?, last_error = ?, next_at = ? WHERE id = ?',
                        (attempts, str(error)[:500], _ts((now or datetime.now(timezone.utc)) + timedelta(seconds=delay)),
                         effect_id))
        self.db.commit()
        return delay

    @_locked
    def failing_effects(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM effects WHERE done = 0 AND last_error != ''")]

    # Local requests (S15): `superagent ask` and `answer` queue them; the running service applies each once.
    @_locked
    def queue_ask(self, text, repos, actor, branch='', options=None):
        """Queue a local request for a new Ticket in `repos`; returns its id (the thread is `local:<id>`). `branch`:
        an existing branch to work on instead of a new one. `options`: {title, new_branch, skills} (see
        ask_options)."""
        cur = self.db.execute('INSERT INTO local_requests (kind, repos, text, actor, at) VALUES (?, ?, ?, ?, ?)',
                              ('ask', json.dumps(list(repos)), text, actor, now_iso()))
        # In kv, not new columns: tables are never altered (see _keep_column_reasons).
        if branch:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (f'ask-branch:{cur.lastrowid}', json.dumps(branch)))
        options = {k: v for k, v in (options or {}).items() if v}
        if options:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)',
                            (f'ask-options:{cur.lastrowid}', json.dumps(options)))
        self.db.commit()
        return cur.lastrowid

    @_locked
    def ask_branch(self, request_id):
        """The existing branch a local request asked to work on; '' for a new branch."""
        row = self.db.execute('SELECT v FROM kv WHERE k = ?', (f'ask-branch:{request_id}',)).fetchone()
        return json.loads(row['v']) if row else ''

    @_locked
    def ask_options(self, request_id):
        """What a local request chose for its Ticket: {title: the Ticket's title ('' = the request's first line),
        new_branch: the name of its new branch ('' = agent/<name>), skills: skill paths for its worker, model and
        thinking: its worker's ('' = the config's)}."""
        row = self.db.execute('SELECT v FROM kv WHERE k = ?', (f'ask-options:{request_id}',)).fetchone()
        return {'title': '', 'new_branch': '', 'skills': [], 'model': '', 'thinking': '',
                **(json.loads(row['v']) if row else {})}

    @_locked
    def queue_answer(self, ticket_id, text, actor):
        """Queue an answer for the Ticket and record it in its conversation, in one transaction."""
        at = now_iso()
        cur = self.db.execute('INSERT INTO local_requests (kind, ticket_id, text, actor, at) VALUES (?, ?, ?, ?, ?)',
                              ('answer', ticket_id, text, actor, at))
        self.db.execute('INSERT INTO local_messages (ticket_id, at, kind, body, key, actor) VALUES (?, ?, ?, ?, ?, ?)',
                        (ticket_id, at, 'answer', text, f'answer-{cur.lastrowid}', actor))
        self.db.commit()
        return cur.lastrowid

    @_locked
    def local_requests(self):
        """Requests the service has not applied yet, oldest first."""
        return [dict(r) for r in self.db.execute('SELECT * FROM local_requests WHERE done = 0 ORDER BY id')]

    @_locked
    def local_request(self, request_id):
        row = self.db.execute('SELECT * FROM local_requests WHERE id = ?', (request_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def local_request_done(self, request_id, ticket_id=None):
        self.db.execute('UPDATE local_requests SET done = 1, ticket_id = COALESCE(?, ticket_id) WHERE id = ?',
                        (ticket_id, request_id))
        self.db.commit()

    @_locked
    def add_local_message(self, ticket_id, kind, body, key):
        """Add a message to a local Ticket's conversation; the same `key` twice is a no-op."""
        cur = self.db.execute('INSERT OR IGNORE INTO local_messages (ticket_id, at, kind, body, key) '
                              'VALUES (?, ?, ?, ?, ?)', (ticket_id, now_iso(), kind, body, key))
        self.db.commit()
        return cur.rowcount == 1

    @_locked
    def local_messages(self, ticket_id):
        """The Ticket's local outbox (`reply`) and answers (`answer`), oldest first."""
        return [dict(r) for r in self.db.execute('SELECT * FROM local_messages WHERE ticket_id = ? ORDER BY seq',
                                                 (ticket_id,))]

    # KV: cursors and small settings.
    @_locked
    def get(self, k, default=None):
        row = self.db.execute('SELECT v FROM kv WHERE k = ?', (k,)).fetchone()
        return json.loads(row['v']) if row else default

    @_locked
    def put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (k, json.dumps(v)))
        self.db.commit()

    @_locked
    def close(self):
        self.db.close()
