"""The web panel: shows only what needs a person, plus enough context to act on it.

Run it with `superagent panel` (or `streamlit run superagent/panel.py -- <config>`); the config path comes from
the SUPERAGENT_CONFIG environment variable or the argument after `--`. The panel reads the store through Store
methods only. Its Stop and Retry buttons record the same operator requests as `superagent stop|retry`, which the
running service applies; its New request form and Answer box call the same functions as `superagent ask|answer`. It never shows the GitHub token, a Ticket's report token, or environment values.
"""

import os
import re
import sys
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from superagent import cli, config, worker_log
from superagent.model import THINKING_LEVELS, short_name, stage

REFRESH_SECONDS = 10
RECENT_TRIGGERS = 300
DECISIONS = ('accept', 'stop', 'context', 'ignore')
WORKER_STEPS = 50
WORKER_CLIP = 4000
STEP_ICONS = {'request': ':material/assignment:', 'reasoning': ':material/psychology:', 'said': ':material/chat:',
              'tool': ':material/terminal:', 'result': ':material/check_circle:', 'error': ':material/error:'}
HOME_TAB, TICKETS_TAB, STATS_TAB, SETTINGS_TAB = MAIN_TABS = (
    ':material/home: Home', ':material/confirmation_number: Tickets', ':material/bar_chart: Stats',
    ':material/settings: Settings')
# The top menu (st.tabs key main-tabs) as a raised segmented control: a sunken tray, the selected tab lifted out of
# it, bold and shadowed, so it reads by shape and weight, not color alone. Nested tabs (in a panel) keep Streamlit's
# look. Streamlit 1.59+ tabs: [data-testid=stTab] with react-aria [data-selected], [data-hovered],
# [data-focus-visible].
_TOP = '.st-key-main-tabs'
_TAB = f'{_TOP} [data-testid="stTab"]:not([data-testid="stTabPanel"] *)'
TAB_STYLE = f'''<style>
{_TOP} [role="tablist"]:not([data-testid="stTabPanel"] *) {{
  gap: 6px; padding: 6px; margin-bottom: 0.75rem; border-radius: 14px; border: none;
  background: linear-gradient(180deg, rgba(49, 51, 63, 0.09), rgba(49, 51, 63, 0.04));
  box-shadow: inset 0 2px 5px rgba(0, 0, 0, 0.12), inset 0 -1px 0 rgba(255, 255, 255, 0.7);
}}
{_TAB} {{
  min-height: 48px; padding: 0 22px; border-radius: 10px;
  transition: transform 150ms ease, box-shadow 150ms ease, background-color 150ms ease;
}}
{_TAB} p {{ font-size: 1.1rem; font-weight: 600; }}
{_TAB}[data-hovered]:not([data-selected]) {{
  background: rgba(255, 255, 255, 0.6); transform: translateY(-1px); box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
}}
{_TAB}[data-selected] {{
  background: linear-gradient(180deg, #ffffff, #f7f8fa); transform: translateY(-2px);
  box-shadow: inset 0 1px 0 #ffffff, 0 6px 14px rgba(0, 0, 0, 0.14), 0 2px 4px rgba(0, 0, 0, 0.10);
}}
{_TAB}[data-selected] p {{ font-weight: 700; }}
{_TAB}[data-focus-visible] {{ outline: 2px solid currentColor; outline-offset: 2px; }}
{_TAB} .react-aria-SelectionIndicator {{ display: none; }}
@media (prefers-reduced-motion: reduce) {{ {_TAB} {{ transition: none; transform: none !important; }} }}
</style>'''


AGENT_NAMES = {'pi': 'Pi', 'copilot': 'GitHub Copilot CLI'}


def config_path():
    if os.environ.get('SUPERAGENT_CONFIG'):
        return os.environ['SUPERAGENT_CONFIG']
    if os.path.basename(sys.argv[0]) == 'panel.py' and len(sys.argv) > 1:
        return sys.argv[1]
    return None


def parse_time(s):
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def issue_url(t):
    """The request thread on GitHub; None for a local Ticket, which has none."""
    return None if cli.is_local(t) else f'https://github.com/{t.repo}/issues/{t.number}'


def thread_markdown(t):
    return f'local request `{t.thread_key}`' if cli.is_local(t) else f'[#{t.number}]({issue_url(t)})'


def pr_url(t):
    """The single PR link; for a cross-repo Ticket the first repo's PR (pr_links has them all)."""
    if t.parts:
        return next((u for _, u in pr_links(t)), None)
    return f'https://github.com/{t.repo}/pull/{t.pr_number}' if t.pr_number else None


def pr_links(t):
    """[(label, url)] for every PR of the Ticket: one per repo of a cross-repo Ticket."""
    if t.parts:
        return [(f"{p['repo']}#{p['pr_number']}", f"https://github.com/{p['repo']}/pull/{p['pr_number']}")
                for p in t.parts if p['pr_number']]
    return [(f'PR #{t.pr_number}', pr_url(t))] if t.pr_number else []


def pr_markdown(t):
    return ''.join(f' · [{label}]({url})' for label, url in pr_links(t))


def updated_at(t):
    times = [t.created_at, t.last_human_at, *(x for a in t.assignments for x in (a['started_at'], a['ended_at']))]
    return max((x for x in times if x), default=t.created_at)


def ago(s, now=None):
    """'just now', '5 min ago', '3 h ago', '2 d ago', then the date: how long since an ISO time."""
    at = parse_time(s)
    if not at:
        return '-'
    secs = ((now or datetime.now(timezone.utc)) - at).total_seconds()
    if secs < 60:
        return 'just now'
    if secs < 3600:
        return f'{int(secs // 60)} min ago'
    if secs < 86400:
        return f'{int(secs // 3600)} h ago'
    if secs < 7 * 86400:
        return f'{int(secs // 86400)} d ago'
    return at.astimezone().strftime('%Y-%m-%d')


def ended_at(t):
    a = t.current
    return (a.ended_at if a and a.ended_at else None) or updated_at(t)


@st.cache_data(max_entries=5000, show_spinner=False)
def _usage(stamps, since):
    return worker_log.usage([p for p, _ in stamps], since)


def _stamps(cfg, t, since=None):
    """The ticket's session files with their mtime and size: a cache key that changes when a file does."""
    stamps = []
    for p in worker_log.session_files(cfg.state_dir, t.id):
        try:
            s = os.stat(p)
        except OSError:
            continue
        if since is None or s.st_mtime >= since.timestamp():  # an older file has no call since then
            stamps.append((p, (s.st_mtime_ns, s.st_size)))
    return stamps


def ticket_usage(cfg, t, since=None):
    """The ticket's worker usage (worker_log.usage), cached until a session file changes."""
    return _usage(tuple(_stamps(cfg, t, since)), since)


def usage_cells(cfg, t):
    u = ticket_usage(cfg, t)
    return {'tokens': u['tokens'], 'cost': u['cost']}


USAGE_COLUMNS = {'tokens': st.column_config.NumberColumn('tokens', format='compact'),
                 'cost': st.column_config.NumberColumn('cost', format='$%.2f', help="Pi's estimate, or Copilot's AI credits at $0.01 each (list price)")}


ACTION_LABELS = {'stop': 'Stop', 'retry': 'Retry', 'done': 'Mark done'}
ACTION_HELP = {'stop': 'End it as stopped and stop its worker.', 'retry': 'Relaunch its worker, or redeliver it.',
               'done': 'End it as done without a pull request: for work that only changed Jira or Confluence, '
                       'or answered a question.'}


def act(store, ticket, action, where):
    if st.button(ACTION_LABELS[action], key=f'{where}-{action}-{ticket.id}', help=ACTION_HELP[action],
                 icon=':material/task_alt:' if action == 'done' else None):
        try:
            st.success(cli.request_action(store, ticket.id, action))
        except RuntimeError as e:
            st.error(str(e))


def actions(store, t, where):
    if t.state == 'ended':
        return
    with st.container(horizontal=True):
        for action in ('done', 'stop', 'retry'):
            act(store, t, action, where)


def answer_box(cfg, store, t, where, attach=True):
    """An answer to the Ticket's question or an instruction, sent as `superagent answer` sends it; for an ended
    Ticket the instruction reopens it. `attach`: offer files too (the ticket's details do; a Home card stays short)."""
    if t.state == 'ended':
        label, button = 'Instruction to reopen this ticket', 'Reopen with instruction'
        tip = 'Reopens the ticket and resumes its worker on the same branch with this instruction.'
    elif stage(t) == 'person/answer':
        label, button = 'Answer', 'Answer'
        tip = 'A running worker gets it right away; a finished one is resumed with it.'
    else:
        label, button = 'Instruction for the worker', 'Send instruction'
        tip = 'A running worker gets it right away; a finished one is resumed with it.'
    text = st.text_area(label, key=f'{where}-answer-text-{t.id}', help=tip)
    files = attach_box(f'{where}-answer-files-{t.id}') if attach else []
    if st.button(button, key=f'{where}-answer-{t.id}', type='primary', icon=':material/send:'):
        try:
            st.success(cli.answer_request(cfg, store, t.id, text, files))
        except RuntimeError as e:
            st.error(str(e))


def attach_box(key):
    """Files for the worker: [(name, bytes)]. Kept under <state_dir>/attachments, never committed; images are also
    shown to its model."""
    up = st.file_uploader('Attach files', accept_multiple_files=True, key=key,
                          help='Screenshots, logs, specs: the worker gets their paths, and sees images. At most 10 '
                               'files of 10 MB each.')
    return [(f.name, f.getvalue()) for f in up or []]


def conversation(cfg, store, t):
    """A local Ticket's conversation: the request, the local outbox and the answers, as `superagent show` lists it."""
    st.markdown('Conversation')
    for msg in cli.show_data(cfg, store, t.id)['conversation']:
        st.markdown(f"**{msg['from']}** · {msg['kind']} · {msg['at']}")
        st.text(msg['text'])


SHELL_TOOLS = {'bash': 'bash', 'shell': 'bash', 'powershell': 'powershell'}  # tool name: code highlighting
REPORT_STATUS = re.compile(r'--status[ =]+["\']?(\w+)')
REPORT_SUMMARY = re.compile(r'--summary[ =]+(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|(\S+))')
LEGEND = (':blue-badge[:material/chat: Agent] :violet-badge[:material/terminal: Shell] '
          ':orange-badge[:material/build: Tool] :green-badge[:material/check_circle: Result] '
          ':red-badge[:material/error: Error] :green-badge[:material/flag: Report] '
          ':gray-badge[:material/assignment: Assignment]')


def report_of(s):
    """(status, summary) when the step is the worker's `superagent-report` call, else None."""
    if s.kind != 'tool' or 'superagent-report' not in s.text:
        return None
    status, summary = REPORT_STATUS.search(s.text), REPORT_SUMMARY.search(s.text)
    return (status.group(1) if status else '?',
            next((g for g in summary.groups() if g is not None), '') if summary else '')


def step_time(s):
    """A step's time in the viewer's zone, with the date: a retried Ticket's steps span days."""
    at = parse_time(s.at)
    return at.astimezone().strftime('%Y-%m-%d %H:%M:%S') if at else s.at


def worker_step(s):
    when = step_time(s)
    report = report_of(s)
    if report:
        st.success(f'**Report: {report[0]}** · {when}\n\n{report[1]}', icon=':material/flag:')
    elif s.kind == 'tool' and s.title in SHELL_TOOLS:
        st.markdown(f':violet-badge[:material/terminal: Shell · {s.title}] {when}')
        st.code(s.text, language=SHELL_TOOLS[s.title], wrap_lines=True)
    elif s.kind == 'tool':
        st.markdown(f':orange-badge[:material/build: {s.title}] {when}')
        st.code(s.text, language=None, wrap_lines=True)
    elif s.kind == 'said':
        st.info(f'**Agent** · {when}\n\n{s.text}', icon=':material/chat:')
    elif s.kind == 'reasoning':
        st.caption(f':material/psychology: Reasoning · {when}\n\n{s.text}')
    else:  # the assignment and tool output are long, so they start folded
        text = s.text if len(s.text) <= WORKER_CLIP else s.text[:WORKER_CLIP] + '\n… (clipped)'
        color = {'request': 'gray', 'result': 'green', 'error': 'red'}[s.kind]
        with st.expander(f':{color}[{s.title}] · {when} · {len(s.text.splitlines())} lines',
                         icon=STEP_ICONS[s.kind]):
            st.code(text, language=None, wrap_lines=True)


def worker_activity(cfg, t):
    """What the worker did, read from its Pi or Copilot session files; tokens are masked."""
    st.markdown('Worker activity')
    u = ticket_usage(cfg, t)
    if u['tokens']:
        st.caption(f"Usage: {u['tokens']:,} tokens (input {u['input']:,}, output {u['output']:,}, cache read "
                   f"{u['cacheRead']:,}, cache write {u['cacheWrite']:,}) · cost ${u['cost']:.2f} (Pi's estimate, or Copilot's AI credits at "
                   "list price)")
    steps = worker_log.steps(worker_log.session_files(cfg.state_dir, t.id), secrets=[cfg.token, t.token])
    if not steps:
        st.info('No worker activity yet.')
        return
    row = st.container(horizontal=True)
    reasoning = row.toggle('Show reasoning', key=f'worker-reasoning-{t.id}')
    show_all = row.toggle('Show all steps', key=f'worker-all-{t.id}')
    newest_first = row.toggle('Newest first', key=f'worker-newest-{t.id}')
    st.caption(f'Started {step_time(steps[0])} · last step {step_time(steps[-1])}')
    if not reasoning:
        steps = [s for s in steps if s.kind != 'reasoning']
    if not show_all and len(steps) > WORKER_STEPS:
        st.caption(f'Showing the last {WORKER_STEPS} of {len(steps)} steps.')
        steps = steps[-WORKER_STEPS:]
    if newest_first:
        steps = steps[::-1]
    st.markdown(LEGEND)
    with st.container(height=500, border=True):
        for s in steps:
            worker_step(s)


def existing_branch(cfg, repos, where=''):
    """Pick a branch of the one chosen repo, as its checkout knows them (GitHub and local), or type its name."""
    if len(repos) != 1:
        st.caption('Choose one repo to list its branches.')
        return ''
    repo = repos[0]
    if st.button('Refresh from GitHub', key=f'{where}new-request-branch-refresh',
                 help=f'Runs git fetch --prune origin in the checkout of {repo}.'):
        try:
            st.success(cli.fetch_branches(cfg, repo))
        except RuntimeError as e:
            st.error(str(e))
    try:
        found = dict(cli.list_branches(cfg, repo))
    except RuntimeError as e:
        st.error(str(e))
        found = {}
    name = st.selectbox('Existing branch', list(found), index=None, key=f'{where}new-request-branch', accept_new_options=True,
                        format_func=lambda n: f'{n} · {found[n]}' if n in found else n,
                        placeholder='Pick a branch or type its name',
                        help='GitHub branches as of the last fetch; a local-only branch is published on delivery.')
    return name or ''


def worker_skills(cfg, repos, where=''):
    """Pick skills for the worker, from the user's skill folders and the chosen repos'."""
    found = {path: f'{name} · {where}' for path, name, where in cli.list_skills(cfg, repos)}
    if not found:
        st.caption('No skills found to add: the worker gets its own superagent-worker skill only.')
        return []
    return st.multiselect('Skills for the worker', list(found), key=f'{where}new-request-skills',
                          format_func=lambda p: found.get(p) or p, placeholder='Its own skill only',
                          help='Added to its own superagent-worker skill. From ~/.pi/agent/skills, ~/.agents/skills '
                               "and the chosen repos' .agents/skills, .pi/skills and .claude/skills.")


@st.cache_data(ttl=3600, show_spinner='Asking for the models the worker can use...')
def _worker_models(agent, exe, state_dir, _cfg):
    if agent == 'copilot':
        return {m: f'{m} · {note}' if note else m for m, note in cli.copilot_models(_cfg)}
    return {m: m for m in cli.list_models(_cfg)}


def worker_models(cfg):
    """{model: label} the worker can run: what Copilot offers this user (with its premium-request cost), or
    `pi --list-models`. Shared by every panel user for an hour: asking Copilot takes about 10 s."""
    found = _worker_models(cfg.agent, cfg.copilot if cfg.agent == 'copilot' else cfg.pi, cfg.state_dir, cfg)
    if not found:
        _worker_models.clear()  # nothing to offer (not signed in?): ask again next time, not in an hour
    return found


def model_input(cfg, where):
    """The Settings model field: Copilot's models to pick from (or type one); Pi's model typed, its provider apart."""
    if cfg.agent != 'copilot':
        return where.text_input('Model', cfg.model, key='set-model')
    choices = worker_models(cfg)
    options = list(choices) if cfg.model in choices else [cfg.model, *choices]
    return where.selectbox('Model', options, index=options.index(cfg.model), key='set-model',
                           accept_new_options=True, format_func=lambda m: choices.get(m, m),
                           help="From GitHub Copilot, with each model's premium-request cost, or type one.")


def worker_setup(cfg, where=''):
    """Pick the worker's model and thinking level; None keeps the config's."""
    copilot = cfg.agent == 'copilot'
    default = cfg.model if copilot else f'{cfg.provider}/{cfg.model}'
    choices = worker_models(cfg)
    left, right = st.columns([3, 1])
    model = left.selectbox('Model', list(choices), index=None, key=f'{where}new-request-model', accept_new_options=True,
                           format_func=lambda m: choices.get(m, m), placeholder=f'Config default: {default}',
                           help="From GitHub Copilot, with each model's premium-request cost, or type one. Empty "
                                "keeps the config's." if copilot else
                           'From `pi --list-models`, or type provider/model. Empty keeps the config\'s.')
    thinking = right.selectbox('Thinking level', THINKING_LEVELS, index=None, key=f'{where}new-request-thinking',
                               placeholder=f'Default: {cfg.thinking}', help='Empty keeps the config\'s.')
    return model or '', thinking or ''


def new_request(cfg, store, tickets, where=''):
    """The same request as `superagent ask`, or, to continue an open Ticket (under More options), the same as
    `superagent answer`. The request text comes first; what most requests leave as it is waits under More options.
    `where`: a widget key prefix."""
    open_tickets = {t.id: t for t in tickets if t.state == 'open'}
    tid = st.session_state.get(f'{where}new-request-continue')  # picked under More options, below
    tid = tid if tid in open_tickets else None
    text = st.text_area('What should superagent do?', key=f'{where}new-request-text', height=140,
                        placeholder='Describe the change, bug or question. Jira issues and Confluence pages you '
                                    'mention are read for the worker.')
    title, repos, branch, new_branch, skills, model, thinking = '', [], '', '', [], '', ''
    if tid:
        t = open_tickets[tid]
        st.caption(f"Goes to {t.name}'s worker on branch `{t.branch or '-'}` in {', '.join(t.repos)}; "
                   'no new ticket or branch.')
    else:
        left, right = st.columns(2)
        title = left.text_input('Title', key=f'{where}new-request-title',
                                help="The ticket's title; its name is made from it.")
        last = [r for r in (store.get('panel:last-repos') or '').split(',') if r in cfg.repo_names]
        repos = right.multiselect('Repos', cfg.repo_names, key=f'{where}new-request-repos',
                                  default=cfg.repo_names if len(cfg.repo_names) == 1 else last,
                                  placeholder='Choose repos', help='Remembers the repos of your last request.')
    files = attach_box(f'{where}new-request-files')
    with st.expander('More options', icon=':material/tune:'):
        st.selectbox('Continue ticket', [None, *open_tickets], key=f'{where}new-request-continue',
                     format_func=lambda i: 'None: open a new ticket and branch' if i is None else
                     f"{open_tickets[i].name} · {', '.join(open_tickets[i].repos)} · {open_tickets[i].branch or '-'}",
                     help='Send this to the worker of an open ticket instead of starting a new one.')
        if not tid:
            kind = st.radio('Branch', ['New branch', 'Existing branch'], horizontal=True,
                            key=f'{where}new-request-branch-kind',
                            help='New: a new branch from the base branch, agent/<name> unless you name it. Existing: '
                                 'work on a branch of one repo, on GitHub or only local; commits go on top of it and '
                                 'its open PR is reused.')
            if kind == 'Existing branch':
                branch = existing_branch(cfg, repos, where)
            else:
                new_branch = st.text_input('New branch name', key=f'{where}new-request-new-branch',
                                           placeholder=f'agent/{short_name(title)}' if title.strip() else 'agent/<name>',
                                           help='Leave empty for the default. It must not exist yet.')
            skills = worker_skills(cfg, repos, where)
            model, thinking = worker_setup(cfg, where)
    if st.button('Send', key=f'{where}new-request-submit', type='primary', icon=':material/send:'):
        try:
            if tid:
                message = cli.answer_request(cfg, store, tid, text, files)
            elif not title.strip():
                raise RuntimeError('Give the request a title: it names the ticket.')
            else:
                message = cli.ask_request(cfg, store, text, repos, branch, title, new_branch, skills, model,
                                          thinking, files)[1]
                store.put('panel:last-repos', ','.join(repos))
            st.success(message)
        except RuntimeError as e:
            st.error(str(e))


STATUS = {'working/queued': 'Queued', 'working': 'Working', 'idle': 'Idle', 'person/answer': 'Waiting for your answer',
          'person/review': 'Ready for review', 'person/blocked': 'Blocked', 'person/failed': 'Failed',
          'person/delivery': 'Delivery failed'}
OUTCOMES = {'merged': 'Merged', 'completed': 'Done', 'closed': 'Closed without merge', 'stopped': 'Stopped'}
SUCCEEDED = ('merged', 'completed')  # a merged pull request, or marked done (work with no pull request)
# Each status as a badge: icon and words as well as color, so it never reads by color alone.
STATUS_LOOK = {'Queued': ('gray', 'schedule'), 'Working': ('blue', 'autorenew'), 'Idle': ('gray', 'pause_circle'),
               'Waiting for your answer': ('orange', 'help'), 'Ready for review': ('green', 'rate_review'),
               'Blocked': ('red', 'block'), 'Failed': ('red', 'error'), 'Delivery failed': ('red', 'error'),
               'Merged': ('green', 'check_circle'), 'Closed without merge': ('gray', 'cancel'),
               'Stopped': ('gray', 'stop_circle'), 'Done': ('green', 'task_alt')}
DECISION_NAMES = {'accept': 'Started work', 'context': 'Added as context', 'stop': 'Stopped a ticket',
                  'ignore': 'Ignored'}
RUN_COLUMNS = ['id', 'state', 'report', 'summary', 'question', 'end_reason', 'started_at', 'ended_at']
RANGES = {'Last 7 days': 7, 'Last 30 days': 30, 'Last 90 days': 90, 'All time': None}
OPEN_COLOR, ENDED_COLOR = '#1f77b4', '#ff7f0e'  # blue and orange: told apart at a glance


def status(t):
    """The Ticket's stage (or its outcome, once ended) in plain words."""
    if t.state == 'ended':
        return OUTCOMES.get(t.outcome, 'Ended')
    return STATUS.get(stage(t), stage(t))


def badge(t):
    """The Ticket's status as a colored badge with an icon, in Markdown."""
    words = status(t)
    color, icon = STATUS_LOOK.get(words, ('gray', 'info'))
    return f':{color}-badge[:material/{icon}: {words}]'


def _day(s):
    at = parse_time(s)
    return at.astimezone(timezone.utc).strftime('%Y-%m-%d') if at else None


@st.cache_data(max_entries=5000, show_spinner=False)
def _daily_tokens(stamps):
    return worker_log.daily_tokens([p for p, _ in stamps])


# Tabs

def in_range(t, days):
    """Was the Ticket created in the last `days` days (None: ever)?"""
    if days is None:
        return True
    at = parse_time(t.created_at)
    return bool(at) and at >= datetime.now(timezone.utc) - timedelta(days=days)


def show_tickets(scope, statuses, repos, time_range):
    """A count card's Show: the Tickets tab, filtered to the tickets behind the count."""
    st.session_state.update({'main-tabs': TICKETS_TAB, 'ticket-scope': scope, 'ticket-stages': statuses,
                             'ticket-repos': repos, 'ticket-range': time_range, 'ticket-search': ''})


def open_ticket(tid):
    """Open →: the Tickets tab with this ticket picked, every filter cleared so it is in the list."""
    show_tickets('all', [], [], list(RANGES)[-1])
    st.session_state['open-ticket'] = tid
    st.session_state['open-seq'] = st.session_state.get('open-seq', 0) + 1


def open_button(t, where):
    st.button('Open', key=f'{where}-open-{t.id}', type='tertiary', icon=':material/arrow_forward:',
              on_click=open_ticket, args=(t.id,), help='Its details, worker activity and conversation')


def needs_you(t, proposed, store):
    """Does the open Ticket wait for a person: a question, a review, a failure, a write to approve, a repo to add?"""
    return t.state == 'open' and bool(stage(t).startswith('person/') or proposed.get(t.id)
                                      or store.get(f'repo-request:{t.id}'))


def home(cfg, store, tickets, proposed):
    """Start a request on the left; on the right, what waits for you (answered right here) and what is running."""
    ask, inbox = st.columns([5, 6], gap='large')
    with ask, st.container(border=True):
        st.markdown('#### :material/add_circle: Ask superagent')
        new_request(cfg, store, tickets)
    by_recent = sorted(tickets, key=updated_at, reverse=True)
    needs = [t for t in by_recent if needs_you(t, proposed, store)]
    with inbox:
        st.markdown(f'#### :material/notifications: Needs you ({len(needs)})')
        if not needs:
            st.info('Nothing needs you right now.', icon=':material/check:')
        for t in needs:
            with st.container(border=True):
                head = st.container(horizontal=True, vertical_alignment='center')
                head.markdown(f"{badge(t)} **{t.title}** · {', '.join(t.repos)} · {ago(updated_at(t))}")
                with head:
                    open_button(t, 'home')
                    act(store, t, 'done', 'home')
                a = t.current
                if t.delivery_error:
                    st.error(f'Delivery failed: {t.delivery_error}')
                if a and a.question and stage(t) == 'person/answer':
                    st.warning(a.question, icon=':material/help:')
                elif a and a.summary:
                    st.caption(a.summary)
                if pr_links(t):
                    st.markdown(pr_markdown(t).removeprefix(' · '))
                repo_request(cfg, store, t, 'home')
                proposals_box(cfg, store, t, 'home')
                answer_box(cfg, store, t, 'home', attach=False)
        busy = [t for t in by_recent if t.state == 'open' and t not in needs]
        st.markdown(f'#### :material/autorenew: Working now ({len(busy)})')
        if not busy:
            st.caption('No other open tickets.')
        for t in busy:
            with st.container(border=True):
                head = st.container(horizontal=True, vertical_alignment='center')
                head.markdown(f"{badge(t)} **{t.title}** · {', '.join(t.repos)} · {ago(updated_at(t))}")
                with head:
                    open_button(t, 'busy')
                    act(store, t, 'done', 'busy')
                if t.current and t.current.summary:
                    st.caption(t.current.summary)
    if not tickets:
        st.caption('No tickets yet: ask above, or mention the bot on a GitHub issue.')


def overview(cfg, tickets, proposed=None):
    """The Stats tab: every Ticket at a glance, for a time range and repos: counts that open the Tickets tab, and
    charts. `proposed`: {ticket id: Jira or Confluence writes waiting for approval}."""
    proposed = proposed or {}
    f = st.columns([2, 5])
    time_range = f[0].selectbox('Time range', list(RANGES), index=1, key='ov-range',
                                help='Counts and charts cover the tickets created in this range.')
    repos = f[1].multiselect('Repo', cfg.repo_names, key='ov-repos', placeholder='All repos')
    days = RANGES[time_range]
    in_repo = [t for t in tickets if not repos or set(repos) & set(t.repos)]
    picked = [t for t in in_repo if in_range(t, days)]
    open_tickets = [t for t in picked if t.state == 'open']
    needs = [t for t in open_tickets if stage(t).startswith('person/') or proposed.get(t.id)]
    merged = [t for t in picked if t.state == 'ended' and t.outcome in SUCCEEDED]
    not_merged = [t for t in picked if t.state == 'ended' and t.outcome not in SUCCEEDED]
    statuses = lambda group: sorted({status(t) for t in group})  # noqa: E731
    cards = [('Tickets', picked, 'all', [], 'Every ticket created in the range'),
             ('Open', open_tickets, 'open', [], 'Not ended yet'),
             ('Need you', needs, 'open', statuses(needs),
              'Waiting for your answer or review, blocked, failed, or not delivered'),
             ('Succeeded', merged, 'all', statuses(merged), 'Ended with its pull request merged, or marked done'),
             ('Not merged', not_merged, 'all', statuses(not_merged), 'Ended with its pull request closed, or stopped')]
    for col, (label, group, scope, wanted, tip) in zip(st.columns(len(cards)), cards, strict=True):
        with col.container(border=True):
            st.metric(label, len(group), help=tip)
            st.button('Show', key=f'ov-show-{label}', type='tertiary', icon=':material/arrow_forward:',
                      disabled=not group, on_click=show_tickets, args=(scope, wanted, repos, time_range),
                      help='Open these tickets on the Tickets tab')
    if not picked:
        return

    left, right = st.columns(2)
    with left:
        st.subheader('Tickets by status')
        counts = pd.Series([status(t) for t in picked]).value_counts()
        st.bar_chart(pd.DataFrame({'status': counts.index, 'tickets': counts.values}), x='status', y='tickets',
                     horizontal=True, height=260)
    with right:
        st.subheader('Tickets per repo')
        names = repos or list(dict.fromkeys([*cfg.repo_names, *(r for t in picked for r in t.repos)]))
        per_repo = pd.DataFrame([{'repo': r, 'state': t.state} for t in picked for r in t.repos])
        per_repo = (per_repo.groupby(['repo', 'state']).size().unstack(fill_value=0)
                    .reindex(index=names, columns=['open', 'ended'], fill_value=0))  # a repo without tickets: 0
        st.bar_chart(per_repo, horizontal=True, height=260, color=[OPEN_COLOR, ENDED_COLOR])

    first = min((d for t in in_repo if (d := _day(t.created_at))), default=None)
    span = days or max(7, (datetime.now(timezone.utc).date() - datetime.fromisoformat(first).date()).days + 1
                       if first else 7)
    dates = pd.date_range(end=datetime.now(timezone.utc).date(), periods=span).strftime('%Y-%m-%d')
    daily = pd.DataFrame(0, index=dates, columns=['opened', 'ended', 'tokens'])
    for t in in_repo:
        for column, day in (('opened', _day(t.created_at)),
                            ('ended', _day(ended_at(t)) if t.state == 'ended' else None)):
            if day in daily.index:
                daily.loc[day, column] += 1
        for day, n in _daily_tokens(tuple(_stamps(cfg, t))).items():
            if day in daily.index:
                daily.loc[day, 'tokens'] += n
    left, right = st.columns(2)
    with left:
        st.subheader('Opened and ended per day')
        st.line_chart(daily[['opened', 'ended']], height=260, color=[OPEN_COLOR, ENDED_COLOR])
    with right:
        st.subheader('Worker tokens per day')
        st.bar_chart(daily[['tokens']], height=260)

    st.subheader('Last 24 hours')
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = lambda s: (parse_time(s) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff  # noqa: E731
    running = sum(1 for t in in_repo if t.state == 'open' and t.current and t.current.state == 'running')
    since = cutoff.replace(second=0, microsecond=0)  # by the minute, so the cache serves the 10 s refreshes
    used = [ticket_usage(cfg, t, since) for t in in_repo]
    cols = st.columns(4)
    cols[0].metric('Running workers', f'{running} / {cfg.max_workers}')
    cols[1].metric('Tickets opened (24 h)', sum(1 for t in in_repo if recent(t.created_at)))
    cols[2].metric('Tickets ended (24 h)', sum(1 for t in in_repo if t.state == 'ended' and recent(ended_at(t))))
    cols[3].metric('Worker cost (24 h)', f"${sum(u['cost'] for u in used):.2f}",
                   help=f"{sum(u['tokens'] for u in used):,} tokens; Pi's estimate, or Copilot's AI credits at list price")


def tickets_tab(cfg, store, tickets):
    """Every Ticket in a list on the left, the picked one's details on the right. Home's Open picks one too."""
    c = st.columns([2, 3, 3, 2, 3])
    scope = c[0].radio('Show', ['open', 'all'], horizontal=True, key='ticket-scope')
    repos = c[1].multiselect('Repo', cfg.repo_names, key='ticket-repos', placeholder='All repos')
    statuses = c[2].multiselect('Status', sorted({status(t) for t in tickets}), key='ticket-stages',
                                placeholder='Any status')
    time_range = c[3].selectbox('Created', list(RANGES), index=len(RANGES) - 1, key='ticket-range')
    query = c[4].text_input('Search', key='ticket-search', placeholder='title, ID or summary',
                            icon=':material/search:').strip().lower()
    newest_first = sorted(tickets, key=lambda t: (t.created_at, t.id), reverse=True)  # rowid moves on every save
    shown = [t for t in newest_first if (scope == 'all' or t.state == 'open') and in_range(t, RANGES[time_range])
             and (not repos or set(repos) & set(t.repos)) and (not statuses or status(t) in statuses)
             and (not query or query in ' '.join([t.id, t.name, t.title, *(a['summary'] for a in t.assignments)])
                  .lower())]
    if not shown:
        st.info('No tickets match.' if scope == 'all' else 'No open tickets match. Choose Show: all for ended ones.')
        return
    rows = [{'status': status(t), 'title': t.title, 'repo': ', '.join(t.repos), 'updated': ago(updated_at(t)),
             'pr': pr_url(t), 'ID': t.name, 'branch': t.branch or '-', 'issue': issue_url(t), **usage_cells(cfg, t)}
            for t in shown]
    # The key names the rows shown: a new ticket or another filter starts a fresh selection, so a remembered row
    # number never points at a different ticket. Changes within a row (status, tokens) keep it. Each Open → (open-seq)
    # starts a fresh one too, so its ticket is picked even when the same one was picked and then unpicked before.
    rows_key = zlib.crc32(' '.join(t.id for t in shown).encode())
    ids = [t.id for t in shown]
    opened = st.session_state.get('open-ticket')
    listing, detail = st.columns([2, 3], gap='medium')
    with listing:
        event = st.dataframe(
            pd.DataFrame(rows), hide_index=True, width='stretch', placeholder='-', alt='Tickets',
            key=f"tickets-table-{rows_key}-{st.session_state.get('open-seq', 0)}", on_select='rerun',
            selection_mode='single-row', column_order=['status', 'title', 'repo', 'updated', 'pr'],
            selection_default={'selection': {'rows': [ids.index(opened)] if opened in ids else []}},
            column_config={'title': st.column_config.TextColumn('title', width='large'),
                           'repo': st.column_config.TextColumn('repo', width='small'),
                           'pr': st.column_config.LinkColumn('PR', display_text=r'pull/(\d+)$', width='small')})
        st.caption('Click a ticket to see it on the right.')
    picked = event.selection.rows
    with detail:
        if not picked or picked[0] >= len(shown):
            st.info('Pick a ticket on the left to see its details, answer it, retry it or stop it.',
                    icon=':material/arrow_back:')
            return
        ticket_detail(cfg, store, shown[picked[0]])


def repo_request(cfg, store, t, where='tk'):
    """The repo the worker asked to add (superagent-report --needs-repo), with Add: the same as `superagent add-repo`.
    `where`: a widget key prefix, as the same ticket can show on Home and in its details."""
    adding = store.get(f'operator:add-repo:{t.id}')
    if adding:
        st.info(f'Adding **{adding}** to this ticket: the service adds it after the current run and resumes the '
                'worker.', icon=':material/hourglass_top:')
        return
    wanted = store.get(f'repo-request:{t.id}')
    if not wanted:
        return
    with st.container(border=True):
        st.markdown(f':orange-badge[:material/add_link: Repo requested] The worker asks to add **{wanted}**, to change '
                    f"it together with {', '.join(t.repos)} in this ticket.")
        if st.button(f'Add {wanted} to this ticket', key=f'{where}-add-repo-{t.id}', type='primary'):
            try:
                st.success(cli.add_repo_request(cfg, store, t.id, wanted))
            except RuntimeError as e:
                st.error(str(e))
        st.caption('Or answer below to tell it otherwise.')


def proposals_box(cfg, store, t, where='tk'):
    """The Jira and Confluence writes the worker proposed (superagent-report propose), each with a preview and
    Approve / Reject: the same as `superagent approve|reject`. Nothing is written without Approve. `where`: a widget
    key prefix; the preview starts open only in the ticket's details."""
    from superagent import atlassian
    for p in store.proposals(ticket_id=t.id):
        if p['state'] in ('rejected',) and p['id'] != st.session_state.get('just-decided'):
            continue
        with st.container(border=True):
            badge = {'pending': ':orange-badge[:material/edit_note: Proposed]',
                     'approved': ':blue-badge[:material/hourglass_top: Approved, writing next round]',
                     'writing': ':blue-badge[:material/sync: Writing]', 'done': ':green-badge[:material/check: Written]',
                     'failed': ':red-badge[:material/error: Failed]', 'rejected': ':gray-badge[Rejected]'}[p['state']]
            st.markdown(f"{badge} **#{p['id']}** {atlassian.target(p)}"
                        + (f" · [{p['result']}]({p['result']})" if p['result'] else ''))
            if p['error'] and p['state'] == 'failed':
                st.error(p['error'])
            with st.expander('What it would write', expanded=p['state'] == 'pending' and where == 'tk'):
                st.markdown(p['body'])
            if p['state'] == 'pending' and p['kind'] == 'confluence-update' and p.get('mode') == 'replace':
                st.warning("Approving replaces the page's whole current text with this. Confluence keeps the old "
                           'version in its page history.', icon=':material/warning:')
            if p['state'] != 'pending':
                continue
            row = st.container(horizontal=True)
            approve = row.button('Approve and write', key=f"{where}-approve-{p['id']}", type='primary')
            reject = row.button('Reject', key=f"{where}-reject-{p['id']}")
            if approve or reject:
                try:
                    st.success(cli.decide_proposal(cfg, store, p['id'], approve))
                    st.session_state['just-decided'] = p['id']
                except RuntimeError as e:
                    st.error(str(e))


def ticket_detail(cfg, store, t):
    """One Ticket: what it is and what it needs first, then its worker activity, conversation, runs and messages."""
    with st.container(border=True):
        st.markdown(f'### {t.title}')
        m = t.mandate
        st.markdown(f"{badge(t)} · {', '.join(t.repos)} · branch `{t.branch or '-'}` · {thread_markdown(t)}"
                    + pr_markdown(t))
        st.caption(f"ID `{t.name}` · asked by {m.get('actor', '?')} at {m.get('at') or t.created_at}"
                   f" · {'ticket folder' if t.parts else 'worktree'} `{t.worktree or '-'}`"
                   + (f' · outcome {t.outcome}' if t.outcome else ''))
        for p in t.parts:
            st.markdown(f"- {p['repo']}: worktree `{p['worktree'] or '-'}`"
                        + (f" · [PR #{p['pr_number']}](https://github.com/{p['repo']}/pull/{p['pr_number']})"
                           if p['pr_number'] else ' · no PR')
                        + (f" · {p['test_result']}" if p['test_result'] else ''))
        a = t.current
        if t.delivery_error:
            st.error(f'Delivery failed: {t.delivery_error}')
        if a and a.question and stage(t) == 'person/answer':
            st.warning(f'Question: {a.question}')
        if t.state == 'open':
            repo_request(cfg, store, t)
        proposals_box(cfg, store, t)
        if a and a.summary:
            st.markdown(f"Latest report ({a.report or '-'}): {a.summary}")
        answer_box(cfg, store, t, 'tk')
        actions(store, t, 'tk')
        local = cli.is_local(t)
        activity, talk, runs, messages = st.tabs(['Worker activity', 'Conversation' if local else 'Request', 'Runs',
                                                  'Messages to worker'])
        with activity:
            worker_activity(cfg, t)
        with talk:
            request = store.trigger(m.get('trigger_id'))
            if local:
                conversation(cfg, store, t)
            elif request:
                st.text(request.text)
            else:
                st.info('The request is no longer stored.')
        with runs:
            st.caption('Each time a worker was started for this ticket, and how it ended.')
            st.dataframe(pd.DataFrame([{k: r[k] for k in RUN_COLUMNS} for r in t.assignments], columns=RUN_COLUMNS),
                         hide_index=True, width='stretch')
        with messages:
            inputs = store.inputs(t.id)
            waiting = sum(1 for i in inputs if not i['delivered'])
            st.caption(f'Answers and instructions for the worker: {len(inputs)} ({waiting} not yet delivered).')
            if inputs:
                st.dataframe(pd.DataFrame([{'at': i['at'], 'kind': i['kind'], 'text': i['text'],
                                            'delivered': 'yes' if i['delivered'] else 'no'} for i in inputs]),
                             hide_index=True, width='stretch')


def service_health(cfg, store):
    """Is the service polling, and is anything failing."""
    status_now = cli.status_data(cfg, store)
    st.markdown(f"Last poll: {status_now['last_poll'] or 'never (is `superagent run` running?)'}")
    if status_now['failing_effects']:
        st.markdown('Failing effects (retried automatically):')
        st.dataframe(pd.DataFrame(status_now['failing_effects'])[['kind', 'key', 'attempts', 'error']],
                     hide_index=True, width='stretch')
    else:
        st.markdown('Failing effects: none.')


def incoming_messages(store):
    """What the service did with each GitHub message it read: the place to look when the agent did not answer."""
    st.caption('Every GitHub issue and comment the service read, and what it did with it. Ignored: not addressed to '
               'the agent, or not from someone in `allowed`.')
    decisions = st.multiselect('Show', DECISIONS, key='trigger-decisions', format_func=lambda d: DECISION_NAMES[d],
                               placeholder='Everything')
    trigs = [(trig, d, r) for trig, d, r in store.recent_triggers(RECENT_TRIGGERS) if not decisions or d in decisions]
    if not trigs:
        st.info('No messages yet.' if not decisions else 'No messages match.')
        return
    rows = [{'at': trig.at, 'what it did': DECISION_NAMES.get(d, d), 'why': r or '(recorded before reasons were kept)',
             'from': trig.actor, 'text': trig.text[:300], 'url': trig.url or None} for trig, d, r in trigs]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch',
                 column_config={'url': st.column_config.LinkColumn('link')})


def settings_tab(cfg, store, tickets):
    """The config, one sub-tab per part, and the service's health and incoming messages."""
    if notice := st.session_state.get('settings-notice'):  # kept: the 10 s refresh would wipe a one-off message
        getattr(st, notice[0])(notice[1])
    repos, worker, everything, health, incoming = st.tabs(['Repos', 'Worker and service', 'All settings',
                                                           'Service health', 'Incoming messages'])
    saved = (f'Changes are saved into `{config_path()}`, keeping its comments. '
             'Restart `superagent run` to apply them.')
    with repos:
        st.caption(saved)
        for r in cfg.repos:
            repo_settings(cfg, tickets, r)
        add_repo(cfg)
    with worker:
        st.caption(saved)
        edit_settings(cfg)
    with everything:
        st.caption('The config as the panel loaded it. Read-only here: change the rest in the config file.')
        rows = [('config file', config_path()), ('repos', ', '.join(cfg.repo_names)),
                ('agent_login', cfg.agent_login), ('allowed', ', '.join(cfg.allowed)), ('agent', cfg.agent),
                ('provider', cfg.provider), ('model', cfg.model), ('thinking', cfg.thinking), ('tools', cfg.tools),
                ('max_workers', cfg.max_workers), ('poll_seconds', cfg.poll_seconds),
                ('max_test_retries', cfg.max_test_retries), ('local_user', cfg.local_user),
                ('work_dir', cfg.work_dir), ('state_dir', cfg.state_dir),
                (f'token set ({cfg.token_env})', 'yes' if cfg.token else 'no')]
        st.dataframe(pd.DataFrame([{'setting': k, 'value': str(v)} for k, v in rows]), hide_index=True,
                     width='stretch', height='content')  # every row, no inner scrolling
    with health:
        service_health(cfg, store)
    with incoming:
        incoming_messages(store)


def settings_done(kind, text):
    """Show `text` at the top of Settings until the next change, and reload the panel with the config file."""
    st.session_state['settings-notice'] = (kind, text)
    st.rerun(scope='app')


def check_repo(name, checkout):
    """A repo name as owner/name, and its checkout folder: empty (superagent makes its own) or an existing one."""
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', name):
        raise ValueError('give the repo as owner/name, as in its GitHub URL')
    if checkout and not os.path.isdir(checkout):
        raise ValueError(f'checkout folder {checkout} does not exist: clone the repo there, or leave it empty')


def repo_settings(cfg, tickets, r):
    """One repo's card: every field editable, plus Remove. A repo with Tickets keeps its name: they refer to it."""
    used = sum(r.name.lower() in (x.lower() for x in t.repos) for t in tickets)
    with st.container(border=True):
        st.markdown(f'**{r.name}** · `{r.checkout}`' + (f' · {used} ticket(s)' if used else ''))
        with st.form(f'repo-form-{r.slug}', border=False):
            c = st.columns([2, 3])
            locked = r.legacy or used
            name = c[0].text_input('Repo (owner/name)', r.name, key=f'set-{r.slug}-name', disabled=bool(locked),
                                   help='Its tickets refer to it by this name.' if used else
                                   'Add a second repo to rename this one.' if r.legacy else
                                   'As in its GitHub URL: github.com/owner/name.')
            checkout = c[1].text_input('Checkout folder', r.checkout, key=f'set-{r.slug}-checkout',
                                       help='A local clone with an origin remote, or the copy superagent keeps '
                                            'itself; worktrees are made from it.')
            c = st.columns([1, 3, 1])
            fields = {'name': name, 'checkout': checkout,
                      'base_branch': c[0].text_input('Base branch', r.base_branch, key=f'set-{r.slug}-base',
                                                     help='New branches start from it; PRs target it.')}
            if not r.legacy:  # the single-repo form has no test command
                fields['test_command'] = c[1].text_input('Test command', r.test_command, key=f'set-{r.slug}-test',
                                                         placeholder='none',
                                                         help='Run with `sh -c` in the worktree before any pull '
                                                              'request; empty: none.')
                fields['test_timeout'] = c[2].number_input('Test timeout (s)', 1, 86400, r.test_timeout,
                                                           key=f'set-{r.slug}-timeout')
            saved = st.form_submit_button('Save', type='primary')
        if not r.legacy and len(cfg.repos) > 1:
            with st.popover('Remove', disabled=bool(used),
                            help='It has tickets, which refer to it.' if used else None):
                st.write(f'Remove {r.name} from the config? Its checkout folder is not touched.')
                if st.button(f'Yes, remove {r.name}', key=f'remove-{r.slug}'):
                    try:
                        config.remove_repo(config_path(), r.name)
                    except (OSError, ValueError) as e:
                        settings_done('error', f'Not removed: {e}')
                    settings_done('success', f'Removed {r.name}. Restart `superagent run` to apply it.')
    if not saved:
        return
    fields = {k: v.strip() if isinstance(v, str) else int(v) for k, v in fields.items()}
    fields['checkout'] = os.path.expanduser(str(fields['checkout']))
    diff = {k: v for k, v in fields.items() if v != getattr(r, k)}
    if not diff:
        settings_done('info', f'{r.name}: nothing changed.')
    try:
        check_repo(fields['name'] if 'name' in diff else 'o/k', fields['checkout'] if 'checkout' in diff else '')
        if r.legacy:  # the single-repo form keeps checkout and base_branch in [executor]
            config.update(config_path(), diff)
        else:
            config.update(config_path(), repos={r.name: diff})
    except (OSError, ValueError, TypeError) as e:
        settings_done('error', f'{r.name} not saved: {e}')
    settings_done('success', f"Saved {', '.join(diff)} of {fields['name']}. Restart `superagent run` to apply them.")


def add_repo(cfg):
    """Add a repository to the config file; the service needs a restart."""
    with st.expander('Add a repo'):
        if cfg.legacy:
            st.caption(f'Your config names one repo (`repo` + `checkout`). Adding one moves {cfg.repo} into a '
                       '[[repos]] table too; its tickets, history and GitHub read positions carry over.')
        st.caption('The bot token must reach the new repo too.')
        with st.form('add-repo-form', border=False):
            c = st.columns([2, 3])
            name = c[0].text_input('Repo (owner/name)', key='add-repo-name', placeholder='owner/name')
            checkout = c[1].text_input('Checkout folder', key='add-repo-checkout',
                                       placeholder='empty: superagent keeps its own copy',
                                       help='Leave it empty and superagent clones the repo itself on first use. Or '
                                            'give a local clone with an origin remote.')
            c = st.columns([1, 3, 1])
            base = c[0].text_input('Base branch', key='add-repo-base', placeholder=cfg.base_branch)
            test = c[1].text_input('Test command', key='add-repo-test', placeholder='none',
                                   help='Run with `sh -c` in the worktree before any pull request.')
            timeout = c[2].number_input('Test timeout (s)', 1, 86400, 900, key='add-repo-timeout')
            submitted = st.form_submit_button('Add repo')
    if not submitted:
        return
    name, checkout = name.strip(), os.path.expanduser(checkout.strip()) if checkout.strip() else ''
    try:
        check_repo(name, checkout)
        config.add_repo(config_path(), name, checkout, base, test, 0 if timeout == 900 else int(timeout))
    except (OSError, ValueError, TypeError) as e:
        settings_done('error', f'Not added: {e}')
    settings_done('success', f'Added {name}. Restart `superagent run` to start working in it.')


def edit_settings(cfg):
    """Change the worker and service settings in the config file; the running service applies them after a restart."""
    with st.form('settings-form', border=True):
        agents = list(config.AGENTS)
        values = {'agent': st.radio('Agent', agents, index=agents.index(cfg.agent), horizontal=True, key='set-agent',
                                    format_func=lambda a: AGENT_NAMES[a],
                                    help='What runs each worker. Pi uses the provider and model below; Copilot uses '
                                         'your `copilot login` and the model below as Copilot names it '
                                         '(for example claude-sonnet-5.5).')}
        c = st.columns(3)
        values |= {'provider': c[0].text_input('Provider', cfg.provider, key='set-provider',
                                               help='Pi only: Copilot always uses GitHub Copilot.'),
                  'model': model_input(cfg, c[1]),
                  'thinking': c[2].selectbox('Thinking level', THINKING_LEVELS, key='set-thinking',
                                             index=THINKING_LEVELS.index(cfg.thinking)
                                             if cfg.thinking in THINKING_LEVELS else 0),
                  'max_workers': c[0].number_input('Max workers', 1, 16, cfg.max_workers, key='set-max-workers'),
                  'max_test_retries': c[1].number_input('Test fix attempts', 0, 20, cfg.max_test_retries,
                                                        key='set-max-test-retries'),
                  'poll_seconds': c[2].number_input('Poll every (s)', 60, 3600, cfg.poll_seconds,
                                                    key='set-poll-seconds')}
        submitted = st.form_submit_button('Save settings')
    if not submitted:
        return
    values = {k: v.strip() if isinstance(v, str) else int(v) for k, v in values.items()}
    changed = {k: v for k, v in values.items() if v != getattr(cfg, k)}
    if not changed:
        settings_done('info', 'Nothing changed.')
    try:
        config.update(config_path(), changed)
    except (OSError, ValueError, TypeError) as e:
        settings_done('error', f'Not saved: {e}')
    settings_done('success', f"Saved {', '.join(changed)}. Restart `superagent run` to apply them.")

# App

@st.cache_resource(on_release=lambda store: store.close())  # an old one, cleared or replaced, is closed
def _cached_store(db_path, class_id, _store_class):
    return _store_class(db_path)


def open_store(db_path):
    from superagent.store import Store
    # keyed on the class: Streamlit reloads an edited store.py, and an old instance would keep the old methods
    return _cached_store(db_path, id(Store), Store)


def service_warning(cfg, store):
    """Say so on every tab when `superagent run` is not polling: the panel only queues requests, it runs nothing."""
    if cli.service_running(cfg, store):
        return
    last = store.get('last-poll')
    waiting = len(store.local_requests())
    st.warning(f"The service is not running: {'its last poll was ' + last if last else 'it has never polled'}. "
               'Start `superagent run` in a terminal and leave it open.'
               + (f' {waiting} request(s) are waiting for it.' if waiting else ''), icon=':material/warning:')


@st.fragment(run_every=REFRESH_SECONDS)
def body(cfg):
    if not os.path.exists(cfg.db_path):
        st.info(f'No database yet at `{cfg.db_path}`. Run `superagent run` first.')
        return
    store = open_store(cfg.db_path)
    tickets = store.tickets()
    head = st.container(horizontal=True, vertical_alignment='center', gap='small')
    head.markdown('### :material/smart_toy: superagent')
    up = cli.service_running(cfg, store)
    head.markdown(f"{':green-badge[:material/check_circle: Service running]' if up else ':red-badge[:material/error: Service stopped]'}"
                  f" · {len(cfg.repo_names)} repo{'s' if len(cfg.repo_names) != 1 else ''} as @{cfg.agent_login}"
                  f" · updated {datetime.now().strftime('%H:%M:%S')}",
                  help='Repos: ' + ', '.join(cfg.repo_names) + f'. The page refreshes every {REFRESH_SECONDS} s.')
    head.button('Refresh', key='refresh', type='tertiary', icon=':material/refresh:')  # a click reruns this fragment
    service_warning(cfg, store)
    # Keyed and tracked, so a Stats card's Show or a Home card's Open can switch to the Tickets tab.
    home_tab, tickets_tab_, stats_tab, settings = st.tabs(MAIN_TABS, key='main-tabs', on_change='rerun')
    proposed = Counter(p['ticket_id'] for p in store.proposals(state='pending'))
    with home_tab:
        home(cfg, store, tickets, proposed)
    with tickets_tab_:
        if tickets:
            tickets_tab(cfg, store, tickets)
        else:
            st.info('No tickets yet: ask on the Home tab, or mention the bot on a GitHub issue.')
    with stats_tab:
        overview(cfg, tickets, proposed)
    with settings:
        settings_tab(cfg, store, tickets)


def main():
    st.set_page_config(page_title='superagent', page_icon=':material/smart_toy:', layout='wide')
    st.html(TAB_STYLE)
    path = config_path()
    if not path:
        st.error('No config: set SUPERAGENT_CONFIG or start the panel with `superagent panel`.')
        return
    try:
        cfg = cli.load_config(path)
    except cli.ConfigError as e:
        st.error(str(e))
        return
    body(cfg)


main()
