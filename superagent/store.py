"""SQLite store: rows are JSON, side effects are written before they run (the effect queue)."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .model import Ticket, Trigger, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT);
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


class Store:
    def __init__(self, path):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)

    # Triggers: the same id twice is a no-op.
    def add_trigger(self, t: Trigger, decision):
        cur = self.db.execute('INSERT OR IGNORE INTO triggers VALUES (?, ?, ?, ?)',
                              (t.id, t.thread_key, decision, json.dumps(t.__dict__)))
        self.db.commit()
        return cur.rowcount == 1

    def has_trigger(self, trigger_id):
        return self.db.execute('SELECT 1 FROM triggers WHERE id = ?', (trigger_id,)).fetchone() is not None

    # Tickets
    def save_ticket(self, t: Ticket):
        self.db.execute('INSERT OR REPLACE INTO tickets VALUES (?, ?, ?, ?)',
                        (t.id, t.thread_key, t.state, json.dumps(t.to_dict())))
        self.db.commit()

    def ticket(self, ticket_id):
        row = self.db.execute('SELECT data FROM tickets WHERE id = ?', (ticket_id,)).fetchone()
        return Ticket.from_dict(json.loads(row['data'])) if row else None

    def tickets(self, state=None, thread_key=None):
        q, args = 'SELECT data FROM tickets WHERE 1=1', []
        if state:
            q, args = q + ' AND state = ?', args + [state]
        if thread_key:
            q, args = q + ' AND thread_key = ?', args + [thread_key]
        return [Ticket.from_dict(json.loads(r['data'])) for r in self.db.execute(q + ' ORDER BY rowid', args)]

    def ticket_by_token(self, token):
        return next((t for t in self.tickets(state='open') if token and t.token == token), None)

    # Inputs: what the worker has not been told yet.
    def add_input(self, ticket_id, kind, text, trigger_id=None):
        self.db.execute('INSERT INTO inputs (ticket_id, kind, text, trigger_id, at) VALUES (?, ?, ?, ?, ?)',
                        (ticket_id, kind, text, trigger_id, now_iso()))
        self.db.commit()

    def pending_inputs(self, ticket_id):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM inputs WHERE ticket_id = ? AND delivered = 0 ORDER BY seq', (ticket_id,))]

    def mark_delivered(self, seqs):
        self.db.executemany('UPDATE inputs SET delivered = 1 WHERE seq = ?', [(s,) for s in seqs])
        self.db.commit()

    # Effects: one row per (kind, key); run after commit, retried with backoff.
    def enqueue(self, kind, key, data, now=None):
        cur = self.db.execute('INSERT OR IGNORE INTO effects (kind, key, data, next_at) VALUES (?, ?, ?, ?)',
                              (kind, key, json.dumps(data), _ts(now or datetime.now(timezone.utc))))
        self.db.commit()
        return cur.rowcount == 1

    def due_effects(self, now=None, kinds=None):
        rows = self.db.execute('SELECT * FROM effects WHERE done = 0 AND next_at <= ? ORDER BY id',
                               (_ts(now or datetime.now(timezone.utc)),))
        return [dict(r, data=json.loads(r['data'])) for r in rows if not kinds or r['kind'] in kinds]

    def effect_done(self, effect_id):
        self.db.execute('UPDATE effects SET done = 1 WHERE id = ?', (effect_id,))
        self.db.commit()

    def effect_failed(self, effect_id, error, now=None):
        attempts = self.db.execute('SELECT attempts FROM effects WHERE id = ?', (effect_id,)).fetchone()[0] + 1
        delay = min(BACKOFF_START * 2 ** (attempts - 1), BACKOFF_MAX)
        self.db.execute('UPDATE effects SET attempts = ?, last_error = ?, next_at = ? WHERE id = ?',
                        (attempts, str(error)[:500], _ts((now or datetime.now(timezone.utc)) + timedelta(seconds=delay)),
                         effect_id))
        self.db.commit()
        return delay

    def failing_effects(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM effects WHERE done = 0 AND last_error != ''")]

    # KV: cursors and small settings.
    def get(self, k, default=None):
        row = self.db.execute('SELECT v FROM kv WHERE k = ?', (k,)).fetchone()
        return json.loads(row['v']) if row else default

    def put(self, k, v):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (k, json.dumps(v)))
        self.db.commit()
