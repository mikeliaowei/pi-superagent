"""Runs one worker per Ticket: its git worktree, its isolated agent process (`pi --mode rpc`, or `copilot --acp`),
and the messages it gets."""

import base64
import json
import logging
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
from .model import new_part
from .pi_rpc import PiRpc, RpcError
from .proctree import group_options, kill_tree
from .worker_log import copilot_home, copilot_session_file, pi_session_files

log = logging.getLogger('superagent.executor')

PACKAGE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(PACKAGE, 'skills', 'superagent-worker')
PROMPT_PATH = os.path.join(PACKAGE, 'prompts', 'worker.md')
REQUEST_CAP, FOLLOWUP_CAP, MAX_FOLLOWUPS = 1500, 600, 20
TEST_OUTPUT_CAP = 64 * 1024  # bytes of a test command's output kept (the tail)
# Attached files: kept under <state_dir>/attachments, listed in the worker's message; images also go to the model.
MAX_FILES, MAX_FILE_BYTES, MAX_IMAGE_BYTES = 10, 10 * 1024 * 1024, 20 * 1024 * 1024  # last: all images of a message
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
               '.webp': 'image/webp'}
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


def ensure_checkout(cfg, repo, run=subprocess.run):
    """Make superagent's own checkout of `repo` (a RepoConfig the config names no checkout for) when its folder is
    missing or empty: a git repo whose origin is the GitHub repo, which fetch_origin then fills with the token. So
    nobody has to clone it first. A checkout the config names is never made. Returns '' or what went wrong."""
    if os.path.exists(os.path.join(repo.checkout, '.git')):
        return ''
    if repo.checkout != cfg.own_checkout(repo.name):  # a configured clone is the user's to make
        return (f'{repo.checkout} is not a git clone of {repo.name}: clone it there, or leave `checkout` out of the '
                'config and superagent makes its own copy')
    try:
        if os.path.isdir(repo.checkout) and os.listdir(repo.checkout):
            return f'{repo.checkout} is not a git clone of {repo.name}: empty it, or set its checkout in the config'
        os.makedirs(repo.checkout, exist_ok=True)
    except OSError as e:
        return f'cannot make the checkout {repo.checkout}: {e}'
    env = git_env(cfg)
    for args in (['init', '-q'], ['remote', 'add', 'origin', f'https://github.com/{repo.name}.git']):
        p = run(['git', *GIT_HARDEN, *args], cwd=repo.checkout, env=env, capture_output=True, text=True)
        if p.returncode != 0:
            return f'git {args[0]} failed in {repo.checkout}: {(p.stderr or p.stdout).strip()}'
    return ''


def _add_worktree(cfg, repo, path, branch, run, start=None):
    """Create (or reuse) a worktree of `repo` (a RepoConfig) at `path` on local `branch`, new from
    origin/<start> (default: the repo's base branch), or from the local branch <start> when GitHub has no such branch.
    A reused worktree gets origin/<base branch> (and origin/<start>) fetched again: its worker has no credentials to
    fetch them, and a resumed run (an answer, say "fix the conflict") must see GitHub as it is now."""
    if os.path.exists(os.path.join(path, '.git')):
        for name in dict.fromkeys([repo.base_branch, start or repo.base_branch]):
            if error := fetch_origin(cfg, repo, name, run):
                log.warning('could not refresh origin/%s of %s for a resumed worker: %s', name, repo.name, error)
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
    if error := ensure_checkout(cfg, repo, run):
        raise PrepareError(error)
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


def add_repo_to_ticket(cfg, ticket, name, run=subprocess.run):
    """Add the configured repo `name` to `ticket` (changed in place; the caller saves it), at its worker's request and
    a person's approval. A single-repo Ticket becomes a cross-repo one first: its worktree moves into the ticket folder
    (git worktree move, so its commits and branch stay), and its pull request and test result go to its part. The
    new repo's worktree joins the folder on agent/<ticket name>; before the first launch, prepare_worktree makes it."""
    folder = os.path.join(cfg.work_dir, 'tickets', ticket.name)
    if not ticket.parts:
        first = new_part(ticket.repo)
        first.update(pr_number=ticket.pr_number, test_result=ticket.test_result)
        if ticket.worktree:
            own = cfg.repo_config(ticket.repo)
            first['worktree'] = os.path.join(folder, own.slug)
            os.makedirs(folder, exist_ok=True)
            p = run(['git', *GIT_HARDEN, 'worktree', 'move', ticket.worktree, first['worktree']], cwd=own.checkout,
                    env=git_env(cfg), capture_output=True, text=True)
            if p.returncode != 0:
                raise PrepareError(f'git worktree move failed: {(p.stderr or p.stdout).strip()}')
            ticket.worktree = folder
        ticket.parts = [first]
    repo = cfg.repo_config(name)
    part = new_part(repo.name)
    if ticket.worktree:
        part['worktree'] = os.path.join(ticket.worktree, repo.slug)
        _add_worktree(cfg, repo, part['worktree'], f'agent/{ticket.name}', run)
    ticket.parts.append(part)


def repo_added_message(cfg, ticket, name):
    """What a resumed worker is told after `name` joined its Ticket: where each repo's worktree is now."""
    lines = [f'A person approved adding {name} to this Ticket. This directory is now a ticket folder with one git '
             f'worktree per repo, each on branch `agent/{ticket.name}` (an earlier worktree moved here: use these '
             'paths, not the old one):']
    for p in ticket.parts:
        rc = cfg.repo_config(p['repo'])
        lines.append(f"- {rc.name}: {p['worktree'] or '(made when you start)'}, base `{rc.base_branch}`"
                     + (f', test command `{rc.test_command}`' if rc.test_command else ''))
    lines.append('Make the change each repo needs and commit in each repo you change; the service runs each test '
                 'command and opens one pull request per changed repo, linked to each other. Then report as usual.')
    return '\n'.join(lines)


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
    # Copilot picks the reasoning effort itself for its 'auto' model and refuses a session that sets one.
    reasoning = [] if model == 'auto' else ['--reasoning-effort', 'none' if effort == 'off' else effort]
    # Copilot refuses to start when an --add-dir directory is missing, and nothing has been attached yet on a new setup.
    os.makedirs(attachments_dir(cfg), exist_ok=True)
    return [shutil.which(cfg.copilot) or cfg.copilot, '--acp', '--stdio', '--model', model,
            *reasoning, '--allow-all-tools', '--disable-builtin-mcps',
            '--no-ask-user', '--no-custom-instructions', '--no-auto-update',
            *[a for s in ticket.skills for a in ('--add-dir', s)], '--add-dir', attachments_dir(cfg)]


def attachments_dir(cfg):
    """Where the files people attach to requests and answers are kept; a Copilot worker may read it (--add-dir)."""
    return os.path.join(cfg.state_dir, 'attachments')


def attachments_note(paths):
    """The lines that tell a worker which files came with its message."""
    if not paths:
        return ''
    lines = ['Files attached to this message (read them; images are also shown to you):']
    for p in paths:
        try:
            size = f'{os.path.getsize(p) / 1024:,.0f} KB'
        except OSError:
            size = 'missing'
        lines.append(f'- {p} ({size})')
    return '\n'.join(lines)


def image_blocks(paths):
    """The attached images as model input ({type: image, data: base64, mimeType}), as many as fit MAX_IMAGE_BYTES;
    other files and unreadable ones are left to the worker to open from attachments_note."""
    blocks, total = [], 0
    for p in paths:
        mime = IMAGE_TYPES.get(os.path.splitext(p)[1].lower())
        if not mime:
            continue
        try:
            with open(p, 'rb') as f:
                data = f.read()
        except OSError:
            continue
        if total + len(data) > MAX_IMAGE_BYTES:
            break
        total += len(data)
        blocks.append({'type': 'image', 'data': base64.b64encode(data).decode(), 'mimeType': mime})
    return blocks


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
        self.new_session = False  # after start: the worker's conversation is new, so it has never seen the request

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
        had_pi_session = bool(pi_session_files(cfg.state_dir, ticket.id))
        self.rpc = spawn()
        await self.rpc.start(argv, cwd, env)
        # Pi resumes the Ticket's session file when there is one; Copilot says whether it had to open a new session.
        self.new_session = bool(getattr(self.rpc, 'fresh', False)) if cfg.agent == 'copilot' else not had_pi_session

    async def run(self, message, timeout, images=()):
        """`images`: image_blocks for the model, sent with this message."""
        if self.queued:
            message = followup_message([{'text': t} for t in self.queued]) + '\n\n' + message
            self.queued = []
        self.running = True
        try:
            return await self.rpc.run(message, timeout, **({'images': list(images)} if images else {}))
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


def push_refused_message(branch, error, attempt, max_attempts):
    """Sent to the same session when GitHub refused to push its commits onto an existing branch."""
    return (f'You reported ready, but GitHub refused the push to `{branch}`: it is not a fast-forward. The branch '
            'on GitHub has commits your history does not contain (someone pushed to it, or you rebased, amended or '
            'squashed published commits). The service never force-pushes an existing branch.\n'
            f'The service has fetched `origin/{branch}` again. Run `git merge origin/{branch}`, resolve any '
            'conflicts so your changes are kept, run the tests, commit, and report ready again. Do not rebase, '
            'amend, squash or reset: every commit on the remote branch must stay in your history '
            f'(fix attempt {attempt} of {max_attempts}; then the Ticket is reported blocked).\n'
            f'GitHub said:\n```\n{error[-TEST_TAIL:]}\n```')


def followup_message(inputs):
    return ('New messages on the request thread (context, not new authority):\n'
            + json.dumps({'followups': _texts(inputs)}, indent=2, ensure_ascii=False))


def recovery_note():
    return ('The service restarted while you were working. Before repeating any outside action, check your '
            'directory, git log and the thread to see what already happened, then continue and report.')
