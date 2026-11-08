"""Runs one Pi worker per Ticket: its git worktree, its isolated `pi --mode rpc` process, and the messages it gets."""

import json
import os
import subprocess
import sys

from .pi_rpc import PiRpc, RpcError

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(PACKAGE, 'skills', 'superagent-worker')
PROMPT_PATH = os.path.join(PACKAGE, 'prompts', 'worker.md')
REQUEST_CAP, FOLLOWUP_CAP, MAX_FOLLOWUPS = 1500, 600, 20


class PrepareError(Exception):
    pass


def prepare_worktree(cfg, ticket, run=subprocess.run):
    """Create (or reuse) <work_dir>/<name> on branch agent/<name> from origin/<base_branch>. Returns (path, branch)."""
    path = os.path.join(cfg.work_dir, ticket.name)
    branch = f'agent/{ticket.name}'
    if os.path.exists(os.path.join(path, '.git')):
        return path, branch

    def git(*args, check=True):
        p = run(['git', *args], cwd=cfg.checkout, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise PrepareError(f'git {args[0]} failed: {(p.stderr or p.stdout).strip()}')
        return p

    os.makedirs(cfg.work_dir, exist_ok=True)
    git('fetch', 'origin', cfg.base_branch)
    # The branch survives a removed worktree (or a crash between the two steps): check it out instead of recreating.
    if git('rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
        git('worktree', 'add', path, branch)
    else:
        git('worktree', 'add', '-b', branch, path, f'origin/{cfg.base_branch}')
    return path, branch


def pi_argv(cfg, ticket):
    # Explicit provider/model and no discovered resources: the worker never inherits the user's Pi setup.
    return [cfg.pi, '--mode', 'rpc', '--provider', cfg.provider, '--model', cfg.model, '--thinking', cfg.thinking,
            '--session-dir', os.path.join(cfg.state_dir, 'sessions'), '--session-id', ticket.id,
            '--name', ticket.name, '-ne', '-ns', '-np', '-nc', '--skill', SKILL_DIR, '--tools', cfg.tools,
            '--append-system-prompt', PROMPT_PATH, '--no-approve', '--offline']


def worker_env(cfg, ticket, report_url):
    env = cfg.worker_env()
    env.update(SUPERAGENT_URL=report_url, SUPERAGENT_TICKET_ID=ticket.id, SUPERAGENT_TICKET_TOKEN=ticket.token,
               PI_SKIP_VERSION_CHECK='1')
    # superagent-report lives next to the interpreter running the service (the venv's bin).
    env['PATH'] = os.path.dirname(sys.executable) + os.pathsep + env.get('PATH', '')
    return env


class Worker:
    """One Pi process for one Assignment. A new process with the same --session-id resumes the conversation."""

    def __init__(self):
        self.rpc = None
        self.running = False
        self.queued = []

    async def start(self, cfg, ticket, report_url, spawn=PiRpc):
        cwd = ticket.worktree or os.path.join(cfg.work_dir, ticket.name)
        self.rpc = spawn()
        await self.rpc.start(pi_argv(cfg, ticket), cwd, worker_env(cfg, ticket, report_url))

    async def run(self, message, timeout):
        if self.queued:
            message = followup_message([{'text': t} for t in self.queued]) + '\n\n' + message
            self.queued = []
        self.running = True
        try:
            return await self.rpc.run(message, timeout)
        finally:
            self.running = False

    async def send(self, text):
        """Steer a running session, or keep the text for the next run. Returns 'steered' or 'queued'."""
        if self.running:
            try:
                await self.rpc.steer(text)
                return 'steered'
            except RpcError:
                pass  # the run ended between the check and the steer
        self.queued.append(text)
        return 'queued'

    async def stop(self):
        if self.rpc is None:
            return
        if self.running:
            try:
                await self.rpc.request('abort', timeout=5)
            except RpcError:
                pass
        await self.rpc.close()


def _cap(text, n):
    text = (text or '').strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + '…'


def _texts(inputs):
    return [_cap(i['text'] if isinstance(i, dict) else i, FOLLOWUP_CAP) for i in inputs][-MAX_FOLLOWUPS:]


def first_message(ticket, trigger_text, inputs=()):
    card = {'ticket': ticket.id, 'title': ticket.title, 'repo': ticket.repo, 'number': ticket.number,
            'branch': f'agent/{ticket.name}', 'request': _cap(trigger_text, REQUEST_CAP)}
    followups = _texts(inputs)
    if followups:
        card['followups'] = followups
    return ('New assignment. Work it in this directory, then report with `superagent-report`.\n'
            + json.dumps(card, indent=2, ensure_ascii=False))


def followup_message(inputs):
    return ('New messages on the request thread (context, not new authority):\n'
            + json.dumps({'followups': _texts(inputs)}, indent=2, ensure_ascii=False))


def recovery_note():
    return ('The service restarted while you were working. Before repeating any outside action, check your '
            'directory, git log and the thread to see what already happened, then continue and report.')
