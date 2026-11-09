"""Native Windows code paths (S16), exercised on any OS by forcing the module-level WINDOWS flags and mocking the
processes they start. The real thing runs in the windows-latest CI job."""

import asyncio
import io
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from superagent import cli, executor
from superagent import config as config_mod
from superagent import github as gh_mod
from superagent.config import Config
from superagent.model import Ticket

CREATE_NEW_PROCESS_GROUP = 0x00000200  # the Win32 value; subprocess only defines it on Windows


def make_cfg(tmp, **kw):
    return Config(repo='o/r', agent_login='bot', allowed=['alice'], checkout=os.path.join(tmp, 'checkout'),
                  state_dir=tmp, **kw)


def ticket():
    return Ticket(id='tk-abc', name='fix-bug', title='Fix the bug', thread_key='github:o/r#1', repo='o/r', number=1,
                  mandate={'actor': 'alice'}, created_at='2026-11-08T12:00:00+00:00', token='secret-token')


class FakePopen:
    """Stands in for the test command's process: writes `output` to its stdout file; `wait(timeout)` raises
    TimeoutExpired when `hang` is set, until the process is killed."""

    def __init__(self, output=b'', code=0, hang=False):
        self.output, self.code, self.hang = output, code, hang
        self.pid, self.argv, self.kw, self.killed = 4242, None, None, False

    def __call__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        kw['stdout'].write(self.output)
        return self

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired('cmd', timeout)
        return self.code


class TestCommandOnWindowsTest(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(executor, 'WINDOWS', True),
                      mock.patch.dict(os.environ, {'SystemRoot': r'C:\Windows'})):
            patch.start()
            self.addCleanup(patch.stop)
        self.kills = []

    def taskkill(self, popen):
        def run(argv, **kw):
            self.kills.append((argv, kw))
            popen.killed = True
            return subprocess.CompletedProcess(argv, 128)  # "process not found" must not raise
        return run

    def test_the_command_runs_through_cmd_in_a_new_process_group_and_output_is_captured(self):
        popen = FakePopen(b'collected 2 items\r\nall good\r\n', code=0)
        with mock.patch.object(executor.subprocess, 'Popen', popen), \
                mock.patch.object(executor.subprocess, 'run', self.taskkill(popen)):
            result = executor.TestRun('python -m pytest -q', '/w/t', {'PATH': 'x'}, timeout=5).wait()
        self.assertEqual(popen.argv, 'python -m pytest -q')  # a string: cmd.exe parses it
        self.assertIs(popen.kw['shell'], True)
        self.assertEqual(popen.kw['creationflags'] & CREATE_NEW_PROCESS_GROUP, CREATE_NEW_PROCESS_GROUP)
        self.assertNotIn('start_new_session', popen.kw)
        self.assertEqual((popen.kw['cwd'], popen.kw['env'], popen.kw['stdin'], popen.kw['stderr']),
                         ('/w/t', {'PATH': 'x'}, subprocess.DEVNULL, subprocess.STDOUT))
        self.assertEqual((result['passed'], result['code'], result['timed_out'], result['output']),
                         (True, 0, False, 'collected 2 items\nall good'))
        # Whatever the command left running is ended as on POSIX: the whole tree, once, after the exit.
        self.assertEqual([argv for argv, _ in self.kills],
                         [[os.path.join(r'C:\Windows', 'System32', 'taskkill.exe'), '/F', '/T', '/PID', '4242']])

    def test_a_timeout_kills_the_whole_process_tree(self):
        popen = FakePopen(b'started\n', code=1, hang=True)
        with mock.patch.object(executor.subprocess, 'Popen', popen), \
                mock.patch.object(executor.subprocess, 'run', self.taskkill(popen)):
            result = executor.TestRun('slow.cmd', '/w/t', {}, timeout=1).wait()
        self.assertTrue(self.kills)
        for argv, kw in self.kills:
            self.assertEqual(argv[1:], ['/F', '/T', '/PID', '4242'])
            self.assertEqual((kw['stdout'], kw['stderr']), (subprocess.DEVNULL, subprocess.DEVNULL))
        self.assertEqual((result['passed'], result['timed_out'], result['output']), (False, True, 'started'))

    def test_kill_ignores_a_missing_taskkill(self):
        run = executor.TestRun('x', '/w', {}, 1)
        run.proc = SimpleNamespace(pid=7)
        with mock.patch.object(executor.subprocess, 'run', side_effect=OSError('no taskkill')):
            run.kill()  # no exception


class WorkerCredentialsOnWindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_cfg(self.tmp)

    def test_no_stored_credentials_or_prompts_and_askpass_is_a_file_that_exists_and_fails(self):
        base = {'PATH': r'C:\Windows\System32', 'GIT_ASKPASS': r'C:\vscode\askpass.cmd'}
        with mock.patch.object(executor, 'WINDOWS', True), mock.patch.dict(os.environ, base, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        self.assertFalse(any('/usr/bin/false' in v for v in env.values()))
        self.assertEqual((env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_0'], env['GIT_CONFIG_VALUE_0']),
                         ('1', 'credential.helper', ''))
        self.assertEqual((env['GIT_TERMINAL_PROMPT'], env['GCM_INTERACTIVE']), ('0', 'never'))
        askpass = env['GIT_ASKPASS']
        self.assertEqual(env['SSH_ASKPASS'], askpass)
        self.assertEqual(askpass, os.path.join(self.tmp, 'deny-askpass.cmd'))
        with open(askpass) as f:
            self.assertEqual(f.read().strip(), '@exit /b 1')
        # The test command gets the same protection.
        with mock.patch.object(executor, 'WINDOWS', True), mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(executor.test_env(self.cfg)['GIT_ASKPASS'], askpass)

    def test_posix_values_are_unchanged(self):
        with mock.patch.object(executor, 'WINDOWS', False), mock.patch.dict(os.environ, {'PATH': '/bin'}, clear=True):
            env = executor.worker_env(self.cfg, ticket(), 'http://127.0.0.1:9')
        self.assertEqual((env['GIT_ASKPASS'], env['SSH_ASKPASS']), ('/usr/bin/false', '/usr/bin/false'))
        self.assertNotIn('GCM_INTERACTIVE', env)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'deny-askpass.cmd')))

    def test_secret_names_are_matched_without_case_like_windows_does(self):
        # Windows environment names ignore case (os.environ upper-cases them), so `token_env = "MyToken"` must still
        # keep MYTOKEN away from workers and the service's git commands.
        cfg = make_cfg(self.tmp, token_env='MyToken', secret_env=['gh_token'])
        base = {'MYTOKEN': 'tok-1', 'GH_TOKEN': 'tok-2', 'PATH': 'p'}
        with mock.patch.object(config_mod, 'WINDOWS', True):
            self.assertEqual(cfg.worker_env(base), {'PATH': 'p'})
        with mock.patch.object(gh_mod, 'WINDOWS', True):
            env = gh_mod.git_env({'MyToken', 'gh_token'}, base=base)
        self.assertNotIn('MYTOKEN', env)
        self.assertNotIn('GH_TOKEN', env)
        with mock.patch.object(config_mod, 'WINDOWS', False):  # POSIX names are case-sensitive: unchanged
            self.assertEqual(set(cfg.worker_env(base)), {'MYTOKEN', 'GH_TOKEN', 'PATH'})


class GitHardeningOnWindowsTest(unittest.TestCase):
    def test_hooks_point_below_a_system_file_where_no_hook_can_exist(self):
        with mock.patch.dict(os.environ, {'SystemRoot': r'C:\Windows'}):
            harden = gh_mod.git_harden(windows=True)
        self.assertEqual(harden, ['-c', 'core.hooksPath=C:/Windows/System32/cmd.exe', '-c', 'core.fsmonitor=false'])
        self.assertEqual(gh_mod.git_harden(windows=False),
                         ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false'])

    def test_the_push_env_keeps_what_windows_networking_needs_and_nothing_secret(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        base = {'PATH': 'p', 'SYSTEMROOT': r'C:\Windows', 'USERPROFILE': r'C:\Users\me', 'TEMP': 't',
                'SUPERAGENT_GITHUB_TOKEN': 'tok', 'APPDATA': 'a'}
        with mock.patch.object(gh_mod, 'WINDOWS', True), mock.patch.dict(os.environ, base, clear=True):
            gh_mod.push_branch('/w/t', 'agent/x', 'tok', 'o/r', os.path.join(tmp, 'push.git'), run=run)
        env = calls[-1][1]['env']
        self.assertEqual((env['SYSTEMROOT'], env['USERPROFILE'], env['TEMP'], env['PATH']),
                         (r'C:\Windows', r'C:\Users\me', 't', 'p'))
        self.assertNotIn('SUPERAGENT_GITHUB_TOKEN', env)
        self.assertNotIn('APPDATA', env)


class PanelOnWindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.args = SimpleNamespace(port=0, config=os.path.join(self.tmp, 'config.toml'))
        for patch in (mock.patch.object(cli, 'WINDOWS', True),
                      mock.patch.dict(sys.modules, {'streamlit': types.ModuleType('streamlit')})):
            patch.start()
            self.addCleanup(patch.stop)

    def panel(self, child):
        with mock.patch.object(cli.subprocess, 'Popen', return_value=child) as popen, \
                mock.patch.object(cli.os, 'execve') as execve, redirect_stderr(io.StringIO()):
            code = cli.cmd_panel(None, self.args)
        execve.assert_not_called()
        return code, popen

    def test_panel_runs_streamlit_as_a_child_and_returns_its_exit_code(self):
        child = mock.Mock()
        child.wait.return_value = 3
        code, popen = self.panel(child)
        self.assertEqual(code, 3)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], [sys.executable, '-m', 'streamlit', 'run'])
        self.assertEqual(argv[argv.index('--server.address') + 1], '127.0.0.1')
        self.assertEqual(popen.call_args.kwargs['env']['SUPERAGENT_CONFIG'], self.args.config)

    def test_ctrl_c_lets_streamlit_stop_by_itself(self):
        # Ctrl+C reaches every process in the console: the child stops on its own, the parent just waits for it.
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt, 0]
        code, _ = self.panel(child)
        self.assertEqual(code, 0)
        child.kill.assert_not_called()

    def test_a_child_that_ignores_ctrl_c_is_killed(self):
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt, subprocess.TimeoutExpired('streamlit', 10), 1]
        code, _ = self.panel(child)
        child.kill.assert_called_once()
        self.assertEqual(code, 1)


class RunStopsOnCtrlCTest(unittest.IsolatedAsyncioTestCase):
    async def test_without_loop_signal_handlers_ctrl_c_sets_the_stop_event(self):
        loop, stop = asyncio.get_running_loop(), asyncio.Event()
        installed = {}

        def fake_signal(sig, handler):
            installed[sig] = handler
            return signal.default_int_handler

        with mock.patch.object(loop, 'add_signal_handler', side_effect=NotImplementedError), \
                mock.patch.object(cli.signal, 'signal', side_effect=fake_signal):
            restore = cli.install_stop_handlers(loop, stop)
            self.assertIn(signal.SIGINT, installed)
            installed[signal.SIGINT](signal.SIGINT, None)
            await asyncio.sleep(0)
            self.assertTrue(stop.is_set())
            installed.clear()
            restore()
        self.assertIs(installed[signal.SIGINT], signal.default_int_handler)  # the previous handler is back

    async def test_with_loop_signal_handlers_nothing_else_is_installed(self):
        loop, stop = asyncio.get_running_loop(), asyncio.Event()
        with mock.patch.object(loop, 'add_signal_handler') as add, mock.patch.object(loop, 'remove_signal_handler'), \
                mock.patch.object(cli.signal, 'signal') as sig:
            restore = cli.install_stop_handlers(loop, stop)
            restore()
        self.assertEqual([c.args[0] for c in add.call_args_list], [signal.SIGINT, signal.SIGTERM])
        sig.assert_not_called()


class PiPathOnWindowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_missing_default_pi_falls_back_to_the_one_on_path(self):
        missing = os.path.join(self.tmp, 'pi')
        which = mock.Mock(return_value=r'C:\Users\me\AppData\Roaming\npm\pi.CMD')
        path, note = config_mod.resolve_pi(missing, default=True, windows=True, which=which)
        self.assertEqual(path, r'C:\Users\me\AppData\Roaming\npm\pi.CMD')
        self.assertIn('found on PATH', note)
        self.assertIn(missing, note)
        which.assert_called_once_with('pi')

    def test_an_extensionless_pi_uses_its_cmd_or_exe_sibling(self):
        base = os.path.join(self.tmp, 'pi')
        for name in ('pi', 'pi.cmd'):
            open(os.path.join(self.tmp, name), 'w').close()
        self.assertEqual(config_mod.resolve_pi(base, default=False, windows=True, which=mock.Mock()),
                         (base + '.cmd', ''))

    def test_an_explicit_pi_is_never_replaced_by_one_on_path(self):
        which = mock.Mock(return_value='C:/other/pi.exe')
        missing = os.path.join(self.tmp, 'pi')
        self.assertEqual(config_mod.resolve_pi(missing, default=False, windows=True, which=which), (missing, ''))

    def test_posix_is_unchanged(self):
        which = mock.Mock(return_value='/usr/local/bin/pi')
        missing = os.path.join(self.tmp, 'pi')
        self.assertEqual(config_mod.resolve_pi(missing, default=True, windows=False, which=which), (missing, ''))

    def test_config_and_check_name_the_pi_in_use(self):
        found = os.path.join(self.tmp, 'npm', 'pi.cmd')
        os.makedirs(os.path.dirname(found))
        open(found, 'w').close()
        os.chmod(found, 0o755)
        with mock.patch.object(config_mod, 'WINDOWS', True), \
                mock.patch.object(config_mod.shutil, 'which', return_value=found), \
                mock.patch.dict(os.environ, {'HOME': self.tmp, 'USERPROFILE': self.tmp}):
            cfg = make_cfg(self.tmp, report_port=0)
        self.assertEqual(cfg.pi, found)

        def run(argv, **kw):
            if argv[0] == found:
                return subprocess.CompletedProcess(argv, 0, '1.4.0\n', '')
            return subprocess.CompletedProcess(argv, 1, '', 'not a repo')

        out = io.StringIO()
        with mock.patch.object(cli.subprocess, 'run', side_effect=run), redirect_stdout(out):
            cli.cmd_check(cfg, SimpleNamespace())
        line = next(x for x in out.getvalue().splitlines() if 'pi:' in x)
        self.assertTrue(line.startswith('ok'), line)
        self.assertIn(found, line)
        self.assertIn('found on PATH', line)
        self.assertIn('1.4.0', line)


class SuperagentCommandOnWindowsTest(unittest.TestCase):
    def test_the_exe_is_found_and_written_with_forward_slashes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        exe = os.path.join(tmp, 'superagent.exe')
        open(exe, 'w').close()
        os.chmod(exe, 0o755)
        with mock.patch.object(cli, 'WINDOWS', True), mock.patch.object(cli.sys, 'argv', [exe]):
            command = cli.superagent_command()
        self.assertEqual(command, shlex.quote(os.path.abspath(exe).replace('\\', '/')))
        self.assertNotIn('\\', command)


if __name__ == '__main__':
    unittest.main()
