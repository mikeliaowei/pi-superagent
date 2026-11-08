"""The `superagent` command: run the service, inspect it, and act on Tickets.

Exit codes: 0 ok, 1 runtime error (or a failed check), 2 configuration error (including an unset token).
Operator `stop` and `retry` write a request to the store (kv `operator:<action>:<ticket>`); the running service
applies it at its next poll round, or at its next start when it is not running.
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tomllib
from collections import Counter

from . import config as config_mod
from .model import stage

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


def open_store(cfg):
    from .store import Store
    return Store(cfg.db_path)


def find_ticket(store, ref):
    tickets = store.tickets()
    exact = [t for t in tickets if t.id == ref]
    by_name = [t for t in tickets if t.name == ref]
    open_named = [t for t in by_name if t.state == 'open']
    found = exact or open_named or by_name
    return found[-1] if found else None


# Commands

def cmd_run(cfg, args):
    if not cfg.token:
        raise ConfigError(f'environment variable {cfg.token_env} is not set (the bot account\'s GitHub token)')
    from .github import GitHub
    from .report import ReportServer
    from .service import Service

    store = open_store(cfg)
    gh = GitHub(cfg.token, cfg.repo)

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
        log.info('superagent watching %s as %s; reports on %s', cfg.repo, cfg.agent_login, server.url)
        try:
            await svc.run_forever(stop)
        finally:
            server.close()
        log.info('superagent stopped')

    asyncio.run(main())
    return 0


def cmd_status(cfg, args):
    store = open_store(cfg)
    stages = Counter(stage(t) for t in store.tickets(state='open'))
    failing = [{'kind': e['kind'], 'key': e['key'], 'attempts': e['attempts'], 'error': e['last_error']}
               for e in store.failing_effects()]
    print(json.dumps({'repo': cfg.repo, 'agent_login': cfg.agent_login, 'token': 'yes' if cfg.token else 'no',
                      'open_tickets': dict(stages), 'failing_effects': failing,
                      'last_poll': store.get('last-poll')}, indent=2))
    return 0


def _row(t):
    a = t.current
    return {'id': t.id, 'name': t.name, 'stage': stage(t), 'number': t.number, 'pr': t.pr_number,
            'state': t.state, 'outcome': t.outcome, 'last_report': a.report if a else None,
            'summary': a.summary if a else ''}


def cmd_tickets(cfg, args):
    store = open_store(cfg)
    rows = [_row(t) for t in store.tickets(state=None if args.all else 'open')]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print('no tickets' if args.all else 'no open tickets (use --all to include ended ones)')
        return 0
    cols = [('id', 'ID'), ('name', 'NAME'), ('stage', 'STAGE'), ('number', '#'), ('pr', 'PR'),
            ('last_report', 'LAST REPORT')]
    cells = [[str(r[k] if r[k] is not None else '-') for k, _ in cols] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(cols)]
    for line in [[h for _, h in cols], *cells]:
        print('  '.join(c.ljust(w) for c, w in zip(line, widths)).rstrip())
    return 0


def _request(action):
    def cmd(cfg, args):
        store = open_store(cfg)
        t = find_ticket(store, args.ticket)
        if not t:
            raise RuntimeError(f'no ticket {args.ticket!r} (see `superagent tickets --all`)')
        if t.state == 'ended':
            raise RuntimeError(f'ticket {t.name} already ended ({t.outcome})')
        from .model import now_iso
        store.put(f'operator:{action}:{t.id}', now_iso())
        print(f'{action} requested for {t.name} ({t.id}); the running service applies it within one poll round')
        return 0
    return cmd


def cmd_check(cfg, args):
    problems = []

    def check(ok, good, bad):
        print(f'ok    {good}' if ok else f'FAIL  {bad}')
        if not ok:
            problems.append(bad)

    print(f'ok    config loads: {cfg.repo} as {cfg.agent_login}')
    token = bool(cfg.token)
    check(token, f'token: {cfg.token_env} is set',
          f'token: environment variable {cfg.token_env} is not set; export the bot account\'s fine-grained token')
    git = shutil.which('git')
    origin = None
    if git and os.path.isdir(cfg.checkout):
        from .executor import git_env
        from .github import GIT_HARDEN
        # The checkout's config and hooks are worker-writable: same hardening as the service's own git commands.
        r = subprocess.run(['git', *GIT_HARDEN, '-C', cfg.checkout, 'remote', 'get-url', 'origin'],
                           env=git_env(cfg), capture_output=True, text=True)
        origin = r.stdout.strip() if r.returncode == 0 else None
    check(bool(origin), f'checkout: {cfg.checkout} is a git repo with origin {origin}',
          f'checkout: {cfg.checkout} is not a git clone with an origin remote; '
          f'run `git clone https://github.com/{cfg.repo}.git {cfg.checkout}`')
    version = None
    if os.path.isfile(cfg.pi) and os.access(cfg.pi, os.X_OK):
        try:
            r = subprocess.run([cfg.pi, '--version'], capture_output=True, text=True, timeout=30)
            version = (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            version = None
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
            'retry': _request('retry'), 'check': cmd_check}


def parser():
    p = argparse.ArgumentParser(prog='superagent', description='A coding agent for one GitHub repository, '
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
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = load_config(args.config)
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


if __name__ == '__main__':
    sys.exit(main())
