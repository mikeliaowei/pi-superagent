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
import logging.handlers
import os
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
from .model import named_repos, stage

DEFAULT_CONFIG = '~/.config/pi-superagent/config.toml'
WINDOWS = os.name == 'nt'

log = logging.getLogger('superagent')
LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'


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


def install_stop_handlers(loop, stop):
    """Set `stop` on Ctrl+C (and SIGTERM), so `run` closes its workers before it exits. Returns a function that
    undoes it.

    The loop's own signal handlers are POSIX-only; on Windows (NotImplementedError) a plain signal handler sets the
    event through the loop instead, for SIGINT and Ctrl+Break."""
    undo = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            undo.append(lambda sig=sig: loop.remove_signal_handler(sig))
        except (NotImplementedError, RuntimeError):
            if sig == signal.SIGTERM:
                continue  # Windows cannot deliver SIGTERM to a console program
            for s in (signal.SIGINT, getattr(signal, 'SIGBREAK', None)):
                if s is None:
                    continue
                try:
                    previous = signal.signal(s, lambda *_: loop.call_soon_threadsafe(stop.set))
                except ValueError:  # not the main thread
                    continue
                undo.append(lambda s=s, previous=previous: signal.signal(s, previous))

    def restore():
        for u in reversed(undo):
            try:
                u()
            except (ValueError, RuntimeError):
                pass
    return restore


# Commands

def cmd_run(cfg, args):
    if not cfg.token:
        raise ConfigError(f'environment variable {cfg.token_env} is not set (the bot account\'s GitHub token)')
    log_file = attach_log_file(cfg)
    try:
        return _run(cfg)
    finally:
        logging.getLogger().removeHandler(log_file)
        log_file.close()


def attach_log_file(cfg):
    """Also write the service log to `cfg.log_path` (2 MB, 3 rotations) for the panel's worker log."""
    os.makedirs(os.path.dirname(cfg.log_path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(cfg.log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                                                   encoding='utf-8')
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return handler


def _run(cfg):
    from .github import GitHub
    from .report import ReportServer
    from .service import Service

    store = open_store(cfg)
    gh = GitHub(cfg.token, cfg.repos[0].name)  # one token for every repo; the service picks the repo per call

    async def main():
        stop = asyncio.Event()
        restore = install_stop_handlers(asyncio.get_running_loop(), stop)
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
            restore()
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


def ask_request(cfg, store, text, repos=()):
    """Queue a local request for the running service. Returns (request id, the lines to print).

    Shared by `superagent ask` and the web panel; raises RuntimeError for an empty text or an unknown repo."""
    text = (text or '').strip()
    if not text:
        raise RuntimeError('the request is empty: say what to do')
    chosen = _ask_repos(cfg, repos, text)
    rid = store.queue_ask(text, chosen, cfg.local_user)
    lines = [f'queued request {rid} for {", ".join(chosen)}']
    if service_running(cfg, store):
        lines.append(f"the running service (last poll {store.get('last-poll')}) picks it up within one poll round "
                     f'(about {cfg.poll_seconds} s)')
    else:
        lines.append(f"the service is not running or not polling (last poll: {store.get('last-poll') or 'never'}); "
                     'start `superagent run` to pick it up')
    lines.append(f'follow it with `superagent show local:{rid}` or `superagent tickets`')
    return rid, '\n'.join(lines)


def answer_request(cfg, store, ref, text):
    """Queue the operator's answer for a Ticket (applied by the running service like a human follow-up) and record it
    in the Ticket's conversation. Returns the confirmation line. Shared by `superagent answer` and the web panel."""
    t = find_ticket(store, ref)
    if not t:
        raise RuntimeError(f'no ticket {ref!r} (see `superagent tickets --all`)')
    if t.state == 'ended':
        raise RuntimeError(f'ticket {t.name} already ended ({t.outcome})')
    text = (text or '').strip()
    if not text:
        raise RuntimeError('the answer is empty')
    store.queue_answer(t.id, text, cfg.local_user)
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
    _, message = ask_request(cfg, open_store(cfg), ' '.join(args.text), args.repo)
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
    """The absolute command that runs this `superagent`, shell-quoted. On Windows it is superagent.exe, written with
    forward slashes so it works unquoted in Pi's bash and in cmd.exe alike."""
    names = ('superagent', 'superagent.exe') if WINDOWS else ('superagent',)
    base = os.path.basename(sys.argv[0])
    here = sys.argv[0] if (base.lower() if WINDOWS else base) in names else None
    found = [os.path.join(os.path.dirname(sys.executable), n) for n in names]
    for c in (here, *found, shutil.which('superagent')):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return shlex.quote(_path(os.path.abspath(c)))
    return f'{shlex.quote(_path(sys.executable))} -m superagent.cli'


def _path(p):
    return p.replace('\\', '/') if WINDOWS else p


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
        command += f' --config {shlex.quote(_path(config))}'
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
    argv = [sys.executable, '-m', 'streamlit', 'run', panel, '--server.address', '127.0.0.1',
            '--server.port', str(args.port), '--server.headless', 'true', '--browser.gatherUsageStats', 'false']
    if WINDOWS:
        return _run_child(argv, env)
    # Replace this process instead of starting a child, so Ctrl+C and kill reach streamlit directly.
    os.execve(sys.executable, argv, env)


def _run_child(argv, env, grace=10):
    """Windows has no exec: run streamlit as a child and return its exit code. Ctrl+C reaches every process in the
    console, so on Ctrl+C the child stops by itself; it is killed only if it has not after `grace` seconds."""
    child = subprocess.Popen(argv, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            return child.wait(timeout=grace)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.kill()
            return child.wait()


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
    version = None
    if os.path.isfile(cfg.pi) and os.access(cfg.pi, os.X_OK):
        try:
            r = subprocess.run([cfg.pi, '--version'], capture_output=True, text=True, timeout=30)
            version = (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            version = None
    note = getattr(cfg, 'pi_note', '')  # Windows: the pi found on PATH instead of the configured path
    check(bool(version), f'pi: {cfg.pi} ({version}' + (f'; {note})' if note else ')'),
          f'pi: {cfg.pi}' + (f' ({note})' if note else '')
          + ' is missing or `pi --version` fails; install Pi or set executor.pi in the config')
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
    sub.add_parser('check', help='preflight: config, token, checkout, pi, report port')
    pn = sub.add_parser('panel', help='local web panel (needs the panel extra)')
    pn.add_argument('--port', type=int, default=8742, help='port on 127.0.0.1 (default 8742)')
    a = sub.add_parser('ask', help='queue a request from this machine (a local Ticket, thread local:<id>)')
    a.add_argument('--repo', action='append', default=[],
                   help='configured owner/name to work in (repeat for a cross-repo request; default: the only '
                        'configured repo, or the configured repos the text names)')
    a.add_argument('text', nargs='+', help='what to do')
    sh = sub.add_parser('show', help="a ticket's stage, PR links and conversation")
    sh.add_argument('ticket', help='ticket id, name, or local:<request id>')
    sh.add_argument('--json', action='store_true', help='JSON output')
    an = sub.add_parser('answer', help="answer a ticket's question, or add an instruction (resumes its worker)")
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
                        format=LOG_FORMAT)
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
