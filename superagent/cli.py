"""The `superagent` command: run the service, inspect it, and act on Tickets.

Exit codes: 0 ok, 1 runtime error (or a failed check), 2 configuration error (including an unset token).
Operator `stop` and `retry` write a request to the store (kv `operator:<action>:<ticket>`); the running service
applies it at its next poll round, or at its next start when it is not running. Local requests (`ask`, `answer`)
are queued in the store the same way. `pi-install` installs the Pi skill and `/superagent` prompt template.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config as config_mod
from .model import THINKING_LEVELS, named_repos, stage

DEFAULT_CONFIG = '~/.config/pi-superagent/config.toml'

log = logging.getLogger('superagent')


class ConfigError(Exception):
    pass


def load_config(path):
    try:
        return config_mod.load(path)
    except FileNotFoundError:
        raise ConfigError(f'config file not found: {path} (copy config.example.toml there)') from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'config file {path} is not valid TOML: {e}') from None
    except TypeError as e:
        raise ConfigError(f'config file {path} has an unknown or missing field: {e}') from None
    except ValueError as e:
        raise ConfigError(f'config file {path}: {e}') from None


_opened = []  # stores opened by the running command, closed when it returns


def open_store(cfg):
    from .store import Store
    store = Store(cfg.db_path)
    _opened.append(store)
    return store


def find_ticket(store, ref):
    tickets = store.tickets()
    exact = [t for t in tickets if t.id == ref]
    by_name = [t for t in tickets if t.name == ref]
    open_named = [t for t in by_name if t.state == 'open']
    found = exact or open_named or by_name or [t for t in tickets if t.thread_key == ref]  # or `local:<id>`
    return found[-1] if found else None


def is_local(t):
    return t.thread_key.startswith('local:')


def pr_urls(t):
    """Every pull request link of the Ticket: one per repo of a cross-repo Ticket."""
    if t.parts:
        return [f"https://github.com/{p['repo']}/pull/{p['pr_number']}" for p in t.parts if p['pr_number']]
    return [f'https://github.com/{t.repo}/pull/{t.pr_number}'] if t.pr_number else []


# Commands

def cmd_run(cfg, args):
    if not cfg.token:
        raise ConfigError(f'environment variable {cfg.token_env} is not set (the bot account\'s GitHub token)')
    from .github import GitHub
    from .report import ReportServer
    from .service import Service

    store = open_store(cfg)
    gh = GitHub(cfg.token, cfg.repos[0].name)  # one token for every repo; the service picks the repo per call

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        holder = {}
        try:
            server = ReportServer(lambda tok: holder['svc'].lookup(tok),
                                  lambda tid, r: holder['svc'].on_report(tid, r), cfg.report_port)
        except OSError as e:
            raise RuntimeError(f'cannot listen on 127.0.0.1:{cfg.report_port}: {e.strerror or e}') from None
        svc = holder['svc'] = Service(cfg, store, gh, report_url=server.url)
        log.info('superagent watching %s as %s; reports on %s', ', '.join(cfg.repo_names), cfg.agent_login,
                 server.url)
        try:
            await svc.run_forever(stop)
        finally:
            server.close()
        log.info('superagent stopped')

    asyncio.run(main())
    return 0


def status_data(cfg, store):
    """What `superagent status` prints; the web panel shows the same facts."""
    stages = Counter(stage(t) for t in store.tickets(state='open'))
    failing = [{'kind': e['kind'], 'key': e['key'], 'attempts': e['attempts'], 'error': e['last_error']}
               for e in store.failing_effects()]
    # The single-repo form keeps its original output; [[repos]] lists every repo.
    where = {'repo': cfg.repo} if cfg.legacy else {'repos': cfg.repo_names}
    return {**where, 'agent_login': cfg.agent_login, 'token': 'yes' if cfg.token else 'no',
            'open_tickets': dict(stages), 'failing_effects': failing, 'last_poll': store.get('last-poll')}


def cmd_status(cfg, args):
    print(json.dumps(status_data(cfg, open_store(cfg)), indent=2))
    return 0


def _row(t, cfg=None):
    """One Ticket as `tickets` shows it; with [[repos]] (cfg not in the single-repo form) it names the repo, or every
    repo of a cross-repo Ticket."""
    a = t.current
    row = {'id': t.id, 'name': t.name, 'thread': t.thread_key}
    if cfg is not None and not cfg.legacy:
        row['repo'] = ', '.join(t.repos)
    # A cross-repo Ticket has one PR per changed repo: `owner/name#<n>, ...` (None until delivered).
    pr = (', '.join(f"{p['repo']}#{p['pr_number']}" for p in t.parts if p['pr_number']) or None) if t.parts \
        else t.pr_number
    return {**row, 'stage': stage(t), 'number': t.number, 'pr': pr,
            'state': t.state, 'outcome': t.outcome, 'last_report': a.report if a else None,
            'summary': a.summary if a else ''}


def cmd_tickets(cfg, args):
    store = open_store(cfg)
    rows = [_row(t, cfg) for t in store.tickets(state=None if args.all else 'open')]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print('no tickets' if args.all else 'no open tickets (use --all to include ended ones)')
        return 0
    cols = [('id', 'ID'), ('name', 'NAME'), *([] if cfg.legacy else [('repo', 'REPO')]), ('stage', 'STAGE'),
            ('number', '#'), ('pr', 'PR'), ('last_report', 'LAST REPORT')]
    cells = [['local' if k == 'number' and r['thread'].startswith('local:') else str(r[k] if r[k] is not None else '-')
              for k, _ in cols] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(cols)]
    for line in [[h for _, h in cols], *cells]:
        print('  '.join(c.ljust(w) for c, w in zip(line, widths)).rstrip())
    return 0


def request_action(store, ref, action):
    """Record an operator `stop` or `retry` request for the service and return the confirmation line.

    Shared by the CLI and the web panel; raises RuntimeError when the Ticket is unknown or already ended.
    """
    t = find_ticket(store, ref)
    if not t:
        raise RuntimeError(f'no ticket {ref!r} (see `superagent tickets --all`)')
    if t.state == 'ended':
        raise RuntimeError(f'ticket {t.name} already ended ({t.outcome})')
    from .model import now_iso
    store.put(f'operator:{action}:{t.id}', now_iso())
    return f'{action} requested for {t.name} ({t.id}); the running service applies it within one poll round'


def _request(action):
    def cmd(cfg, args):
        print(request_action(open_store(cfg), args.ticket, action))
        return 0
    return cmd


# Local requests (S15): whoever can run this CLI on this machine is the operator, acting as config `local_user`.

def service_running(cfg, store):
    """Whether `superagent run` looks alive: it polled within about two poll rounds."""
    try:
        last = datetime.fromisoformat(store.get('last-poll') or '')
    except ValueError:
        return False
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last <= timedelta(seconds=2 * cfg.poll_seconds + 30)


def _ask_repos(cfg, repos, text):
    """The repos of a local request: each --repo, then the configured repos the text names (S14); with neither, the
    only configured repo."""
    chosen = []
    for r in repos or ():
        try:
            name = cfg.repo_config(r).name
        except ValueError:
            raise RuntimeError(f'repo {r} is not configured (configured: {", ".join(cfg.repo_names)})') from None
        if name not in chosen:
            chosen.append(name)
    chosen += [r for r in named_repos(text, cfg.repo_names) if r not in chosen]
    if not chosen:
        if len(cfg.repo_names) > 1:
            raise RuntimeError(f'several repos are configured; name one with --repo: {", ".join(cfg.repo_names)}')
        chosen = list(cfg.repo_names)
    return chosen


BRANCH_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._/-]*')


def _valid_branch(name):
    return bool(BRANCH_NAME.fullmatch(name)) and '..' not in name and '//' not in name and not (
        name.endswith(('/', '.lock', '.')) or name == 'HEAD' or name.startswith('refs/'))


def _checkout_git(cfg, repo, *args, timeout=None):
    # The service's own git environment: no token, no secrets, no global config, hooks off.
    from .executor import git_env
    from .github import GIT_HARDEN
    try:
        p = subprocess.run(['git', *GIT_HARDEN, *args], cwd=cfg.repo_config(repo).checkout, env=git_env(cfg),
                           capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f'git {args[0]} failed in the checkout of {repo}: {e}') from e
    if p.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed in the checkout of {repo}: {(p.stderr or p.stdout).strip()}')
    return p.stdout


def list_branches(cfg, repo):
    """The branches a request on `repo` can work on, read from its checkout: [(name, where)] with where 'GitHub',
    'local' or 'GitHub and local'. GitHub branches are as of the checkout's last fetch (see fetch_branches). The
    base branch and the workers' own local agent/* branches are left out."""
    places = _branch_places(cfg, repo)
    base = cfg.repo_config(repo).base_branch.lower()
    names = [n for n, where in places.items() if _valid_branch(n) and n.lower() != base
             and not (where == ['local'] and n.startswith('agent/'))]
    return [(n, ' and '.join(sorted(places[n]))) for n in sorted(names, key=str.lower)]


def _branch_places(cfg, repo):
    """Every branch the checkout of `repo` knows: {name: ['GitHub' and/or 'local']}."""
    places = {}
    for ref in _checkout_git(cfg, repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin',
                             'refs/heads').split():
        for prefix, place in (('refs/remotes/origin/', 'GitHub'), ('refs/heads/', 'local')):
            if ref.startswith(prefix):
                places.setdefault(ref[len(prefix):], []).append(place)
    return places


def fetch_branches(cfg, repo):
    """Bring the checkout's copy of the GitHub branches of `repo` up to date (git fetch --prune origin)."""
    from .executor import fetch_origin
    try:
        error = fetch_origin(cfg, cfg.repo_config(repo), '*', timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        error = str(e)
    if error:
        raise RuntimeError(f'git fetch failed in the checkout of {repo}: {error}')
    return f'fetched the branches of {repo} from GitHub'


def _existing_branch(cfg, branch, repos):
    """Check an existing branch named for a request; returns it stripped ('' for a new branch)."""
    branch = (branch or '').strip()
    if not branch:
        return ''
    if len(repos) != 1:
        raise RuntimeError('an existing branch works with one repo; name one with --repo')
    if not _valid_branch(branch):
        raise RuntimeError(f'{branch!r} is not a valid branch name')
    base = cfg.repo_config(repos[0]).base_branch
    if branch.lower() == base.lower():
        raise RuntimeError(f'{branch} is the base branch of {repos[0]}; name a feature branch, or leave it out for a '
                           'new branch')
    return branch


def _new_branch(cfg, store, name, repos):
    """Check the name chosen for a request's new branch; returns it stripped ('' for the default agent/<name>).
    The service creates it and force-pushes it, so it must not be a branch that exists already."""
    name = (name or '').strip()
    if not name:
        return ''
    if not _valid_branch(name):
        raise RuntimeError(f'{name!r} is not a valid branch name')
    for repo in repos:
        if name.lower() == cfg.repo_config(repo).base_branch.lower():
            raise RuntimeError(f'{name} is the base branch of {repo}; name a new branch')
        if name.lower() in {b.lower() for b in _branch_places(cfg, repo)}:
            raise RuntimeError(f'{name} already exists in {repo}; to work on it choose it as an existing branch '
                               '(--branch), or name a new one')
    busy = next((t for t in store.tickets(state='open') if t.branch.lower() == name.lower()), None)
    if busy:
        raise RuntimeError(f'ticket {busy.name} already works on {busy.branch}; name another branch')
    return name


def _skill_paths(skills):
    """The chosen skills as absolute paths, each a folder with a SKILL.md or a .md file, without repeats."""
    paths = []
    for s in skills or ():
        path = os.path.abspath(os.path.expanduser(s.strip()))
        if not (os.path.isfile(os.path.join(path, 'SKILL.md')) or (os.path.isfile(path) and path.endswith('.md'))):
            raise RuntimeError(f'no skill at {path}: a skill is a folder with a SKILL.md, or a .md file')
        if path not in paths:
            paths.append(path)
    return paths


def pi_agent_dir():
    return os.path.abspath(os.path.expanduser(os.environ.get('PI_CODING_AGENT_DIR') or '~/.pi/agent'))


REPO_SKILL_DIRS = ('.agents/skills', '.pi/skills', '.claude/skills')


def list_skills(cfg, repos=()):
    """The skills a request can give its worker: [(path, name, where)], sorted by name. From the user's Pi skills
    ('Pi'), ~/.agents/skills ('~/.agents') and each of `repos`' checkout (.agents, .pi and .claude skills, where =
    the repo). A skill is a folder with a SKILL.md; superagent's own operator skill (pi-install) is left out."""
    roots = [(os.path.join(pi_agent_dir(), 'skills'), 'Pi'),
             (os.path.join(os.path.expanduser('~'), '.agents', 'skills'), '~/.agents')]
    for repo in repos:
        checkout = cfg.repo_config(repo).checkout
        roots += [(os.path.join(checkout, *d.split('/')), cfg.repo_config(repo).name) for d in REPO_SKILL_DIRS]
    found = []
    for root, where in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            skill = os.path.join(root, name, 'SKILL.md')
            try:
                with open(skill, encoding='utf-8', errors='replace') as f:
                    if PI_MARKER in f.read():
                        continue  # the operator's skill: it drives superagent, not a worker's task
            except OSError:
                continue
            found.append((os.path.join(root, name), name, where))
    return sorted(found, key=lambda s: (s[1].lower(), s[2]))


def list_models(cfg, run=subprocess.run):
    """The models Pi can run, as 'provider/model', from `pi --list-models`; [] when Pi cannot list them, and for a
    Copilot worker (typed by name; `copilot` lists its models only inside a session)."""
    if cfg.agent == 'copilot':
        return []
    try:
        p = run([cfg.pi, '--list-models'], capture_output=True, text=True, timeout=60,
                env={**os.environ, 'PI_SKIP_VERSION_CHECK': '1'})
    except (OSError, subprocess.SubprocessError):
        return []
    models, header = [], False
    for line in (p.stdout or '').splitlines():
        cols = line.split()
        if cols[:2] == ['provider', 'model']:
            header = True
        elif header and len(cols) >= 2:
            models.append(f'{cols[0]}/{cols[1]}')
    return models


def copilot_models(cfg):
    """[(model, note)] the GitHub Copilot CLI offers this user, asked the way a worker runs it (its own COPILOT_HOME,
    no GitHub MCP server); no prompt is sent, so no premium request is used. [] when Copilot cannot be asked."""
    from .copilot_acp import offered_models
    from .worker_log import copilot_home
    argv = [shutil.which(cfg.copilot) or cfg.copilot, '--acp', '--stdio', '--disable-builtin-mcps',
            '--no-custom-instructions', '--no-auto-update']
    env = {**cfg.worker_env(), 'COPILOT_HOME': copilot_home(cfg.state_dir), 'COPILOT_PLUGIN_DIR_ONLY': 'true',
           'COPILOT_AUTO_UPDATE': 'false'}
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)  # the session's working folder
    except OSError:
        return []
    return offered_models(argv, cfg.state_dir, env)


def ask_request(cfg, store, text, repos=(), branch='', title='', new_branch='', skills=(), model='', thinking=''):
    """Queue a local request for the running service. Returns (request id, the lines to print).

    `branch`: an existing branch to work on instead of a new agent/<name> branch (one repo only): on GitHub, or only
    in the repo's checkout, which the first delivery then publishes. `new_branch`: the name of the new branch
    instead of agent/<name>; it must not exist yet. `title`: the Ticket's title, which its name is made from
    (default: the first line of `text`). `skills`: skill folders or files the worker gets besides its own.
    `model` ('provider/model', or a model of the config's provider) and `thinking`: the worker's ('' = the config's).
    Shared by `superagent ask` and the web panel; raises RuntimeError for an empty text, an unknown repo, a bad
    branch or a missing skill."""
    text = (text or '').strip()
    if not text:
        raise RuntimeError('the request is empty: say what to do')
    if (branch or '').strip() and (new_branch or '').strip():
        raise RuntimeError('choose an existing branch or a new branch name, not both')
    chosen = _ask_repos(cfg, repos, text)
    branch = _existing_branch(cfg, branch, chosen)
    new_branch = _new_branch(cfg, store, new_branch, chosen)
    model, thinking = (model or '').strip(), (thinking or '').strip().lower()
    if model and not re.fullmatch(r'[\w.:@+-]+(/[\w.:@+/-]+)?', model):
        raise RuntimeError(f'{model!r} is not a model: use provider/model, as `pi --list-models` shows')
    if thinking and thinking not in THINKING_LEVELS:
        raise RuntimeError(f'thinking must be one of {", ".join(THINKING_LEVELS)}')
    options = {'title': ' '.join((title or '').split()), 'new_branch': new_branch, 'skills': _skill_paths(skills),
               'model': model, 'thinking': thinking}
    rid = store.queue_ask(text, chosen, cfg.local_user, branch, options)
    lines = [f'queued request {rid} for {", ".join(chosen)}' + (f' on existing branch {branch}' if branch else '')
             + (f' on new branch {new_branch}' if new_branch else '')]
    if service_running(cfg, store):
        lines.append(f"the running service (last poll {store.get('last-poll')}) picks it up within one poll round "
                     f'(about {cfg.poll_seconds} s)')
    else:
        lines.append(f"the service is not running or not polling (last poll: {store.get('last-poll') or 'never'}); "
                     'start `superagent run` to pick it up')
    lines.append(f'follow it with `superagent show local:{rid}` or `superagent tickets`')
    return rid, '\n'.join(lines)


def answer_request(cfg, store, ref, text):
    """Queue the operator's answer or instruction for a Ticket (applied by the running service like a human follow-up)
    and record it in the Ticket's conversation. For an ended Ticket the service reopens it first. Returns the
    confirmation line. Shared by `superagent answer` and the web panel."""
    t = find_ticket(store, ref)
    if not t:
        raise RuntimeError(f'no ticket {ref!r} (see `superagent tickets --all`)')
    if t.state == 'ended':
        other = next((o for o in store.tickets(state='open', thread_key=t.thread_key) if o.id != t.id), None)
        if other:
            raise RuntimeError(f'cannot reopen {t.name}: {other.name} is open on the same thread')
    text = (text or '').strip()
    if not text:
        raise RuntimeError('the answer is empty')
    store.queue_answer(t.id, text, cfg.local_user)
    if t.state == 'ended':
        return (f'instruction queued for {t.name} ({t.id}); the running service reopens the ticket and resumes its '
                'worker within one poll round')
    return f'answer queued for {t.name} ({t.id}); the running service delivers it within one poll round'


def _queued_request(store, ref):
    """A local request that has no Ticket yet, by `local:<id>`, as `show` reports it; None when there is none."""
    if not ref.startswith('local:') or not ref[6:].isdigit():
        return None
    r = store.local_request(int(ref[6:]))
    if not r or r['kind'] != 'ask':
        return None
    return {'request': r['id'], 'state': 'ignored' if r['done'] else 'queued', 'repos': json.loads(r['repos'] or '[]'),
            'text': r['text']}


def show_data(cfg, store, ref):
    """A Ticket's header and conversation (request, replies, answers), as `superagent show` prints it."""
    t = find_ticket(store, ref)
    if not t:
        queued = _queued_request(store, ref)
        if queued:
            return queued
        raise RuntimeError(f'no ticket {ref!r} (see `superagent tickets --all`)')
    a = t.current
    m = t.mandate
    trig = store.trigger(m.get('trigger_id'))
    conversation = [{'at': trig.at if trig else m.get('at'), 'from': m.get('actor', '?'), 'kind': 'request',
                     'text': trig.text if trig else t.title}]
    for msg in store.local_messages(t.id):
        who = (msg['actor'] or 'operator') if msg['kind'] == 'answer' else 'superagent'
        conversation.append({'at': msg['at'], 'from': who, 'kind': msg['kind'], 'text': msg['body']})
    return {'id': t.id, 'name': t.name, 'title': t.title, 'stage': stage(t), 'state': t.state, 'outcome': t.outcome,
            'repos': t.repos, 'thread': t.thread_key,
            'url': None if is_local(t) else f'https://github.com/{t.repo}/issues/{t.number}',
            'prs': pr_urls(t), 'branch': t.branch, 'requested_by': m.get('actor'),
            'last_report': a.report if a else None, 'summary': a.summary if a else '',
            'question': a.question if a else '', 'conversation': conversation}


def show_text(data):
    if 'request' in data:
        what = 'is queued; the service has not picked it up yet' if data['state'] == 'queued' \
            else 'was not taken: it names no configured repo'
        return f"request local:{data['request']} for {', '.join(data['repos'])} {what}"
    lines = [f"{data['name']} ({data['id']}) · {data['stage']} · {', '.join(data['repos'])}",
             f"title: {data['title']}",
             f"thread: {data['url'] or data['thread']} (requested by {data['requested_by']})"]
    if data['prs']:
        lines.append('PRs: ' + ', '.join(data['prs']))
    if data['outcome']:
        lines.append(f"outcome: {data['outcome']}")
    lines.append('')
    for msg in data['conversation']:
        first, *rest = (msg['text'] or '').splitlines() or ['']
        lines.append(f"[{msg['at']}] {msg['from']}: {first}")
        lines += [f'    {line}' for line in rest]
    return '\n'.join(lines)


def cmd_ask(cfg, args):
    _, message = ask_request(cfg, open_store(cfg), ' '.join(args.text), args.repo, args.branch, args.title,
                             args.new_branch, args.skill, args.model, args.thinking)
    print(message)
    return 0


def cmd_show(cfg, args):
    data = show_data(cfg, open_store(cfg), args.ticket)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else show_text(data))
    return 0


def cmd_answer(cfg, args):
    print(answer_request(cfg, open_store(cfg), args.ticket, ' '.join(args.text)))
    return 0


# Pi integration: a skill and a prompt template, installed into the user's Pi agent dir.

PI_MARKER = 'superagent-pi-install: managed file'
PI_FILES = (('pi/superagent/SKILL.md', ('skills', 'superagent', 'SKILL.md')), ('pi/superagent.md', ('prompts', 'superagent.md')))


def superagent_command():
    """The absolute command that runs this `superagent`, shell-quoted."""
    here = sys.argv[0] if os.path.basename(sys.argv[0]) == 'superagent' else None
    for c in (here, os.path.join(os.path.dirname(sys.executable), 'superagent'), shutil.which('superagent')):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return shlex.quote(os.path.abspath(c))
    return f'{shlex.quote(sys.executable)} -m superagent.cli'


def cmd_pi_install(cfg, args):
    agent = os.path.abspath(os.path.expanduser(args.agent_dir or os.environ.get('PI_CODING_AGENT_DIR')
                                               or '~/.pi/agent'))
    here = os.path.dirname(os.path.abspath(__file__))
    files = [(os.path.join(here, src), os.path.join(agent, *dst)) for src, dst in PI_FILES]

    def ours(path):
        with open(path, encoding='utf-8') as f:
            return PI_MARKER in f.read()

    if args.uninstall:
        for _, dst in files:
            if not os.path.exists(dst):
                continue
            if ours(dst):
                os.remove(dst)
                print(f'removed {dst}')
            else:
                print(f'kept {dst} (not installed by superagent)')
        try:
            os.rmdir(os.path.dirname(files[0][1]))  # skills/superagent, when empty
        except OSError:
            pass
        print('run /reload in Pi (or restart it) to unload them')
        return 0
    if not args.force:
        for _, dst in files:
            if os.path.exists(dst) and not ours(dst):
                raise RuntimeError(f'{dst} exists and was not installed by superagent; use --force to overwrite it')
    command = superagent_command()
    config = os.path.abspath(os.path.expanduser(args.config))
    if config != os.path.abspath(os.path.expanduser(DEFAULT_CONFIG)):
        command += f' --config {shlex.quote(config)}'
    skill_path = files[0][1]
    for src, dst in files:
        with open(src, encoding='utf-8') as f:
            text = f.read().replace('{{SUPERAGENT}}', command).replace('{{SKILL_PATH}}', skill_path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'wrote {dst}')
    print(f'commands use: {command}')
    print('run /reload in Pi (or restart it), then try: /superagent <what to do, naming owner/repo>')
    return 0


PANEL_EXTRA = 'install the panel extra: pip install -e ".[panel]"'


def cmd_panel(cfg, args):
    try:
        import streamlit  # noqa: F401
    except ImportError:
        raise ConfigError(PANEL_EXTRA) from None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', args.port))
        except OSError:
            raise RuntimeError(f'port {args.port} is in use on 127.0.0.1; pick another with --port') from None
    panel = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.py')
    env = {**os.environ, 'SUPERAGENT_CONFIG': os.path.abspath(os.path.expanduser(args.config))}
    print(f'superagent panel on http://127.0.0.1:{args.port} (Ctrl+C to stop)', file=sys.stderr)
    sys.stderr.flush()
    # Replace this process instead of starting a child, so Ctrl+C and kill reach streamlit directly.
    os.execve(sys.executable, [sys.executable, '-m', 'streamlit', 'run', panel, '--server.address', '127.0.0.1',
                               '--server.port', str(args.port), '--server.headless', 'true',
                               '--browser.gatherUsageStats', 'false'], env)


def cmd_check(cfg, args):
    problems = []

    def check(ok, good, bad):
        print(f'ok    {good}' if ok else f'FAIL  {bad}')
        if not ok:
            problems.append(bad)

    print(f'ok    config loads: {", ".join(cfg.repo_names)} as {cfg.agent_login}')
    token = bool(cfg.token)
    check(token, f'token: {cfg.token_env} is set',
          f'token: environment variable {cfg.token_env} is not set; export the bot account\'s fine-grained token')
    git = shutil.which('git')
    for repo in cfg.repos:
        origin = None
        if git and os.path.isdir(repo.checkout):
            from .executor import git_env
            from .github import GIT_HARDEN
            # The checkout's config and hooks are worker-writable: same hardening as the service's own git commands.
            r = subprocess.run(['git', *GIT_HARDEN, '-C', repo.checkout, 'remote', 'get-url', 'origin'],
                               env=git_env(cfg), capture_output=True, text=True)
            origin = r.stdout.strip() if r.returncode == 0 else None
        check(bool(origin), f'checkout: {repo.checkout} is a git repo with origin {origin}',
              f'checkout: {repo.checkout} is not a git clone with an origin remote; '
              f'run `git clone https://github.com/{repo.name}.git {repo.checkout}`')
    exe = shutil.which(cfg.copilot) if cfg.agent == 'copilot' else cfg.pi
    version = None
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        try:
            r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=30)
            version = (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            version = None
    if cfg.agent == 'copilot':
        check(bool(version), f'copilot: {exe} ({version})',
              f'copilot: {cfg.copilot} is missing or `copilot --version` fails; install GitHub Copilot CLI '
              '(npm install -g @github/copilot), run `copilot login`, or set executor.copilot in the config')
    else:
        check(bool(version), f'pi: {cfg.pi} ({version})',
              f'pi: {cfg.pi} is missing or `pi --version` fails; install Pi or set executor.pi in the config')
    free = True
    if cfg.report_port:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', cfg.report_port))
            except OSError:
                free = False
    check(free, f'report port {cfg.report_port} is free',
          f'report port {cfg.report_port} is in use (is `superagent run` already running?); '
          f'stop it or set service.report_port')
    if not token:
        return 2
    return 1 if problems else 0


COMMANDS = {'run': cmd_run, 'status': cmd_status, 'tickets': cmd_tickets, 'stop': _request('stop'),
            'retry': _request('retry'), 'check': cmd_check, 'panel': cmd_panel, 'ask': cmd_ask, 'show': cmd_show,
            'answer': cmd_answer, 'pi-install': cmd_pi_install}
NO_CONFIG = {'pi-install'}  # commands that work before a config file exists


def parser():
    p = argparse.ArgumentParser(prog='superagent', description='A coding agent for GitHub repositories, '
                                                               'running Pi workers.')
    p.add_argument('--config', default=DEFAULT_CONFIG, help=f'config file (default {DEFAULT_CONFIG})')
    p.add_argument('-v', '--verbose', action='store_true', help='debug logging')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('run', help='run the service until Ctrl+C')
    sub.add_parser('status', help='service state as JSON')
    t = sub.add_parser('tickets', help='list tickets')
    t.add_argument('--all', action='store_true', help='include ended tickets')
    t.add_argument('--json', action='store_true', help='JSON output')
    for name, text in (('stop', 'stop a ticket and its worker'), ('retry', 'relaunch or redeliver a ticket')):
        s = sub.add_parser(name, help=text)
        s.add_argument('ticket', help='ticket id or name')
    sub.add_parser('check', help='preflight: config, token, checkout, pi or copilot, report port')
    pn = sub.add_parser('panel', help='local web panel (needs the panel extra)')
    pn.add_argument('--port', type=int, default=8742, help='port on 127.0.0.1 (default 8742)')
    a = sub.add_parser('ask', help='queue a request from this machine (a local Ticket, thread local:<id>)')
    a.add_argument('--repo', action='append', default=[],
                   help='configured owner/name to work in (repeat for a cross-repo request; default: the only '
                        'configured repo, or the configured repos the text names)')
    a.add_argument('--title', default='',
                   help="the ticket's title, which its name and default branch agent/<name> are made from "
                        '(default: the first line of the requirements)')
    which = a.add_mutually_exclusive_group()
    which.add_argument('--branch', default='',
                       help='work on this existing branch (one repo): start from it, push onto it without force, and '
                            'reuse its open PR; default: a new branch agent/<name>')
    which.add_argument('--new-branch', default='',
                       help='name the new branch (it must not exist yet); default: agent/<name>')
    a.add_argument('--skill', action='append', default=[], metavar='PATH',
                   help='a skill folder (with a SKILL.md) or .md file the worker gets besides its own (repeat for '
                        'more)')
    a.add_argument('--model', default='',
                   help="the worker's model, as provider/model (or a model of the config's provider); default: the "
                        "config's")
    a.add_argument('--thinking', default='', choices=['', *THINKING_LEVELS],
                   help="the worker's thinking level; default: the config's")
    a.add_argument('text', nargs='+', help='the requirements: what to do')
    sh = sub.add_parser('show', help="a ticket's stage, PR links and conversation")
    sh.add_argument('ticket', help='ticket id, name, or local:<request id>')
    sh.add_argument('--json', action='store_true', help='JSON output')
    an = sub.add_parser('answer', help="answer a ticket's question, or add an instruction (resumes its worker; "
                                       'reopens an ended ticket)')
    an.add_argument('ticket', help='ticket id, name, or local:<request id>')
    an.add_argument('text', nargs='+', help='the answer')
    pi = sub.add_parser('pi-install', help='install the superagent skill and /superagent prompt template into Pi')
    pi.add_argument('--agent-dir', help='Pi agent dir (default: $PI_CODING_AGENT_DIR or ~/.pi/agent)')
    pi.add_argument('--uninstall', action='store_true', help='remove the files pi-install wrote')
    pi.add_argument('--force', action='store_true', help='overwrite files pi-install did not write')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = None if args.command in NO_CONFIG else load_config(args.config)
        return COMMANDS[args.command](cfg, args)
    except ConfigError as e:
        print(f'superagent: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # expected runtime failures are one line; -v shows the traceback
        if args.verbose:
            log.exception('failed')
        print(f'superagent: {e}', file=sys.stderr)
        return 1
    finally:
        # Closing here, not at garbage collection, keeps "unclosed database" warnings out of the output.
        while _opened:
            _opened.pop().close()


if __name__ == '__main__':
    sys.exit(main())
