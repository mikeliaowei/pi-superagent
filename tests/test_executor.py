import asyncio
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from superagent import executor, report, worker_log
from superagent.config import Config
from superagent.copilot_acp import CopilotAcp, offered_models
from superagent.github import GIT_HARDEN
from superagent.model import Ticket
from superagent.pi_rpc import PiRpc, RpcError

FAKE = os.path.join(os.path.dirname(__file__), 'fake_pi.py')
FAKE_COPILOT = os.path.join(os.path.dirname(__file__), 'fake_copilot.py')


def process_alive(pid):
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; it has exited.
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


def cfg(tmp, **kw):
    return Config(repo='o/r', agent_login='bot', allowed=['alice'], checkout=os.path.join(tmp, 'checkout'),
                  state_dir=tmp, **kw)


def ticket(**kw):
    base = dict(id='tk-abc', name='fix-bug', title='Fix the bug', thread_key='github:o/r#1', repo='o/r', number=1,
                mandate={'actor': 'alice'}, created_at='2026-11-08T12:00:00+00:00', token='secret-token')
    return Ticket(**{**base, **kw})


async def fake(mode, session='s-1'):
    rpc = PiRpc()
    await rpc.start([sys.executable, FAKE, '--session-id', session], cwd=None,
                    env={**os.environ, 'FAKE_PI_MODE': mode})
    return rpc


class FakeSpawn(PiRpc):
    """Runs the fake script in place of the configured pi binary, keeping every other argument."""

    async def start(self, argv, cwd, env):
        self.argv, self.env = argv, env
        await super().start([sys.executable, FAKE, *argv[1:]], cwd, env)


class PiRpcTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_returns_response_data(self):
        rpc = await fake('ok', session='tk-9')
        try:
            self.assertEqual((await rpc.request('get_state'))['sessionId'], 'tk-9')
            with self.assertRaisesRegex(RpcError, 'unknown command'):
                await rpc.request('nonsense')
        finally:
            await rpc.close()

    async def test_run_returns_final_text_only_after_agent_settled(self):
        rpc = await fake('ok', session='tk-9')
        try:
            r = await rpc.run('do it', timeout=10)
        finally:
            await rpc.close()
        # 'first' ended an agent_end; the run kept going until agent_settled.
        self.assertEqual(r.text, 'final tk-9 : do it')
        self.assertTrue(r.settled)
        self.assertEqual(r.error, '')
        self.assertFalse(r.aborted)

    async def test_stop_reason_error_is_a_failed_run(self):
        rpc = await fake('error')
        try:
            r = await rpc.run('do it', timeout=10)
        finally:
            await rpc.close()
        self.assertEqual(r.error, 'provider exploded')
        self.assertTrue(r.settled)
        self.assertEqual(r.text, '')

    async def test_dialog_is_answered_cancelled_and_run_finishes(self):
        rpc = await fake('dialog')
        try:
            r = await rpc.run('do it', timeout=10)
        finally:
            await rpc.close()
        self.assertEqual(r.text, 'dialog cancelled=True')
        self.assertTrue(r.settled)
        self.assertEqual([d['id'] for d in rpc.cancelled_dialogs], ['d1'])  # notify needs no answer

    async def test_timeout_aborts_the_run(self):
        rpc = await fake('hang')
        try:
            r = await rpc.run('do it', timeout=0.5)
            state = await rpc.request('get_state')
        finally:
            await rpc.close()
        self.assertEqual(r.error, 'timeout')
        self.assertTrue(r.aborted)
        self.assertFalse(state['isStreaming'])  # the abort reached Pi

    async def test_crash_fails_the_waiting_run_with_exit_info(self):
        rpc = await fake('crash')
        try:
            r = await rpc.run('do it', timeout=10)
            with self.assertRaisesRegex(RpcError, 'pi exited'):
                await rpc.request('get_state')
        finally:
            await rpc.close()
        self.assertIn('pi exited (code 3)', r.error)
        self.assertIn('fatal: worker blew up', r.error)
        self.assertFalse(r.settled)

    async def test_handled_disposition_returns_immediately(self):
        rpc = await fake('handled')
        try:
            r = await asyncio.wait_for(rpc.run('/cmd', timeout=10), 2)
        finally:
            await rpc.close()
        self.assertEqual((r.text, r.error, r.settled, r.aborted), ('', '', False, False))

    async def test_close_ends_the_process(self):
        rpc = await fake('ok')
        await rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)

    async def spawn_with_child(self, worker_exits_on_eof):
        """A worker whose tool started a long-running process: it writes that child's pid, then either exits when
        its stdin closes or keeps running."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pidfile = os.path.join(tmp, 'child.pid')
        script = (
            'import subprocess, sys, time\n'
            'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
            f'open({pidfile!r}, "w").write(str(c.pid))\n'
            + ('sys.stdin.read()\n' if worker_exits_on_eof else 'time.sleep(120)\n'))
        rpc = PiRpc()
        await rpc.start([sys.executable, '-c', script], cwd=None, env=dict(os.environ))
        for _ in range(200):
            if os.path.exists(pidfile) and open(pidfile).read():
                break
            await asyncio.sleep(0.05)
        return rpc, int(open(pidfile).read())

    async def assert_gone(self, pid):
        for _ in range(100):
            if not process_alive(pid):
                return
            await asyncio.sleep(0.05)
        self.fail(f'process {pid} is still running')

    async def test_stopping_a_stuck_worker_ends_the_processes_its_tools_started(self):
        rpc, child = await self.spawn_with_child(worker_exits_on_eof=False)
        await rpc.close(grace=0.5)
        self.assertIsNotNone(rpc.proc.returncode)
        await self.assert_gone(child)

    @unittest.skipIf(os.name == 'nt', 'a Windows process tree loses children whose parent already exited')
    async def test_a_worker_that_exits_leaves_no_process_behind(self):
        rpc, child = await self.spawn_with_child(worker_exits_on_eof=True)
        await rpc.close()
        self.assertIsNotNone(rpc.proc.returncode)
        await self.assert_gone(child)


class WorkerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'work', 'fix-bug'))
        self.cfg = cfg(self.tmp)

    async def start(self, mode):
        w = executor.Worker()
        with mock.patch.dict(os.environ, {'FAKE_PI_MODE': mode}):
            await w.start(self.cfg, ticket(), 'http://127.0.0.1:1', spawn=FakeSpawn)
        return w

    async def test_worker_runs_with_ticket_session_and_env(self):
        w = await self.start('ok')
        try:
            r = await w.run('hello', timeout=10)
        finally:
            await w.stop()
        self.assertEqual(r.text, 'final tk-abc tk-abc: hello')
        self.assertEqual(w.rpc.argv, executor.pi_argv(self.cfg, ticket()))

    async def test_send_steers_a_running_session(self):
        w = await self.start('hang')
        try:
            run = asyncio.create_task(w.run('work', timeout=10))
            while not w.running:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            self.assertEqual(await w.send('also this'), 'steered')
            r = await run
        finally:
            await w.stop()
        self.assertEqual(r.text, 'steer: also this')

    async def test_send_while_idle_queues_for_the_next_run(self):
        w = await self.start('ok')
        try:
            self.assertEqual(await w.send('later note'), 'queued')
            r = await w.run('next', timeout=10)
        finally:
            await w.stop()
        self.assertIn('next', r.text)
        self.assertIn('later note', r.text)
        self.assertEqual(w.queued, [])


class ArgvEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg(self.tmp, provider='anthropic', model='claude-x', tools='read,bash')

    def test_pi_argv_is_explicit_and_isolated(self):
        argv = executor.pi_argv(self.cfg, ticket())

        def opt(flag):
            return argv[argv.index(flag) + 1]

        self.assertEqual(argv[0], self.cfg.pi)
        self.assertEqual((opt('--mode'), opt('--provider'), opt('--model')), ('rpc', 'anthropic', 'claude-x'))
        self.assertEqual((opt('--session-id'), opt('--name'), opt('--tools')), ('tk-abc', 'fix-bug', 'read,bash'))
        self.assertEqual(opt('--session-dir'), os.path.join(self.tmp, 'sessions'))
        for flag in ('-ne', '-ns', '-np', '-nc', '--no-approve', '--offline'):
            self.assertIn(flag, argv)
        self.assertTrue(os.path.isfile(os.path.join(opt('--skill'), 'SKILL.md')))
        self.assertTrue(os.path.isfile(opt('--append-system-prompt')))
        with open(os.path.join(opt('--skill'), 'SKILL.md')) as f:
            self.assertIn('name: superagent-worker', f.read())

    def test_pi_argv_uses_the_requests_model_and_thinking_else_the_configs(self):
        def opts(t):
            argv = executor.pi_argv(self.cfg, t)
            return tuple(argv[argv.index(f) + 1] for f in ('--provider', '--model', '--thinking'))

        self.assertEqual(opts(ticket()), ('anthropic', 'claude-x', self.cfg.thinking))
        self.assertEqual(opts(ticket(model='github-copilot/claude-opus-5.5', thinking='high')),
                         ('github-copilot', 'claude-opus-5.5', 'high'))
        self.assertEqual(opts(ticket(model='claude-y')), ('anthropic', 'claude-y', self.cfg.thinking))
        self.assertEqual(opts(ticket(model='openrouter/anthropic/claude-z')),
                         ('openrouter', 'anthropic/claude-z', self.cfg.thinking))

    def test_worker_env_strips_secrets_and_adds_report_vars(self):
        base = {'PATH': '/usr/bin', 'SUPERAGENT_GITHUB_TOKEN': 'x', 'GH_TOKEN': 'y', 'GITHUB_TOKEN': 'z', 'HOME': '/h'}
        with mock.patch.dict(os.environ, base, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        for k in ('SUPERAGENT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
            self.assertNotIn(k, env)
        self.assertEqual(env['HOME'], '/h')
        self.assertEqual(env['SUPERAGENT_URL'], 'http://127.0.0.1:9')
        self.assertEqual(env['SUPERAGENT_TICKET_ID'], 'tk-abc')
        self.assertEqual(env['SUPERAGENT_TICKET_TOKEN'], 'secret-token')
        self.assertEqual(env['PI_SKIP_VERSION_CHECK'], '1')
        self.assertEqual(env['PATH'], os.path.dirname(sys.executable) + os.pathsep + '/usr/bin')

    @mock.patch.object(executor, 'WINDOWS', False)  # the Windows values: tests/test_windows.py
    def test_worker_env_turns_off_git_credential_helpers_and_prompts(self):
        base = {'PATH': '/usr/bin', 'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'user.name',
                'GIT_CONFIG_VALUE_0': 'w', 'GIT_CONFIG_KEY_1': 'user.email', 'GIT_CONFIG_VALUE_1': 'w@x'}
        with mock.patch.dict(os.environ, base, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        self.assertEqual(env['GIT_CONFIG_COUNT'], '3')
        self.assertEqual((env['GIT_CONFIG_KEY_0'], env['GIT_CONFIG_KEY_1']), ('user.name', 'user.email'))
        self.assertEqual((env['GIT_CONFIG_KEY_2'], env['GIT_CONFIG_VALUE_2']), ('credential.helper', ''))
        self.assertEqual((env['GIT_TERMINAL_PROMPT'], env['GIT_ASKPASS'], env['SSH_ASKPASS']),
                         ('0', '/usr/bin/false', '/usr/bin/false'))
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin'}, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        self.assertEqual((env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_0'], env['GIT_CONFIG_VALUE_0']),
                         ('1', 'credential.helper', ''))

    def test_worker_cannot_push_with_the_users_credential_helper(self):
        tmp = self.tmp
        helper = os.path.join(tmp, 'helper.sh')
        log = os.path.join(tmp, 'helper.log')
        # Forward slashes: Git for Windows runs the helper through its sh, and a config file treats \ as an escape.
        with open(helper, 'w') as f:
            f.write(f'#!/bin/sh\necho called >> "{log.replace(os.sep, "/")}"\necho username=u; echo password=p\n')
        os.chmod(helper, 0o755)
        gitconfig = os.path.join(tmp, 'gitconfig')
        with open(gitconfig, 'w') as f:
            f.write(f'[credential]\n\thelper = {helper.replace(os.sep, "/")}\n')
        with mock.patch.dict(os.environ, {'PATH': os.environ['PATH'], 'GIT_CONFIG_GLOBAL': gitconfig,
                                          'GIT_CONFIG_NOSYSTEM': '1'}, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        r = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=example.com\n\n', env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertFalse(os.path.exists(log))
        self.assertNotIn('password=p', r.stdout)


class PrepareWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg(self.tmp, base_branch='dev')
        self.calls, self.envs = [], []

    def run_ok(self, branch_exists=False, fail=None, origin_url=''):
        def run(cmd, **kw):
            if cmd[1].startswith('--git-dir='):  # the fresh repo of a fetch from GitHub
                self.calls.append((cmd, kw.get('cwd')))
                return subprocess.CompletedProcess(cmd, 0, '', '')
            self.assertEqual(cmd[1:1 + len(GIT_HARDEN)], GIT_HARDEN)
            self.envs.append(kw.get('env'))
            cmd = [cmd[0], *cmd[1 + len(GIT_HARDEN):]]
            self.calls.append((cmd, kw.get('cwd')))
            code = 0
            if cmd[1:3] == ['rev-parse', '--verify']:
                code = 0 if branch_exists else 1
            if cmd[-1] == 'remote.origin.url':
                return subprocess.CompletedProcess(cmd, 0, origin_url, '')
            if fail and fail in cmd:
                return subprocess.CompletedProcess(cmd, 128, '', 'fatal: no such remote')
            return subprocess.CompletedProcess(cmd, code, '', '')
        return run

    def test_fetches_and_adds_a_worktree_on_a_new_branch(self):
        path, branch = executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok())
        self.assertEqual((path, branch), (os.path.join(self.cfg.work_dir, 'fix-bug'), 'agent/fix-bug'))
        cmds = [c for c, _ in self.calls]
        self.assertEqual(cmds[:2], [['git', 'config', '--get', 'remote.origin.url'], ['git', 'fetch', 'origin', 'dev']])
        self.assertEqual(cmds[-1], ['git', 'worktree', 'add', '-b', 'agent/fix-bug', path, 'origin/dev'])
        self.assertTrue(all(cwd == self.cfg.checkout for _, cwd in self.calls))

    def test_git_runs_without_secrets_hooks_or_inherited_config(self):
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': 'tok-x', 'GH_TOKEN': 'gh-y',
                                          'GIT_CONFIG_PARAMETERS': "'core.hooksPath'='/evil'"}):
            executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok())
        self.assertTrue(self.envs)
        for env in self.envs:
            self.assertNotIn('SUPERAGENT_GITHUB_TOKEN', env)
            self.assertNotIn('GH_TOKEN', env)
            self.assertNotIn('GIT_CONFIG_PARAMETERS', env)
            self.assertFalse(any('tok-x' in v for v in env.values()))
            self.assertEqual((env['GIT_CONFIG_NOSYSTEM'], env['GIT_CONFIG_GLOBAL'], env['GIT_TERMINAL_PROMPT']),
                             ('1', os.devnull, '0'))

    def test_existing_worktree_is_reused(self):
        path = os.path.join(self.cfg.work_dir, 'fix-bug')
        os.makedirs(path)
        open(os.path.join(path, '.git'), 'w').close()
        self.assertEqual(executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok()), (path, 'agent/fix-bug'))
        self.assertEqual(self.calls, [])

    def test_existing_branch_is_checked_out_without_recreating_it(self):
        path, _ = executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok(branch_exists=True))
        self.assertEqual(self.calls[-1][0], ['git', 'worktree', 'add', path, 'agent/fix-bug'])

    def test_git_failure_raises_with_stderr(self):
        with self.assertRaisesRegex(executor.PrepareError, 'no such remote'):
            executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok(fail='fetch'))

    def test_an_origin_on_github_is_fetched_with_the_token_through_a_fresh_repo(self):
        # A service started outside VS Code has no credential helper, so an anonymous fetch of a private repo fails.
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': 'tok-x'}):
            path, _ = executor.prepare_worktree(self.cfg, ticket(),
                                                run=self.run_ok(origin_url='https://github.com/O/r.git\n'))
        cmds = [c for c, _ in self.calls]
        remote = next(c for c in cmds if 'https://github.com/o/r.git' in c)
        self.assertEqual(remote[-1], '+refs/heads/dev:refs/heads/dev')
        self.assertNotIn(['git', 'fetch', 'origin', 'dev'], cmds)
        self.assertEqual(cmds[-1], ['git', 'worktree', 'add', '-b', 'agent/fix-bug', path, 'origin/dev'])
        self.assertFalse(any('tok-x' in ' '.join(c) for c in cmds))

    def test_an_origin_on_github_without_a_token_is_fetched_as_before(self):
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': ''}):
            executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok(origin_url='https://github.com/o/r.git'))
        self.assertIn(['git', 'fetch', 'origin', 'dev'], [c for c, _ in self.calls])

    def test_fetching_every_branch_prunes_from_github_or_from_another_origin(self):
        repo = self.cfg.repo_config('o/r')
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': 'tok-x'}):
            self.assertEqual(executor.fetch_origin(self.cfg, repo, '*', self.run_ok(origin_url='/srv/r.git')), '')
            self.assertEqual(self.calls[-1][0], ['git', 'fetch', '--prune', '--no-tags', 'origin'])
            executor.fetch_origin(self.cfg, repo, '*', self.run_ok(origin_url='https://github.com/o/r'))
        self.assertEqual(self.calls[-2][0][-1], '+refs/heads/*:refs/heads/*')
        self.assertIn('--prune', self.calls[-1][0])
        self.assertEqual(executor.fetch_origin(self.cfg, repo, '*', self.run_ok(fail='fetch')), 'fatal: no such remote')


class MessagesTest(unittest.TestCase):
    def test_first_message_carries_ticket_and_capped_texts(self):
        msg = executor.first_message(ticket(), 'x' * 5000, [{'text': 'y' * 2000}])
        self.assertIn('tk-abc', msg)
        self.assertIn('Fix the bug', msg)
        self.assertIn('x' * 1400, msg)
        self.assertNotIn('x' * 1501, msg)
        self.assertIn('y' * 500, msg)
        self.assertNotIn('y' * 601, msg)

    def test_followup_message_caps_each_input(self):
        msg = executor.followup_message([{'text': 'a' * 900}, {'text': 'short'}])
        self.assertNotIn('a' * 601, msg)
        self.assertIn('short', msg)

    def test_recovery_note_warns_before_repeating_outside_actions(self):
        note = executor.recovery_note()
        for word in ('directory', 'git log', 'thread', 'repeating'):
            self.assertIn(word, note)


class ReportCliTest(unittest.TestCase):
    def call(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
            try:
                code = report.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_posts_report_with_bearer_token(self):
        seen = []
        srv = report.ReportServer(lambda tok: 'tk-abc' if tok == 'secret' else None,
                                  lambda tid, rep: seen.append((tid, rep)), port=0)
        try:
            code, out, _ = self.call(['--status', 'ready', '--summary', 'done'],
                                     {'SUPERAGENT_URL': srv.url, 'SUPERAGENT_TICKET_TOKEN': 'secret'})
        finally:
            srv.close()
        self.assertEqual(code, 0)
        self.assertIn('ready', out)
        self.assertEqual(seen, [('tk-abc', {'status': 'ready', 'summary': 'done', 'question': '', 'details': ''})])

    def test_details_file_is_sent_with_the_report(self):
        seen = []
        srv = report.ReportServer(lambda tok: 'tk-abc', lambda tid, rep: seen.append(rep), port=0)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, 'pr.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('### Problem\n\nNav was hidden.\n')
        try:
            code, _, _ = self.call(['--status', 'ready', '--summary', 'done', '--details-file', path],
                                   {'SUPERAGENT_URL': srv.url, 'SUPERAGENT_TICKET_TOKEN': 't'})
        finally:
            srv.close()
        self.assertEqual(code, 0)
        self.assertEqual(seen[0]['details'], '### Problem\n\nNav was hidden.')

    def test_missing_details_file_exits_2(self):
        code, _, err = self.call(['--status', 'ready', '--summary', 'done', '--details-file',
                                  os.path.join(tempfile.gettempdir(), 'superagent-no-such-file.md')],
                                 {'SUPERAGENT_URL': 'http://127.0.0.1:1', 'SUPERAGENT_TICKET_TOKEN': 't'})
        self.assertEqual(code, 2)
        self.assertIn('--details-file', err)

    def test_missing_env_exits_2(self):
        code, _, err = self.call(['--status', 'ready', '--summary', 'done'], {})
        self.assertEqual(code, 2)
        self.assertIn('SUPERAGENT_URL', err)

    def test_waiting_requires_a_question(self):
        code, _, err = self.call(['--status', 'waiting', '--summary', 'need input'],
                                 {'SUPERAGENT_URL': 'http://127.0.0.1:1', 'SUPERAGENT_TICKET_TOKEN': 't'})
        self.assertEqual(code, 2)
        self.assertIn('--question', err)

    def test_http_error_exits_1(self):
        srv = report.ReportServer(lambda tok: None, lambda tid, rep: None, port=0)
        try:
            code, _, err = self.call(['--status', 'ready', '--summary', 'done'],
                                     {'SUPERAGENT_URL': srv.url, 'SUPERAGENT_TICKET_TOKEN': 'bad'})
        finally:
            srv.close()
        self.assertEqual(code, 1)
        self.assertIn('401', err)


class ReportServerTest(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.srv = report.ReportServer(lambda tok: 'tk-abc' if tok == 'good' else None,
                                       lambda tid, rep: self.seen.append((tid, rep)), port=0)

    def tearDown(self):
        self.srv.close()

    def post(self, body, token='good'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.srv.url + '/report', data=data, method='POST',
                                     headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bad_token_is_401(self):
        self.assertEqual(self.post({'status': 'ready', 'summary': 'x'}, token='nope')[0], 401)
        self.assertEqual(self.seen, [])

    def test_bad_body_is_400(self):
        for body in (b'not json', {'status': 'done', 'summary': 'x'}, {'status': 'ready', 'summary': ''},
                     {'status': 'ready', 'summary': 'x' * 4001}, {'status': 'waiting', 'summary': 'x'},
                     {'status': 'ready', 'summary': 'x', 'details': 5},
                     {'status': 'ready', 'summary': 'x', 'details': 'x' * 16001}):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.seen, [])

    def raw_post(self, length, body=b'{}'):
        conn = http.client.HTTPConnection('127.0.0.1', self.srv.httpd.server_address[1], timeout=3)
        try:
            conn.putrequest('POST', '/report')
            conn.putheader('Authorization', 'Bearer good')
            conn.putheader('Content-Length', length)
            conn.endheaders()
            conn.send(body)
            return conn.getresponse().status
        finally:
            conn.close()

    def test_bad_content_length_is_400_before_reading(self):
        for length in ('abc', '-1'):
            self.assertEqual(self.raw_post(length), 400, length)
        self.assertEqual(self.seen, [])

    def test_report_for_an_assignment_that_is_not_running_is_409(self):
        def refuse(tid, rep):
            raise report.NotRunning(f'{tid} is not running')
        self.srv.on_report = refuse
        code, body = self.post({'status': 'ready', 'summary': 'late'})
        self.assertEqual((code, body['ok']), (409, False))

    def test_good_report_calls_on_report(self):
        code, body = self.post({'status': 'waiting', 'summary': 'stuck', 'question': 'Which API?'})
        self.assertEqual((code, body['ok']), (200, True))
        self.assertEqual(self.seen, [('tk-abc', {'status': 'waiting', 'summary': 'stuck', 'question': 'Which API?',
                                                 'details': ''})])
        self.assertTrue(self.srv.url.startswith('http://127.0.0.1:'))


class FakeCopilot(CopilotAcp):
    """Runs tests/fake_copilot.py in place of the copilot binary, keeping every other argument."""

    async def start(self, argv, cwd, env):
        self.argv, self.env = argv, env
        await super().start([sys.executable, FAKE_COPILOT, *argv[1:]], cwd, env)


class CopilotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'work', 'fix-bug'))
        self.fake_state = os.path.join(self.tmp, 'fake-copilot')
        os.makedirs(self.fake_state)
        self.cfg = cfg(self.tmp, agent='copilot', copilot='copilot', model='claude-sonnet-5.5', thinking='off')

    async def start(self, mode, t=None):
        t = t or ticket()
        w = executor.Worker()
        def spawn():
            return FakeCopilot(worker_log.copilot_session_file(self.tmp, t.id), executor.copilot_preamble(t))
        with mock.patch.dict(os.environ, {'FAKE_COPILOT_MODE': mode, 'FAKE_COPILOT_STATE': self.fake_state}):
            await w.start(self.cfg, t, 'http://127.0.0.1:1', spawn=spawn)
        return w

    def turns(self, sid='s1'):
        with open(os.path.join(self.fake_state, sid)) as f:
            return f.read().splitlines()

    async def test_a_new_session_gets_the_worker_prompt_once_and_a_later_process_resumes_it(self):
        w = await self.start('ok')
        try:
            r = await w.run('hello', timeout=10)
            r2 = await w.run('again', timeout=10)
        finally:
            await w.stop()
        self.assertEqual((r.text, r.error, r.settled), ('turns 1: hello', '', True))  # the last message only
        self.assertEqual(r2.text, 'turns 2: again')
        self.assertEqual(w.rpc.env['COPILOT_HOME'], os.path.join(self.tmp, 'copilot-home'))  # not ~/.copilot
        first, second = self.turns()
        self.assertIn('You are a superagent worker', first)
        self.assertIn('superagent-report --status', first)  # the worker skill, inlined
        self.assertNotIn('superagent worker', second)
        self.assertEqual(worker_log.session_files(self.tmp, 'tk-abc'), [])  # no Copilot events folder here
        with open(worker_log.copilot_session_file(self.tmp, 'tk-abc')) as f:
            self.assertEqual(f.read(), 's1')
        w = await self.start('ok')  # a retry or an answer: a new process
        try:
            r = await w.run('answer', timeout=10)
        finally:
            await w.stop()
        self.assertEqual(r.text, 'turns 3: answer')  # same session; the replayed history is not the result
        self.assertNotIn('superagent worker', self.turns()[2])

    async def test_a_lost_session_starts_a_new_one_with_the_prompt(self):
        os.makedirs(os.path.join(self.tmp, 'copilot-sessions'))
        with open(worker_log.copilot_session_file(self.tmp, 'tk-abc'), 'w') as f:
            f.write('gone')
        w = await self.start('ok')
        try:
            await w.run('hello', timeout=10)
        finally:
            await w.stop()
        self.assertIn('You are a superagent worker', self.turns()[0])

    async def test_a_message_during_a_run_is_queued_and_a_timeout_cancels_the_run(self):
        w = await self.start('hang')
        try:
            run = asyncio.create_task(w.run('work', timeout=1))
            while not w.running:
                await asyncio.sleep(0.01)
            self.assertEqual(await w.send('also this'), 'queued')  # copilot cannot be steered
            r = await run
        finally:
            await w.stop()
        self.assertEqual((r.error, r.aborted), ('timeout', True))
        self.assertEqual(w.queued, ['also this'])

    async def test_permission_requests_are_allowed_and_a_crash_is_an_error(self):
        w = await self.start('permission')
        try:
            r = await w.run('work', timeout=10)
        finally:
            await w.stop()
        self.assertEqual(r.text, 'permission yes')
        w = await self.start('crash')
        try:
            r = await w.run('work', timeout=10)
        finally:
            await w.stop()
        self.assertRegex(r.error, '^copilot exited')

    def test_argv_keeps_the_users_setup_and_github_login_out(self):
        t = ticket(skills=['/skills/a'], thinking='high', model='github-copilot/gpt-6')
        argv = executor.copilot_argv(self.cfg, t)
        self.assertEqual(argv[1:5], ['--acp', '--stdio', '--model', 'gpt-6'])
        for flag in ('--disable-builtin-mcps', '--no-custom-instructions', '--allow-all-tools', '--no-ask-user'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--reasoning-effort') + 1], 'high')
        self.assertEqual(argv[-2:], ['--add-dir', '/skills/a'])
        argv = executor.copilot_argv(self.cfg, ticket())
        self.assertEqual((argv[argv.index('--model') + 1], argv[argv.index('--reasoning-effort') + 1]),
                         ('claude-sonnet-5.5', 'none'))
        self.assertIn('/skills/a/SKILL.md', executor.copilot_preamble(t).replace(os.sep, '/'))

    def test_the_auto_model_gets_no_reasoning_effort(self):
        # Copilot refuses the session: 'Reasoning effort is not supported when using the "auto" model.'
        for thinking in ('high', 'off'):
            argv = executor.copilot_argv(self.cfg, ticket(model='auto', thinking=thinking))
            self.assertEqual(argv[argv.index('--model') + 1], 'auto')
            self.assertNotIn('--reasoning-effort', argv)


class CopilotModelsTest(unittest.TestCase):
    def test_the_enabled_models_come_with_their_premium_request_cost(self):
        state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state, True)
        env = {**os.environ, 'FAKE_COPILOT_STATE': state}
        self.assertEqual(offered_models([sys.executable, FAKE_COPILOT], state, env),
                         [('auto', 'Let Copilot pick'), ('gpt-6', '1x premium requests'),
                          ('opus', '15x premium requests')])  # 'off' is disabled by policy
        self.assertEqual(offered_models([os.path.join(state, 'no-copilot')], state, env), [])


if __name__ == '__main__':
    unittest.main()
