"""Runs one worker per Ticket: its git worktree, its isolated agent process (`pi --mode rpc`, or `copilot --acp`),
and the messages it gets."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from functools import partial

from .copilot_acp import CopilotAcp
from .github import GIT_HARDEN, GitHubError, fetch_from_github, git_env as _git_env
from .pi_rpc import PiRpc, RpcError
from .proctree import group_options, kill_tree
from .worker_log import copilot_home, copilot_session_file

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(PACKAGE, 'skills', 'superagent-worker')
PROMPT_PATH = os.path.join(PACKAGE, 'prompts', 'worker.md')
REQUEST_CAP, FOLLOWUP_CAP, MAX_FOLLOWUPS = 1500, 600, 20
TEST_OUTPUT_CAP = 64 * 1024  # bytes of a test command's output kept (the tail)
TEST_TAIL = 3000  # characters of that output sent back to the worker
WINDOWS = os.name == 'nt'
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


def _add_worktree(cfg, repo, path, branch, run, start=None):
    """Create (or reuse) a worktree of `repo` (a RepoConfig) at `path` on local `branch`, new from
    origin/<start> (default: the repo's base branch), or from the local branch <start> when GitHub has no such branch."""
    if os.path.exists(os.path.join(path, '.git')):
        return
    env = git_env(cfg)
    start = start or repo.base_branch

    def git(*args, check=True):
        # The checkout shares config and hooks with every worker's worktree: hooks off, no secrets in env.
        p = run(['git', *GIT_HARDEN, *args], cwd=repo.checkout, env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise PrepareError(f'git {args[0]} failed: {(p.stderr or p.stdout).strip()}')
        return p

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fetch_error = fetch_origin(cfg, repo, start, run)
    start_ref = f'origin/{start}'
    if fetch_error:
        local = git('rev-parse', '--verify', '--quiet', f'refs/heads/{start}', check=False).returncode == 0
        if not (start != repo.base_branch and local and "couldn't find remote ref" in fetch_error):
            raise PrepareError(f'git fetch failed: {fetch_error}')
        start_ref = f'refs/heads/{start}'  # only in the checkout: the first delivery publishes it
    # The branch survives a removed worktree (or a crash between the two steps): check it out instead of recreating.
    if git('rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
        git('worktree', 'add', path, branch)
    else:
        git('worktree', 'add', '-b', branch, path, start_ref)


def fetch_origin(cfg, repo, branch, run=subprocess.run, timeout=None):
    """git fetch origin <branch> in the checkout of `repo` (a RepoConfig); '*' fetches and prunes every branch.

    An origin on github.com is fetched with the GitHub token (fetch_from_github): outside VS Code nothing else
    signs in for git. Returns git's error output, '' when it worked."""
    env = git_env(cfg)
    url = run(['git', *GIT_HARDEN, 'config', '--get', 'remote.origin.url'], cwd=repo.checkout, env=env,
              capture_output=True, text=True, timeout=timeout).stdout.strip().rstrip('/').removesuffix('.git')
    if cfg.token and url.lower() == f'https://github.com/{repo.name}'.lower():
        try:
            fetch_from_github(repo.checkout, repo.name, cfg.token, branch, os.path.dirname(cfg.push_dir(repo)),
                              run, env, prune=branch == '*', timeout=timeout)
        except GitHubError as e:
            return str(e)
        return ''
    args = ['fetch', '--prune', '--no-tags', 'origin'] if branch == '*' else ['fetch', 'origin', branch]
    p = run(['git', *GIT_HARDEN, *args], cwd=repo.checkout, env=env, capture_output=True, text=True,
            timeout=timeout)
    return '' if p.returncode == 0 else (p.stderr or p.stdout).strip()


def prepare_worktree(cfg, ticket, run=subprocess.run):
    """Create (or reuse) the Ticket's worktree on branch agent/<name> from origin/<base_branch> of its repo.

    For a Ticket on an existing branch the local branch agent/<name> starts from origin/<existing branch> instead
    (so it never clashes with a checkout already on that branch), and the returned branch is the existing one: the
    one delivery pushes to. A new branch the request named (`ticket.branch` before the first launch) is returned the
    same way, while the local branch stays agent/<name>. A cross-repo Ticket gets one worktree per part inside its
    ticket folder; their paths are written into `ticket.parts`. Returns (path, branch): the worktree, or the ticket
    folder."""
    path = worktree_path(cfg, ticket)
    branch = f'agent/{ticket.name}'
    if not ticket.parts:
        _add_worktree(cfg, cfg.repo_config(ticket.repo), path, branch, run, ticket.existing_branch)
        return path, ticket.existing_branch or ticket.branch or branch
    for part in ticket.parts:
        repo = cfg.repo_config(part['repo'])
        part['worktree'] = part['worktree'] or os.path.join(path, repo.slug)
        _add_worktree(cfg, repo, part['worktree'], branch, run)
    return path, ticket.branch or branch


def worker_model(cfg, ticket):
    """(provider, model) the Ticket's worker runs: the request's choice ('provider/model', or a model of the config's
    provider), else the config's."""
    if not ticket.model:
        return cfg.provider, cfg.model
    provider, _, model = ticket.model.partition('/')
    return (provider, model) if model else (cfg.provider, provider)


def pi_argv(cfg, ticket):
    # Explicit provider/model and no discovered resources: the worker never inherits the user's Pi setup. Only the
    # skills the request chose are added to the worker's own.
    skills = [a for s in ticket.skills for a in ('--skill', s)]
    provider, model = worker_model(cfg, ticket)
    return [cfg.pi, '--mode', 'rpc', '--provider', provider, '--model', model,
            '--thinking', ticket.thinking or cfg.thinking,
            '--session-dir', os.path.join(cfg.state_dir, 'sessions'), '--session-id', ticket.id,
            '--name', ticket.name, '-ne', '-ns', '-np', '-nc', '--skill', SKILL_DIR, *skills, '--tools', cfg.tools,
            '--append-system-prompt', PROMPT_PATH, '--no-approve', '--offline']


def copilot_argv(cfg, ticket):
    # The user's plugins, AGENTS.md files and Copilot's GitHub MCP server (their GitHub login) stay out, and its
    # COPILOT_HOME is its own (Worker.start). Nobody is at a terminal, so every tool is allowed and none asks the user.
    _, model = worker_model(cfg, ticket)
    effort = ticket.thinking or cfg.thinking
    return [shutil.which(cfg.copilot) or cfg.copilot, '--acp', '--stdio', '--model', model,
            '--reasoning-effort', 'none' if effort == 'off' else effort, '--allow-all-tools', '--disable-builtin-mcps',
            '--no-ask-user', '--no-custom-instructions', '--no-auto-update',
            *[a for s in ticket.skills for a in ('--add-dir', s)]]


def copilot_preamble(ticket):
    """What Pi gets as --append-system-prompt and --skill: Copilot reads it at the top of its first message."""
    with open(PROMPT_PATH, encoding='utf-8') as f:
        prompt = f.read().strip()
    with open(os.path.join(SKILL_DIR, 'SKILL.md'), encoding='utf-8') as f:
        skill = f.read().split('---', 2)[-1].strip()  # without its front matter
    extra = ''.join(f'\n- {os.path.join(s, "SKILL.md")}' for s in ticket.skills)
    return f'{prompt}\n\n{skill}' + (f'\n\nRead these skills before you start:{extra}' if extra else '')


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
            argv = self.command if WINDOWS else ['sh', '-c', self.command]  # cmd.exe parses a string
            self.proc = subprocess.Popen(argv, shell=WINDOWS, **group_options(), **opts)
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
        if self.proc is not None:
            kill_tree(self.proc.pid)


class Worker:
    """One agent process for one Assignment. A new process for the same Ticket resumes its conversation."""

    def __init__(self):
        self.rpc = None
        self.running = False
        self.queued = []

    async def start(self, cfg, ticket, report_url, spawn=None):
        cwd, env = worktree_path(cfg, ticket), worker_env(cfg, ticket, report_url)
        if cfg.agent == 'copilot':
            spawn = spawn or partial(CopilotAcp, copilot_session_file(cfg.state_dir, ticket.id),
                                     copilot_preamble(ticket))
            # Its own COPILOT_HOME: the user's ~/.copilot setup added ~140k tokens to every model call. The login is
            # in the OS credential store, so it still signs in.
            env.update(COPILOT_HOME=copilot_home(cfg.state_dir), COPILOT_PLUGIN_DIR_ONLY='true',
                       COPILOT_AUTO_UPDATE='false')
            argv = copilot_argv(cfg, ticket)
        else:
            spawn, argv = spawn or PiRpc, pi_argv(cfg, ticket)
        self.rpc = spawn()
        await self.rpc.start(argv, cwd, env)

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
    if ticket.existing_branch:
        text += (f'\n\nThis directory starts from the existing branch `{ticket.existing_branch}`, which may already '
                 'have an open pull request. The service pushes your commits onto it without force, so add commits '
                 'on top: do not rebase, amend or squash existing commits.')
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
