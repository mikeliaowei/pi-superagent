"""Several repositories in one service (S12) and tests the service runs before any pull request (S13)."""

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from superagent import cli
from superagent import config as cfgmod
from superagent.config import Config, RepoConfig
from superagent.github import GitHub, poll_repos
from superagent.model import Ticket, stage
from superagent.report import ReportServer
from superagent.service import Service
from superagent.store import Store
from tests.fake_github import FakeGitHub, FakeGitHubs
from tests.test_service import GIT_ENV, TOKEN, Git, Pi, git, until
from tests import toml_path

NOW = datetime(2026, 11, 8, 13, tzinfo=timezone.utc)


def write(path, text):
    with open(path, 'w') as f:
        f.write(text)
    return path


def py_command(tmp, name, code):
    """A test_command running `code` with this Python. The same command line works in sh and in cmd.exe."""
    script = write(os.path.join(tmp, f'{name}.py'), textwrap.dedent(code))
    return f'"{sys.executable}" "{script}"'


def make_clone(tmp, slug):
    """A bare origin with one commit on main, and a local clone of it. Returns the clone's path."""
    origin, checkout = os.path.join(tmp, f'{slug}.git'), os.path.join(tmp, slug)
    git('init', '--bare', '-b', 'main', origin, cwd=tmp)
    git('clone', origin, checkout, cwd=tmp)
    write(os.path.join(checkout, 'README'), f'{slug}\n')
    git('add', 'README', cwd=checkout)
    git('commit', '-m', 'init', cwd=checkout)
    git('push', 'origin', 'HEAD:main', cwd=checkout)
    return checkout


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-cfg-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, text):
        return cfgmod.load(write(os.path.join(self.tmp, 'c.toml'), text))

    HEAD = '[github]\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n'

    def test_single_repo_form_is_one_legacy_repo(self):
        c = self.load('[github]\nrepo = "o/r"\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n'
                      '[executor]\ncheckout = "~/src/r"\nbase_branch = "dev"\n')
        self.assertEqual(c.repos, [RepoConfig('o/r', os.path.expanduser('~/src/r'), 'dev', '', 900, legacy=True)])
        self.assertTrue(c.legacy)
        self.assertIs(c.repo_config('O/R'), c.repos[0])
        self.assertEqual((c.repo, c.checkout, c.base_branch, c.max_test_retries), ('o/r', c.repos[0].checkout, 'dev', 2))

    def test_repos_tables_with_defaults(self):
        c = self.load(self.HEAD + '[executor]\nbase_branch = "dev"\nmax_test_retries = 3\n'
                      '[[repos]]\nname = "a/one"\ncheckout = "~/src/one"\ntest_command = "make test"\n'
                      '[[repos]]\nname = "b/two"\ncheckout = "/src/two"\nbase_branch = "main"\ntest_timeout = 60\n')
        self.assertEqual(c.repos, [RepoConfig('a/one', os.path.expanduser('~/src/one'), 'dev', 'make test', 900),
                                   RepoConfig('b/two', '/src/two', 'main', '', 60)])
        self.assertFalse(c.legacy)
        self.assertEqual((c.max_test_retries, c.repo_config('B/Two').name), (3, 'b/two'))
        with self.assertRaisesRegex(ValueError, 'x/y is not configured'):
            c.repo_config('x/y')

    def test_bad_repo_lists_are_rejected_with_the_problem_named(self):
        cases = [
            ('[[repos]]\nname = "a/one"\ncheckout = "/a"\n[[repos]]\nname = "A/One"\ncheckout = "/b"\n',
             'duplicate repo in \\[\\[repos\\]\\]: A/One'),
            ('repos = []\n', '\\[\\[repos\\]\\] is empty'),
            ('[[repos]]\nname = "one"\ncheckout = "/a"\n', 'repo must be owner/name: one'),
        ]
        for text, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.load(text + self.HEAD)
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.load(self.HEAD.replace('[github]\n', '[github]\nrepo = "o/r"\n')
                      + '[executor]\ncheckout = "/c"\n[[repos]]\nname = "a/one"\ncheckout = "/a"\n')
        with self.assertRaises(TypeError):  # the CLI reports it as an unknown or missing field
            self.load('[[repos]]\nname = "a/one"\n' + self.HEAD)


class CheckTest(unittest.TestCase):
    def test_check_checks_every_repos_checkout(self):
        tmp = tempfile.mkdtemp(prefix='superagent-check-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ, GIT_ENV):
            good = make_clone(tmp, 'one')
        config = write(os.path.join(tmp, 'c.toml'),
                       f'[github]\nagent_login = "bot"\ntoken_env = "SA_TEST_TOKEN"\n[policy]\nallowed = ["alice"]\n'
                       f'[executor]\npi = "{toml_path(tmp)}/no-pi"\n[service]\nstate_dir = "{toml_path(tmp)}/state"\nreport_port = 0\n'
                       f'[[repos]]\nname = "a/one"\ncheckout = "{toml_path(good)}"\n'
                       f'[[repos]]\nname = "b/two"\ncheckout = "{toml_path(tmp)}/missing"\n')
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'SA_TEST_TOKEN': 'x'}), redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(['--config', config, 'check'])
        self.assertEqual(code, 1)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], 'ok    config loads: a/one, b/two as bot')
        self.assertIn(f'ok    checkout: {good} is a git repo with origin {tmp}/one.git', lines)
        self.assertIn(f'FAIL  checkout: {tmp}/missing is not a git clone with an origin remote; '
                      f'run `git clone https://github.com/b/two.git {tmp}/missing`', lines)


class PollTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.put('github-start', '2000-01-01T00:00:00+00:00')
        self.one, self.two, self.other = FakeGitHub('a/one'), FakeGitHub('b/two'), FakeGitHub('x/y')
        self.fakes = FakeGitHubs(self.one, self.two, self.other)
        for f in (self.one, self.two, self.other):
            f.add_issue('Fix it', '@bot fix')
            f.add_comment(1, '@bot and more')
        self.gh = GitHub(TOKEN, 'a/one', opener=self.fakes)

    def calls(self, path):
        return [c for c in self.fakes.calls if c.path == path]

    def test_one_notifications_call_for_all_repos_and_no_cursor_collision(self):
        out = poll_repos(self.gh, self.store, ['a/one', 'b/two'], 'bot', {}, now=NOW)
        self.assertEqual(len(self.calls('/notifications')), 1)
        self.assertEqual({t.id for t in out if t.kind == 'issue'}, {'github-issue|a/one#1', 'github-issue|b/two#1'})
        self.assertEqual({t.thread_key for t in out}, {'github:a/one#1', 'github:b/two#1'})
        self.assertEqual(len({t.id for t in out if t.kind == 'comment'}), 2)
        stepped = (NOW - timedelta(seconds=5)).isoformat(timespec='seconds')
        self.assertEqual((self.store.get('github-cursor:a/one#1'), self.store.get('github-cursor:b/two#1')),
                         (stepped, stepped))
        self.assertIsNone(self.store.get('github-cursor:1'))
        self.assertEqual((self.store.get('github-retry:a/one'), self.store.get('github-retry:b/two')), ({}, {}))
        self.assertIsNone(self.store.get('github-retry'))

        # Each repo reads its own cursor; subscribed numbers are per repo.
        self.store.put('github-cursor:b/two#1', '2026-11-08T12:59:00+00:00')
        self.fakes.calls.clear()
        poll_repos(self.gh, self.store, ['a/one', 'b/two'], 'bot', {'b/two': [1]}, now=NOW + timedelta(minutes=1))
        self.assertEqual(len(self.calls('/notifications')), 1)
        self.assertEqual(self.calls('/repos/a/one/issues/1/comments')[0].query['since'], [stepped])
        self.assertEqual(self.calls('/repos/b/two/issues/1/comments')[0].query['since'], ['2026-11-08T12:59:00+00:00'])
        self.assertEqual(self.calls('/repos/x/y/issues/1'), [])

    def test_a_failing_item_retries_only_in_its_own_repo(self):
        self.fakes.fakes['b/two'] = BrokenRepo(self.two)
        poll_repos(self.gh, self.store, ['a/one', 'b/two'], 'bot', {}, now=NOW)
        self.assertEqual(self.store.get('github-retry:b/two'), {'1': NOW.isoformat(timespec='seconds')})
        self.assertEqual(self.store.get('github-retry:a/one'), {})
        self.assertIsNone(self.store.get('github-cursor:b/two#1'))


class BrokenRepo:
    """Answers every request for its repo with a 500."""

    def __init__(self, fake):
        self.fake = fake

    def __call__(self, req):
        from tests.fake_github import http_error
        if req.full_url.endswith('/notifications') or '/notifications?' in req.full_url:
            return self.fake(req)
        raise http_error(req.full_url, 500, 'boom')


class ServiceBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-multi-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {**GIT_ENV, 'SUPERAGENT_GITHUB_TOKEN': TOKEN, 'GH_TOKEN': 'gh-secret-456'})
        env.start()
        self.addCleanup(env.stop)
        self.state = os.path.join(self.tmp, 'state')
        self.store = Store(os.path.join(self.state, 'superagent.sqlite'))
        self.store.put('github-start', '2000-01-01T00:00:00+00:00')
        self.git = Git()
        self.server = ReportServer(lambda tok: self.svc.lookup(tok), lambda tid, r: self.svc.on_report(tid, r))
        self.addCleanup(self.server.close)

    def config(self, **kw):
        return Config(agent_login='bot', allowed=['alice'], state_dir=self.state, pi='/nonexistent/pi', **kw)

    def service(self, cfg, opener, pi, **kw):
        self.cfg = cfg
        first = cfg.repos[0].name
        self.svc = Service(cfg, self.store, GitHub(TOKEN, first, opener=opener), spawn=pi, run_git=self.git,
                           report_url=self.server.url, **kw)
        self.addAsyncCleanup(self.svc.close)
        return self.svc

    async def round(self):
        await self.svc.poll_once()
        await self.svc.run_effects()

    async def drain(self):
        """Run until no worker or test command is running and no effect is due."""
        for _ in range(100):
            busy = [t for t in [*self.svc.tasks.values(), *self.svc.test_tasks.values()] if not t.done()]
            if busy:
                await asyncio.gather(*busy)
            await asyncio.sleep(0)
            await self.svc.run_effects()
            busy = [t for t in [*self.svc.tasks.values(), *self.svc.test_tasks.values()] if not t.done()]
            if not busy and not self.store.due_effects():
                return
        raise AssertionError('did not drain')

    def effect_keys(self, kind):
        return [r[0] for r in self.store.db.execute('SELECT key FROM effects WHERE kind = ? ORDER BY id', (kind,))]


class MultiRepoServiceTest(ServiceBase):
    async def test_two_repos_get_their_own_tickets_worktrees_and_pull_requests(self):
        one_co, two_co = make_clone(self.tmp, 'one'), make_clone(self.tmp, 'two')
        one, two, other = FakeGitHub('a/one'), FakeGitHub('b/two'), FakeGitHub('x/y')
        fakes = FakeGitHubs(one, two, other)
        for f in (one, two, other):
            f.add_issue('Fix it', '@bot fix')
        cfg = self.config(repos=[{'name': 'a/one', 'checkout': one_co}, {'name': 'b/two', 'checkout': two_co}])
        done = {'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}}
        pi = Pi(dict(done), dict(done))
        self.service(cfg, fakes, pi)
        await self.svc.poll_once()
        self.assertEqual(len([c for c in fakes.calls if c.path == '/notifications']), 1)
        await self.drain()

        tickets = {t.repo: t for t in self.store.tickets()}
        self.assertEqual(set(tickets), {'a/one', 'b/two'})  # nothing for the unconfigured x/y
        ta, tb = tickets['a/one'], tickets['b/two']
        self.assertEqual(sorted([ta.name, tb.name]), ['fix-it', 'fix-it-2'])  # short names unique across repos
        self.assertEqual((ta.thread_key, tb.thread_key), ('github:a/one#1', 'github:b/two#1'))
        self.assertEqual(ta.worktree, os.path.join(cfg.work_dir, 'a__one', ta.name))
        self.assertEqual(tb.worktree, os.path.join(cfg.work_dir, 'b__two', tb.name))
        for t, slug in ((ta, 'one'), (tb, 'two')):
            origin = git('remote', 'get-url', 'origin', cwd=t.worktree).stdout.strip()
            self.assertEqual(origin, os.path.join(self.tmp, f'{slug}.git'))
        for t, fake in ((ta, one), (tb, two)):
            self.assertEqual(list(fake.pulls), [t.pr_number])
            pr = fake.pulls[t.pr_number]
            self.assertEqual((pr['head'], pr['base']), (f'agent/{t.name}', 'main'))
            self.assertIn(f'Tests: no test command configured for {t.repo}.', pr['body'])
            posted = fake.posted(1)
            self.assertEqual(len(posted), 2)
            self.assertIn(f'https://github.com/{t.repo}/pull/{t.pr_number}', posted[1])
        self.assertEqual(other.pulls, {})
        self.assertEqual(other.posted(), [])
        pushes = sorted((argv[-2], re.sub(r'\.git\.\w+$', '.git', next(a for a in argv if a.startswith('--git-dir='))))
                        for argv, _ in self.git.pushes)
        self.assertEqual(pushes, [('https://github.com/a/one.git', f"--git-dir={self.state}/push/a__one.git"),
                                  ('https://github.com/b/two.git', f"--git-dir={self.state}/push/b__two.git")])

        two.merge(tb.pr_number)
        await self.svc.check_pulls()
        self.assertEqual((self.store.ticket(ta.id).state, self.store.ticket(tb.id).outcome), ('open', 'merged'))

    async def test_cli_and_status_show_the_repo(self):
        cfg = self.config(repos=[{'name': 'a/one', 'checkout': '/x'}, {'name': 'b/two', 'checkout': '/y'}])
        self.store.save_ticket(Ticket(id='tk-1', name='fix-it', title='Fix', thread_key='github:b/two#4', repo='b/two',
                                      number=4, mandate={}, created_at='2026-11-08T12:00:00+00:00'))
        self.assertEqual(cli.status_data(cfg, self.store)['repos'], ['a/one', 'b/two'])
        self.assertEqual(cli._row(self.store.ticket('tk-1'), cfg)['repo'], 'b/two')
        cfg_path = write(os.path.join(self.tmp, 'c.toml'),
                         f'[github]\nagent_login = "bot"\n[policy]\nallowed = ["a"]\n[service]\nstate_dir = "{toml_path(self.state)}"\n'
                         f'[[repos]]\nname = "a/one"\ncheckout = "/x"\n[[repos]]\nname = "b/two"\ncheckout = "/y"\n')
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--config', cfg_path, 'tickets']), 0)
        header, row = out.getvalue().splitlines()
        self.assertTrue(header.startswith('ID'))
        self.assertIn('REPO', header)
        self.assertIn('b/two', row)


class UpgradeTest(ServiceBase):
    """A database written by the single-repo form keeps working after the config moves to [[repos]]."""

    async def legacy_era(self):
        self.one_co, self.two_co = make_clone(self.tmp, 'one'), make_clone(self.tmp, 'two')
        self.one, self.two = FakeGitHub('a/one'), FakeGitHub('b/two')
        self.fakes = FakeGitHubs(self.one, self.two)
        self.one.add_issue('Fix it', '@bot fix')  # a/one#1: an issue request
        n = self.one.add_issue('Other thing', 'no mention')
        self.one.add_comment(n, '@bot please do the other thing')  # a/one#2: a comment request
        legacy = self.config(repo='a/one', checkout=self.one_co)
        pi = Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}}, {'mode': 'hang'})
        self.service(legacy, self.fakes, pi)
        await self.round()
        await self.round()
        await until(lambda: len(pi.runs) == 2 and pi.runs[1].messages)
        await asyncio.gather(self.svc.tasks[self.by_number(1).id])
        await self.svc.run_effects()
        await self.svc.close()
        self.assertEqual(len(self.store.tickets()), 2)
        self.assertEqual(self.store.get('github-cursor:1') is not None, True)
        return self.store.get('github-cursor:1')

    def by_number(self, n, repo='a/one'):
        return next(t for t in self.store.tickets() if t.number == n and t.repo == repo)

    async def test_switching_to_repos_reuses_old_cursors_and_ids_without_duplicates(self):
        old_cursor = await self.legacy_era()
        old = {t.id: t.worktree for t in self.store.tickets()}
        self.two.add_issue('Fix it', '@bot fix')  # b/two#1: the same number in another repo
        # The original repo is listed second: the old keys still belong to it, not to the first entry.
        cfg = self.config(repos=[{'name': 'b/two', 'checkout': self.two_co}, {'name': 'a/one', 'checkout': self.one_co}])
        self.fakes.calls.clear()
        self.service(cfg, self.fakes, Pi({'mode': 'hang'}, {'mode': 'hang'}))
        self.svc.recover()
        await self.svc.poll_once()
        since = {c.path: c.query.get('since') for c in self.fakes.calls if c.path.endswith('/comments')}
        self.assertEqual(since['/repos/a/one/issues/1/comments'], [old_cursor])
        self.assertEqual(since['/repos/b/two/issues/1/comments'], ['2000-01-01T00:00:00+00:00'])
        tickets = self.store.tickets()
        self.assertEqual(len(tickets), 3)
        self.assertEqual(self.by_number(1, 'b/two').mandate['trigger_id'], 'github-issue|b/two#1')
        self.assertEqual({t.id: t.worktree for t in tickets if t.id in old}, old)  # old worktrees stay where they are
        self.assertIsNotNone(self.store.get('github-cursor:a/one#1'))
        await self.svc.check_pulls()
        t = self.by_number(1)
        self.one.merge(t.pr_number)
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).outcome, 'merged')

    async def test_old_issue_ids_are_not_reopened_when_the_cursors_are_gone(self):
        await self.legacy_era()
        t = self.by_number(1)
        self.one.merge(t.pr_number)  # ended Tickets are where a re-read request would open a second one
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).outcome, 'merged')
        for k in [r[0] for r in self.store.db.execute("SELECT k FROM kv WHERE k LIKE 'github-cursor:%'")]:
            self.store.put(k, None)
        cfg = self.config(repos=[{'name': 'a/one', 'checkout': self.one_co}, {'name': 'b/two', 'checkout': self.two_co}])
        self.service(cfg, self.fakes, Pi())
        await self.svc.poll_once()
        await self.svc.poll_once()
        self.assertEqual(len(self.store.tickets()), 2)
        self.assertFalse(self.store.has_trigger('github-issue|a/one#1'))


class EnforcedTestsTest(ServiceBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.checkout = make_clone(self.tmp, 'r')
        self.gh = FakeGitHub('o/r')
        self.n = self.gh.add_issue('Fix it', '@bot fix')

    def start(self, command, *steps, **repo):
        self.command = command
        cfg = self.config(repos=[{'name': 'o/r', 'checkout': self.checkout, 'test_command': command, **repo}])
        self.pi = Pi(*steps)
        return self.service(cfg, self.gh, self.pi)

    async def test_passing_tests_deliver_with_a_pr_line_and_run_with_the_worker_environment(self):
        seen = os.path.join(self.tmp, 'seen')
        os.makedirs(seen)
        self.start(py_command(self.tmp, 'dump', f'''
            import os
            with open({os.path.join(seen, 'env')!r}, 'w') as f:
                f.writelines(f'{{k}}={{v}}\\n' for k, v in os.environ.items())
            with open({os.path.join(seen, 'pwd')!r}, 'w') as f:
                f.write(os.getcwd())
            print('all good')
            '''), {'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}})
        await self.round()
        await self.drain()
        t = self.store.tickets()[0]
        self.assertIn(self.command, self.pi.runs[0].messages[0])  # the worker is told what will run
        env = open(f'{seen}/env').read()
        for secret in (TOKEN, 'gh-secret-456', 'SUPERAGENT_GITHUB_TOKEN', 'GH_TOKEN', t.token):
            self.assertNotIn(secret, env)
        self.assertIn('credential.helper', env)
        self.assertIn('GIT_TERMINAL_PROMPT=0', env)
        self.assertEqual(os.path.realpath(open(f'{seen}/pwd').read().strip()), os.path.realpath(t.worktree))
        pr = self.gh.pulls[t.pr_number]
        self.assertRegex(pr['body'], re.escape(f'Tests: `{self.command}` passed in ') + r'\d+s')
        self.assertEqual(self.effect_keys('test'), [f'{t.id}:{t.current.id}:test-1'])
        self.assertEqual((t.test_failures, stage(t)), (0, 'person/review'))

    async def test_failing_tests_open_no_pr_and_resume_the_same_session_with_the_output_tail(self):
        def fix(cwd, env):
            write(os.path.join(cwd, 'ok.txt'), 'ok\n')
        self.start(py_command(self.tmp, 'check', '''
            import os, sys
            if not os.path.exists('ok.txt'):
                print('first' + '-line')
                for i in range(1, 3001):
                    print(i)
                sys.exit(3)
            '''),
                   {'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}},
                   {'before': fix, 'report': {'status': 'ready', 'summary': 'Fixed the tests.'}})
        await self.round()
        await self.drain()
        t = self.store.tickets()[0]
        self.assertEqual(len(self.pi.runs), 2)
        self.assertEqual(self.pi.runs[1].session_id(), t.id)
        msg = self.pi.runs[1].messages[0]
        self.assertIn(self.command, msg)
        self.assertIn('exit code 3', msg)
        self.assertIn('2999\n3000', msg)
        self.assertNotIn('first-line', msg)  # only the tail
        self.assertLess(len(msg) - 2 * len(self.command), 3460)  # the tail is capped; the command appears twice
        self.assertEqual(len(self.gh.pulls), 1)  # only after the fix
        self.assertEqual(len(self.git.pushes), 1)
        self.assertEqual(self.gh.pulls[t.pr_number]['body'].count('Tests:'), 1)
        self.assertEqual(t.test_failures, 0)  # reset by the pass
        self.assertTrue(t.test_output.endswith('3000'))  # the last failure is kept for people
        a1, a2 = t.assignments[0]['id'], t.assignments[1]['id']
        self.assertEqual(self.effect_keys('test'), [f'{t.id}:{a1}:test-1', f'{t.id}:{a2}:test-2'])
        self.assertEqual(len(self.gh.posted(self.n)), 2)  # the ack and the PR link: no noise about the failure

    async def test_tests_still_failing_after_the_retries_block_the_ticket_honestly(self):
        ready = {'report': {'status': 'ready', 'summary': 'Fixed.'}}
        self.start(py_command(self.tmp, 'nope', 'print("nope"); raise SystemExit(1)'),
                   {'commit': True, **ready}, dict(ready), dict(ready))
        await self.round()
        await self.drain()
        t = self.store.tickets()[0]
        self.assertEqual(len(self.pi.runs), 3)  # the first try plus max_test_retries (2) fixes
        self.assertEqual(len(self.effect_keys('test')), 3)
        self.assertEqual((t.current.report, t.current.summary), ('blocked', f'tests still failing: {self.command}'))
        self.assertEqual((stage(t), t.test_failures, t.pr_number), ('person/blocked', 3, None))
        self.assertEqual((self.gh.pulls, self.git.pushes), ({}, []))
        posted = [p.split('<!--')[0].strip() for p in self.gh.posted(self.n)]
        self.assertEqual(len(posted), 2)
        self.assertIn('tests still failing', posted[1])
        self.assertIn(self.command, posted[1])

    async def test_a_test_command_that_runs_too_long_is_killed_and_reported_as_a_timeout(self):
        # The command leaves a child running: the kill must end the whole group (process tree on Windows).
        self.start(py_command(self.tmp, 'slow', '''
            import subprocess, sys
            print('started', flush=True)
            subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']).wait()
            '''), {'commit': True, 'report': {'status': 'ready', 'summary': 'x'}},
                   {'report': {'status': 'blocked', 'summary': 'cannot speed it up'}}, test_timeout=1)
        began = time.monotonic()
        await self.round()
        await self.drain()
        self.assertLess(time.monotonic() - began, 20)
        msg = self.pi.runs[1].messages[0]
        self.assertIn('timed out after 1s', msg)
        self.assertIn('started', msg)
        t = self.store.tickets()[0]
        self.assertEqual((t.test_failures, self.gh.pulls), (1, {}))

    async def test_without_a_test_command_delivery_is_unchanged_but_says_so(self):
        self.start('', {'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}})
        await self.round()
        await self.drain()
        t = self.store.tickets()[0]
        self.assertEqual(self.effect_keys('test'), [])
        self.assertEqual(self.effect_keys('deliver'), [f'{t.id}:{t.current.id}'])
        self.assertIn('Tests: no test command configured for o/r.', self.gh.pulls[t.pr_number]['body'])
        self.assertNotIn('test_command', self.pi.runs[0].messages[0])


class TicketCompatibilityTest(unittest.TestCase):
    def test_a_ticket_without_new_features_saves_only_fields_the_previous_version_knows(self):
        # Rollback: the previous version loads Tickets with Ticket(**data), so unknown keys would crash it.
        old = {'id', 'name', 'title', 'thread_key', 'repo', 'number', 'mandate', 'created_at', 'state', 'outcome',
               'branch', 'worktree', 'pr_number', 'token', 'assignments', 'last_human_at', 'delivery_error'}
        t = Ticket(id='tk-1', name='n', title='t', thread_key='github:o/r#1', repo='o/r', number=1, mandate={},
                   created_at='2026-11-08T12:00:00+00:00')
        self.assertEqual(set(t.to_dict()), old)
        self.assertEqual(Ticket.from_dict(t.to_dict()), t)
        t.test_failures = 1
        self.assertEqual(t.to_dict()['test_failures'], 1)  # used features are still stored

    def test_a_ticket_saved_by_the_previous_version_loads_with_defaults(self):
        old = {'id': 'tk-old', 'name': 'fix-it', 'title': 'Fix it', 'thread_key': 'github:o/r#1', 'repo': 'o/r',
               'number': 1, 'mandate': {'actor': 'alice'}, 'created_at': '2026-11-08T12:00:00+00:00',
               'state': 'open', 'outcome': None, 'branch': 'agent/fix-it', 'worktree': '/w/fix-it', 'pr_number': None,
               'token': 't', 'assignments': [], 'last_human_at': None, 'delivery_error': ''}
        s = Store(':memory:')
        s.db.execute('INSERT INTO tickets VALUES (?, ?, ?, ?)', ('tk-old', 'github:o/r#1', 'open', json.dumps(old)))
        t = s.ticket('tk-old')
        self.assertEqual((t.test_failures, t.test_output, t.test_result, t.worktree), (0, '', '', '/w/fix-it'))


if __name__ == '__main__':
    unittest.main()
