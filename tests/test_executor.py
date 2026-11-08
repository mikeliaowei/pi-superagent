import asyncio
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

from superagent import executor, report
from superagent.config import Config
from superagent.model import Ticket
from superagent.pi_rpc import PiRpc, RpcError

FAKE = os.path.join(os.path.dirname(__file__), 'fake_pi.py')


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


class PrepareWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = cfg(self.tmp, base_branch='dev')
        self.calls = []

    def run_ok(self, branch_exists=False, fail=None):
        def run(cmd, **kw):
            self.calls.append((cmd, kw.get('cwd')))
            code = 0
            if cmd[1:3] == ['rev-parse', '--verify']:
                code = 0 if branch_exists else 1
            if fail and fail in cmd:
                return subprocess.CompletedProcess(cmd, 128, '', 'fatal: no such remote')
            return subprocess.CompletedProcess(cmd, code, '', '')
        return run

    def test_fetches_and_adds_a_worktree_on_a_new_branch(self):
        path, branch = executor.prepare_worktree(self.cfg, ticket(), run=self.run_ok())
        self.assertEqual((path, branch), (os.path.join(self.cfg.work_dir, 'fix-bug'), 'agent/fix-bug'))
        cmds = [c for c, _ in self.calls]
        self.assertEqual(cmds[0], ['git', 'fetch', 'origin', 'dev'])
        self.assertEqual(cmds[-1], ['git', 'worktree', 'add', '-b', 'agent/fix-bug', path, 'origin/dev'])
        self.assertTrue(all(cwd == self.cfg.checkout for _, cwd in self.calls))

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
        self.assertEqual(seen, [('tk-abc', {'status': 'ready', 'summary': 'done', 'question': ''})])

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
                     {'status': 'ready', 'summary': 'x' * 4001}, {'status': 'waiting', 'summary': 'x'}):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.seen, [])

    def test_good_report_calls_on_report(self):
        code, body = self.post({'status': 'waiting', 'summary': 'stuck', 'question': 'Which API?'})
        self.assertEqual((code, body['ok']), (200, True))
        self.assertEqual(self.seen, [('tk-abc', {'status': 'waiting', 'summary': 'stuck', 'question': 'Which API?'})])
        self.assertTrue(self.srv.url.startswith('http://127.0.0.1:'))


if __name__ == '__main__':
    unittest.main()
