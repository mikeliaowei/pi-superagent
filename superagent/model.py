"""The domain, ported from dsh-superagent's src/core: no I/O here.

A Trigger is one message from a Source. Accepted Triggers open a Ticket, the unit of work. Each attempt to work
on a Ticket is an Assignment, which ends with the worker's Report.
"""

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

REPORTS = ('progress', 'waiting', 'ready', 'blocked', 'failed', 'dropped')
OUTCOMES = ('completed', 'merged', 'closed', 'stopped', 'failed')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def short_name(title, taken=()):
    """1-4 kebab-case words, at most 32 characters, unique among `taken` (the Ticket's branch is agent/<name>)."""
    words = re.findall(r'[a-z0-9]+', title.lower())[:4] or ['task']
    base = '-'.join(words)[:32].strip('-') or 'task'
    name, n = base, 2
    while name in taken:
        name = f'{base[:29]}-{n}'
        n += 1
    return name


@dataclass
class Trigger:
    id: str  # deterministic per source event, so receiving it twice is a no-op
    thread_key: str  # e.g. 'github:owner/repo#12'
    actor: str
    human: bool
    addressed: bool  # the text @-mentions the agent
    at: str
    text: str
    url: str = ''
    kind: str = 'comment'  # comment | issue | review
    meta: dict = field(default_factory=dict)


@dataclass
class Assignment:
    id: str
    state: str = 'pending'  # pending -> running -> ended
    report: str | None = None  # one of REPORTS
    summary: str = ''
    question: str = ''
    started_at: str | None = None
    ended_at: str | None = None
    end_reason: str = ''


@dataclass
class Ticket:
    id: str
    name: str
    title: str
    thread_key: str
    repo: str
    number: int
    mandate: dict  # {actor, strength: 'explicit', trigger_id, at}; never changes
    created_at: str
    state: str = 'open'  # open | ended
    outcome: str | None = None  # one of OUTCOMES once ended
    branch: str = ''
    worktree: str = ''
    pr_number: int | None = None
    token: str = ''  # per-Ticket secret the worker uses to report back
    assignments: list = field(default_factory=list)
    last_human_at: str | None = None
    delivery_error: str = ''

    @property
    def current(self):
        return Assignment(**self.assignments[-1]) if self.assignments else None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def stage(t):
    """What the Ticket needs right now. The first matching row wins, as in dsh-superagent's stage.ts."""
    a = t.current
    if t.state == 'ended':
        return 'ended'
    if t.delivery_error:
        return 'person/delivery'
    person_since = bool(a and a.ended_at and t.last_human_at and t.last_human_at > a.ended_at)
    if a and a.report == 'waiting' and a.question and not person_since:
        return 'person/answer'
    if a and a.report == 'ready' and not person_since:
        return 'person/review'
    if a and a.report in ('blocked', 'failed') and not person_since:
        return f'person/{a.report}'
    if a is None or a.state == 'pending':
        return 'working/queued'
    if a.report == 'waiting' or a.state == 'ended':
        return 'idle'
    return 'working'
