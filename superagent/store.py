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
CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT, reason TEXT DEFAULT '');
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
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript(SCHEMA)
            self._upgrade()

    def _upgrade(self):
        """Bring a database from an older version up to SCHEMA. Only adds columns; never rewrites rows."""
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(triggers)')}
        if 'reason' not in columns:
            self.db.execute("ALTER TABLE triggers ADD COLUMN reason TEXT DEFAULT ''")
            self.db.commit()

    # Triggers: the same id twice is a no-op.
    @_locked
    def add_trigger(self, t: Trigger, decision, reason=''):
        cur = self.db.execute('INSERT OR IGNORE INTO triggers (id, thread_key, decision, data, reason) '
                              'VALUES (?, ?, ?, ?, ?)', (t.id, t.thread_key, decision, json.dumps(t.__dict__), reason))
        self.db.commit()
        return cur.rowcount == 1

    @_locked
    def recent_triggers(self, limit=300):
        """[(Trigger, decision, reason)], most recently received first."""
        rows = self.db.execute('SELECT data, decision, reason FROM triggers ORDER BY rowid DESC LIMIT ?', (limit,))
        return [(Trigger(**json.loads(r['data'])), r['decision'], r['reason'] or '') for r in rows.fetchall()]

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

    # KV: cursors and small settings.
    @_locked
    def get(self, k, default=None):
        row = self.db.execute('SELECT v FROM kv WHERE k = ?', (k,)).fetchone()
        return json.loads(row['v']) if row else default

    @_locked
    def put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (k, json.dumps(v)))
        self.db.commit()
