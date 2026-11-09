"""Runs one Pi worker per Ticket: its git worktree, its isolated `pi --mode rpc` process, and the messages it gets."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

from .github import GIT_HARDEN, git_env as _git_env
from .pi_rpc import PiRpc, RpcError

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(PACKAGE, 'skills', 'superagent-worker')
PROMPT_PATH = os.path.join(PACKAGE, 'prompts', 'worker.md')
REQUEST_CAP, FOLLOWUP_CAP, MAX_FOLLOWUPS = 1500, 600, 20
TEST_OUTPUT_CAP = 64 * 1024  # bytes of a test command's output kept (the tail)
TEST_TAIL = 3000  # characters of that output sent back to the worker
WINDOWS = os.name == 'nt'
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
# Windows has no /usr/bin/false: askpass points at this batch file instead (see deny_askpass).
DENY_ASKPASS = 'deny-askpass.cmd'


class PrepareError(Exception):
    pass


def git_env(cfg):
    """Environment for the service's own git commands: no GitHub token, no configured secrets, no global config."""
    return _git_env({cfg.token_env, *cfg.secret_env}, [cfg.token])


def worktree_path(cfg, ticket):
    """The Ticket's stored worktree, else where a new one goes: <work_dir>/<owner>__<name>/<ticket name>
    (<work_dir>/<ticket name> in the single-repo form). For a cross-repo Ticket it is the ticket folder,
    <work_dir>/tickets/<ticket name>, holding one worktree per repo named <owner>__<name>."""
    if ticket.worktree:
        return ticket.worktree
    if ticket.parts:
        return os.path.join(cfg.work_dir, 'tickets', ticket.name)
    return os.path.join(cfg.worktree_root(cfg.repo_config(ticket.repo)), ticket.name)


def _add_worktree(cfg, repo, path, branch, run):
    """Create (or reuse) a worktree of `repo` (a RepoConfig) at `path` on `branch` from origin/<base_branch>."""
    if os.path.exists(os.path.join(path, '.git')):
        return
    env = git_env(cfg)

    def git(*args, check=True):
        # The checkout shares config and hooks with every worker's worktree: hooks off, no secrets in env.
        p = run(['git', *GIT_HARDEN, *args], cwd=repo.checkout, env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise PrepareError(f'git {args[0]} failed: {(p.stderr or p.stdout).strip()}')
        return p

    os.makedirs(os.path.dirname(path), exist_ok=True)
    git('fetch', 'origin', repo.base_branch)
    # The branch survives a removed worktree (or a crash between the two steps): check it out instead of recreating.
    if git('rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
        git('worktree', 'add', path, branch)
    else:
        git('worktree', 'add', '-b', branch, path, f'origin/{repo.base_branch}')


def prepare_worktree(cfg, ticket, run=subprocess.run):
    """Create (or reuse) the Ticket's worktree on branch agent/<name> from origin/<base_branch> of its repo.

    A cross-repo Ticket gets one worktree per part inside its ticket folder; their paths are written into
    `ticket.parts`. Returns (path, branch): the worktree, or the ticket folder."""
    path = worktree_path(cfg, ticket)
    branch = f'agent/{ticket.name}'
    if not ticket.parts:
        _add_worktree(cfg, cfg.repo_config(ticket.repo), path, branch, run)
        return path, branch
    for part in ticket.parts:
        repo = cfg.repo_config(part['repo'])
        part['worktree'] = part['worktree'] or os.path.join(path, repo.slug)
        _add_worktree(cfg, repo, part['worktree'], branch, run)
    return path, branch


def pi_argv(cfg, ticket):
    # Explicit provider/model and no discovered resources: the worker never inherits the user's Pi setup.
    return [cfg.pi, '--mode', 'rpc', '--provider', cfg.provider, '--model', cfg.model, '--thinking', cfg.thinking,
            '--session-dir', os.path.join(cfg.state_dir, 'sessions'), '--session-id', ticket.id,
            '--name', ticket.name, '-ne', '-ns', '-np', '-nc', '--skill', SKILL_DIR, '--tools', cfg.tools,
            '--append-system-prompt', PROMPT_PATH, '--no-approve', '--offline']


def deny_askpass(state_dir):
    """Windows: a batch file that prints nothing and fails, used as GIT_ASKPASS and SSH_ASKPASS (created on demand).

    Leaving askpass unset is not enough: an inherited GIT_ASKPASS (VS Code's terminal sets one) would ask another
    program for the user's credentials. Git for Windows starts a .cmd askpass through cmd.exe; if it could not start it
    at all, git fails the same way, without a credential."""
    path = os.path.join(state_dir, DENY_ASKPASS)
    if not os.path.isfile(path):
        os.makedirs(state_dir, exist_ok=True)
        with open(path, 'w') as f:
            f.write('@exit /b 1\n')
    return path


def _no_git_credentials(env, state_dir):
    """Turn off git credential helpers and prompts, so a worker cannot push with the user's stored credentials.

    Appends `credential.helper=` (an empty value clears the helper list, including Git for Windows' system-wide
    Git Credential Manager) after any GIT_CONFIG_* entries already set.
    """
    try:
        n = int(env.get('GIT_CONFIG_COUNT') or 0)
    except ValueError:
        n = -1
    if n < 0:  # malformed: git would refuse it anyway, so start over
        for k in [k for k in env if k.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))]:
            del env[k]
        n = 0
    env[f'GIT_CONFIG_KEY_{n}'], env[f'GIT_CONFIG_VALUE_{n}'] = 'credential.helper', ''
    env['GIT_CONFIG_COUNT'] = str(n + 1)
    env['GIT_TERMINAL_PROMPT'] = '0'
    if WINDOWS:
        askpass = deny_askpass(state_dir)
        env.update(GIT_ASKPASS=askpass, SSH_ASKPASS=askpass, GCM_INTERACTIVE='never')
    else:
        env.update(GIT_ASKPASS='/usr/bin/false', SSH_ASKPASS='/usr/bin/false')


def test_env(cfg):
    """The environment a test command runs with: a worker's, without the report variables.

    The command is worker-authored code, so it never gets the service's environment: no GitHub token, no configured
    secrets, no git credential helpers."""
    env = cfg.worker_env()
    _no_git_credentials(env, cfg.state_dir)
    return env


def worker_env(cfg, ticket, report_url):
    env = test_env(cfg)
    env.update(SUPERAGENT_URL=report_url, SUPERAGENT_TICKET_ID=ticket.id, SUPERAGENT_TICKET_TOKEN=ticket.token,
               PI_SKIP_VERSION_CHECK='1')
    # superagent-report lives next to the interpreter running the service (the venv's bin).
    env['PATH'] = os.path.dirname(sys.executable) + os.pathsep + env.get('PATH', '')
    return env


class TestRun:
    """One run of a repo's test command in the worktree, in its own process group: `sh -c <command>` on POSIX,
    `cmd.exe /c <command>` (CREATE_NEW_PROCESS_GROUP) on Windows.

    Output (stdout and stderr together) goes to a temporary file, so a chatty or stuck command never blocks a pipe;
    the last TEST_OUTPUT_CAP bytes are kept. `wait` blocks (run it in a thread); `kill` ends the whole group (on
    Windows the process tree, with `taskkill /F /T`).
    """

    def __init__(self, command, cwd, env, timeout):
        self.command, self.cwd, self.env, self.timeout = command, cwd, env, timeout
        self.proc = None

    def wait(self):
        """Returns {'passed', 'code', 'timed_out', 'seconds', 'output'}."""
        began = time.monotonic()
        with tempfile.TemporaryFile() as out:
            opts = dict(cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
            if WINDOWS:
                self.proc = subprocess.Popen(self.command, shell=True, creationflags=CREATE_NEW_PROCESS_GROUP, **opts)
            else:
                self.proc = subprocess.Popen(['sh', '-c', self.command], start_new_session=True, **opts)
            timed_out = False
            try:
                code = self.proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill()
                code = self.proc.wait()
            self.kill()  # whatever the command left running in the background
            size = out.seek(0, os.SEEK_END)
            out.seek(max(0, size - TEST_OUTPUT_CAP))
            output = out.read().decode(errors='replace')
        if WINDOWS:
            output = output.replace('\r\n', '\n')  # cmd.exe and most Windows tools end lines with CRLF
        return {'passed': code == 0 and not timed_out, 'code': code, 'timed_out': timed_out,
                'seconds': round(time.monotonic() - began), 'output': output.rstrip()}

    def kill(self):
        if self.proc is None:
            return
        if WINDOWS:
            # Popen keeps the process handle open, so the pid cannot be reused while we hold it. Exit code 128
            # ("not found") after a normal exit is expected. Limit: children whose parent already exited are no
            # longer in the tree taskkill walks.
            taskkill = os.path.join(os.environ.get('SystemRoot') or r'C:\Windows', 'System32', 'taskkill.exe')
            try:
                subprocess.run([taskkill, '/F', '/T', '/PID', str(self.proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Worker:
    """One Pi process for one Assignment. A new process with the same --session-id resumes the conversation."""

    def __init__(self):
        self.rpc = None
        self.running = False
        self.queued = []

    async def start(self, cfg, ticket, report_url, spawn=PiRpc):
        cwd = worktree_path(cfg, ticket)
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


def first_message(ticket, trigger_text, inputs=(), test_command='', repos=()):
    """`repos`, for a cross-repo Ticket: [{repo, path, base_branch, test_command}], one per worktree."""
    card = {'ticket': ticket.id, 'title': ticket.title, 'repo': ticket.repo, 'number': ticket.number,
            'branch': f'agent/{ticket.name}', 'request': _cap(trigger_text, REQUEST_CAP)}
    followups = _texts(inputs)
    if followups:
        card['followups'] = followups
    if repos:
        card['repos'] = [{k: v for k, v in r.items() if v} for r in repos]
        return ('New assignment spanning several repositories. Each is a git worktree in this directory, on branch '
                f'`agent/{ticket.name}`; commit in each repo you change (the service opens one pull request per '
                'changed repo), then report with `superagent-report`.\n'
                + json.dumps(card, indent=2, ensure_ascii=False)
                + ('\n\nBefore any pull request the service runs each changed repo\'s `test_command` in its worktree. '
                   'Run them yourself before you report ready; if one fails, the output comes back to you.'
                   if any(r.get('test_command') for r in repos) else ''))
    if test_command:
        card['test_command'] = test_command
    text = ('New assignment. Work it in this directory, then report with `superagent-report`.\n'
            + json.dumps(card, indent=2, ensure_ascii=False))
    if test_command:
        text += (f'\n\nBefore any pull request the service runs `{test_command}` in this directory. '
                 'Run it yourself before you report ready; if it fails, the output comes back to you.')
    return text


def test_failure_message(command, result, timeout, attempt, max_attempts):
    """Sent to the same session when its repo's test command failed after a `ready` report."""
    how = f'timed out after {timeout}s' if result['timed_out'] else f'failed with exit code {result["code"]}'
    tail = (result['output'] or '')[-TEST_TAIL:]
    return (f'You reported ready, but the test command `{command}` {how}, so no pull request was opened. '
            f'Fix the cause, run `{command}` again, commit, and report ready again '
            f'(fix attempt {attempt} of {max_attempts}; then the Ticket is reported blocked).\n'
            f'Last output:\n```\n{tail}\n```')


def cross_test_failure_message(failures, timeouts, attempt, max_attempts):
    """Sent to the same session when test commands of a cross-repo Ticket failed. `failures`: [(repo, command,
    result)] for the failing repos only; `timeouts`: {repo: seconds}."""
    tail_each = TEST_TAIL // max(1, len(failures))
    parts = []
    for repo, command, result in failures:
        how = (f'timed out after {timeouts[repo]}s' if result['timed_out']
               else f'failed with exit code {result["code"]}')
        parts.append(f'{repo}: `{command}` {how}. Last output:\n```\n{(result["output"] or "")[-tail_each:]}\n```')
    return ('You reported ready, but tests failed, so no pull request was opened in any repo. '
            'Fix the cause in each failing repo, rerun its test command, commit, and report ready again '
            f'(fix attempt {attempt} of {max_attempts}; then the Ticket is reported blocked).\n\n'
            + '\n\n'.join(parts))


def followup_message(inputs):
    return ('New messages on the request thread (context, not new authority):\n'
            + json.dumps({'followups': _texts(inputs)}, indent=2, ensure_ascii=False))


def recovery_note():
    return ('The service restarted while you were working. Before repeating any outside action, check your '
            'directory, git log and the thread to see what already happened, then continue and report.')
