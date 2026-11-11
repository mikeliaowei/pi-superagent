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


TITLE_MAX = 80


def ticket_title(text, limit=TITLE_MAX):
    """One line of at most `limit` characters; a longer one ends at its last whole word, marked with an ellipsis."""
    title = ' '.join(text.split()) or 'Request'
    if len(title) <= limit:
        return title
    cut = title[:limit - 1]
    if ' ' in cut:
        cut = cut[:cut.rindex(' ')]
    return cut.rstrip(' ,;:.-') + '…'


NAME_WORDS, NAME_MAX = 8, 48


def short_name(title, taken=()):
    """Up to 8 kebab-case words, at most 48 characters cut at a whole word, unique among `taken` (the Ticket's
    branch is agent/<name>)."""
    words = re.findall(r'[a-z0-9]+', title.lower())[:NAME_WORDS] or ['task']
    base = words[0][:NAME_MAX]
    for w in words[1:]:
        if len(base) + 1 + len(w) > NAME_MAX:
            break
        base = f'{base}-{w}'
    name, n = base, 2
    while name in taken:
        name = f'{base[:NAME_MAX - 3]}-{n}'
        n += 1
    return name


def named_repos(text, configured):
    """The `configured` repos (owner/name) that `text` names, in order of first mention, matched case-insensitively
    and word-bounded. Quoted lines (`>`) are someone else's words and name nothing. Returns configured spellings."""
    own = '\n'.join(line for line in (text or '').splitlines() if not line.lstrip().startswith('>'))
    found = []
    for r in configured:
        m = re.search(rf'(?<![\w./-]){re.escape(r)}(?![\w/-]|\.\w)', own, flags=re.I)
        if m:
            found.append((m.start(), r))
    return [r for _, r in sorted(found)]


def new_part(repo):
    """One repo of a cross-repo Ticket."""
    return {'repo': repo, 'worktree': '', 'pr_number': None, 'test_result': '', 'pushed': ''}


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
    # The repo's test command (S13). New fields have defaults, so Tickets stored by earlier versions still load.
    test_failures: int = 0  # failed runs since the last pass
    test_output: str = ''  # the tail of the last failing run's output
    test_result: str = ''  # the PR body line of the last passing run
    # A cross-repo Ticket (S14) works in several repos: one part per repo, the thread's repo first. Each part is
    # {repo, worktree, pr_number, test_result, pushed}; `worktree` above is then the ticket folder holding them.
    # Empty for a single-repo Ticket, which keeps using worktree, pr_number and test_result above.
    parts: list = field(default_factory=list)
    # A branch that already exists on GitHub to work on (a local request's choice): the worktree starts from it,
    # commits are pushed onto it without force, and its open PR is reused. Empty: a new branch agent/<name>.
    existing_branch: str = ''
    # Skill folders (or files) the request chose for its worker, given to Pi with --skill besides the worker's own.
    skills: list = field(default_factory=list)
    # The worker's model ('provider/model' or a model of the config's provider) and thinking level; '' = config's.
    model: str = ''
    thinking: str = ''
    # The worker's markdown description of its change, from its last `ready` report; goes into the PR body.
    details: str = ''

    @property
    def repos(self):
        """Every repo this Ticket works in, its own first."""
        return [p['repo'] for p in self.parts] or [self.repo]

    @property
    def current(self):
        return Assignment(**self.assignments[-1]) if self.assignments else None

    def to_dict(self):
        # Fields added after the first release are left out while unused: the previous version loads Tickets with
        # Ticket(**data) and would crash on keys it does not know, so a rollback keeps working.
        return {k: v for k, v in asdict(self).items() if not (k in LATER_FIELDS and v == LATER_FIELDS[k])}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# Ticket fields added after the first release, with the value meaning "not used".
LATER_FIELDS = {'test_failures': 0, 'test_output': '', 'test_result': '', 'parts': [], 'existing_branch': '',
                'skills': [], 'model': '', 'thinking': '', 'details': ''}

THINKING_LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')


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
