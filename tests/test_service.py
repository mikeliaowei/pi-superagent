"""End-to-end service tests: fake GitHub, the fake Pi script, a real report server and real git worktrees."""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from superagent import cli
from superagent.config import Config
from superagent.executor import first_message, followup_message, recovery_note
from superagent.github import GitHub
from superagent.model import stage
from superagent.pi_rpc import PiRpc
from superagent.report import ReportServer
from superagent.service import Service
from superagent.store import Store
from tests.fake_github import FakeGitHub
from tests import toml_path

FAKE_PI = os.path.join(os.path.dirname(__file__), 'fake_pi.py')
TOKEN = 'ghp_TESTTOKEN_must_never_leak_123'
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def post_report(env, report):
    req = urllib.request.Request(env['SUPERAGENT_URL'] + '/report', data=json.dumps(report).encode(), method='POST',
                                 headers={'Authorization': f"Bearer {env['SUPERAGENT_TICKET_TOKEN']}",
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10):
        pass


class ScriptedRpc(PiRpc):
    """Runs tests/fake_pi.py in place of pi. Before each run it may commit in the worktree and report like a worker."""

    def __init__(self, step):
        super().__init__()
        self.step, self.messages = step, []

    async def start(self, argv, cwd, env):
        self.argv, self.cwd, self.env = argv, cwd, env
        await super().start([sys.executable, FAKE_PI, *argv[1:]], cwd,
                            {**env, 'FAKE_PI_MODE': self.step.get('mode', 'ok')})

    async def run(self, message, timeout):
        self.messages.append(message)
        if before := self.step.get('before'):
            before(self.cwd, self.env)  # runs as the worker would: in its worktree, with its environment
        if self.step.get('commit'):
            with open(os.path.join(self.cwd, 'fix.txt'), 'w') as f:
                f.write('fixed\n')
            git('add', 'fix.txt', cwd=self.cwd)
            git('commit', '-m', 'fix', cwd=self.cwd)
        if report := self.step.get('report'):
            await asyncio.to_thread(post_report, self.env, report)
        return await super().run(message, timeout)

    def session_id(self):
        return self.argv[self.argv.index('--session-id') + 1]


class Pi:
    """The `spawn` override: one ScriptedRpc per launch, each taking the next scripted step."""

    def __init__(self, *steps):
        self.steps, self.runs = list(steps), []

    def __call__(self):
        rpc = ScriptedRpc(self.steps.pop(0) if self.steps else {})
        self.runs.append(rpc)
        return rpc


class Git:
    """Real git, except pushes (which would reach github.com): those are recorded, or fail on demand."""

    def __init__(self):
        self.pushes, self.fail_push = [], False

    def __call__(self, argv, **kw):
        if 'push' in argv:
            self.pushes.append((argv, kw.get('env') or {}))
            return subprocess.CompletedProcess(argv, 1 if self.fail_push else 0, '', 'denied' if self.fail_push else '')
        return subprocess.run(argv, **kw)


async def until(pred, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError('condition not reached')
        await asyncio.sleep(0.02)


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-test-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {**GIT_ENV, 'SUPERAGENT_GITHUB_TOKEN': TOKEN})
        env.start()
        self.addCleanup(env.stop)
        origin, checkout = os.path.join(self.tmp, 'origin.git'), os.path.join(self.tmp, 'checkout')
        git('init', '--bare', '-b', 'main', origin, cwd=self.tmp)
        git('clone', origin, checkout, cwd=self.tmp)
        with open(os.path.join(checkout, 'README'), 'w') as f:
            f.write('hello\n')
        git('add', 'README', cwd=checkout)
        git('commit', '-m', 'init', cwd=checkout)
        git('push', 'origin', 'HEAD:main', cwd=checkout)
        self.cfg = Config(repo='o/r', agent_login='bot', allowed=['alice'], checkout=checkout,
                          state_dir=os.path.join(self.tmp, 'state'), pi='/nonexistent/pi')
        self.store = Store(self.cfg.db_path)
        self.store.put('github-start', '2000-01-01T00:00:00+00:00')  # as if the service first ran long ago
        self.gh, self.git = FakeGitHub(), Git()
        self.now = datetime.now(timezone.utc)
        self.server = ReportServer(lambda tok: self.svc.lookup(tok), lambda tid, r: self.svc.on_report(tid, r))
        self.addCleanup(self.server.close)

    def service(self, pi, **kw):
        self.svc = Service(self.cfg, self.store, GitHub(TOKEN, 'o/r', opener=self.gh), spawn=pi, run_git=self.git,
                           report_url=self.server.url, **kw)
        self.addAsyncCleanup(self.svc.close)
        return self.svc

    async def round(self):
        await self.svc.poll_once()
        await self.svc.run_effects()

    async def settle(self):
        """Let every running worker finish, then run the effects their reports queued."""
        await asyncio.gather(*list(self.svc.tasks.values()))
        await asyncio.sleep(0)
        await self.svc.run_effects()

    def only_ticket(self):
        tickets = self.store.tickets()
        self.assertEqual(len(tickets), 1)
        return tickets[0]

    def decisions(self):
        return {r['id']: r['decision'] for r in self.store.db.execute('SELECT id, decision FROM triggers')}

    async def test_explicit_mention_runs_to_a_merged_pull_request(self):
        n = self.gh.add_issue('Fix the login bug', '@bot please fix it')
        pi = Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed the login bug.'}})
        self.service(pi)
        await self.svc.poll_once()
        t = self.only_ticket()
        self.assertEqual(t.mandate['actor'], 'alice')
        self.assertEqual(t.mandate['strength'], 'explicit')
        self.assertEqual(t.mandate['trigger_id'], f'github-issue|{n}')
        self.assertEqual((t.title, t.name, t.number), ('Fix the login bug', 'fix-the-login-bug', n))
        self.assertEqual(stage(t), 'working/queued')
        await self.svc.run_effects()
        await self.round()  # a second poll sees the same issue and the agent's own ack: nothing new
        self.assertEqual(len(self.store.tickets()), 1)
        # The fast fake worker may already have delivered (a PR-link reply); what matters is one ack.
        acks = [c for c in self.gh.posted(n) if c.startswith('On it')]
        self.assertEqual(len(acks), 1)
        self.assertNotIn('\n\n', acks[0].split('<!--')[0].strip())  # one short sentence
        await until(lambda: pi.runs and pi.runs[0].messages)
        t = self.store.ticket(t.id)
        self.assertEqual(pi.runs[0].session_id(), t.id)
        self.assertEqual(pi.runs[0].messages[0], first_message(t, 'Fix the login bug\n\n@bot please fix it'))
        self.assertEqual((t.branch, t.worktree), ('agent/fix-the-login-bug', os.path.join(self.cfg.work_dir, t.name)))
        await self.settle()
        t = self.store.ticket(t.id)
        self.assertEqual(len(self.gh.pulls), 1)
        pr = next(iter(self.gh.pulls.values()))
        self.assertEqual((pr['head'], pr['base'], pr['title']), ('agent/fix-the-login-bug', 'main', 'Fix the login bug'))
        self.assertIn('Fixed the login bug.', pr['body'])
        self.assertIn(f'Closes #{n}', pr['body'])
        self.assertEqual(self.git.pushes[0][0][-2:], ['https://github.com/o/r.git',
                                                      '+refs/heads/agent/fix-the-login-bug:refs/heads/agent/fix-the-login-bug'])
        git_dir = next(a for a in self.git.pushes[0][0] if a.startswith('--git-dir='))
        self.assertTrue(git_dir.startswith(f"--git-dir={os.path.join(self.cfg.state_dir, 'push.git')}."))  # fresh per push
        self.assertEqual(t.pr_number, pr['number'])
        self.assertEqual(stage(t), 'person/review')
        posted = self.gh.posted(n)
        self.assertEqual(len(posted), 2)
        self.assertIn(f"https://github.com/o/r/pull/{pr['number']}", posted[1])
        self.gh.merge(pr['number'])
        await self.svc.check_pulls()
        t = self.store.ticket(t.id)
        self.assertEqual((t.state, t.outcome, stage(t)), ('ended', 'merged', 'ended'))
        self.assertEqual(self.svc.workers, {})

    async def test_pull_request_thread_body_has_no_closes_line(self):
        pr_thread = self.gh.add_issue('Tweak docs', '@bot tidy this PR')
        self.gh.issues[pr_thread]['pull_request'] = {}
        self.gh.pulls[pr_thread] = {'number': pr_thread, 'state': 'open', 'merged': False, 'head': 'x', 'base': 'main'}
        self.service(Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Tidied.'}}))
        await self.round()
        await self.settle()
        new = [p for k, p in self.gh.pulls.items() if k != pr_thread]
        self.assertEqual(len(new), 1)
        self.assertNotIn('Closes', new[0]['body'])

    async def test_closed_pull_request_ends_the_ticket_closed(self):
        self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Done.'}}))
        await self.round()
        await self.settle()
        t = self.only_ticket()
        self.gh.close(t.pr_number)
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).outcome, 'closed')

    async def test_unlisted_bot_and_own_messages_are_ignored(self):
        n = self.gh.add_issue('Do a thing', '@bot do it', login='mallory')
        self.gh.add_comment(n, '@bot run this', login='dependabot[bot]')
        own = self.gh.add_comment(n, '@bot talking to myself', login='bot')
        self.service(Pi())
        await self.round()
        self.assertEqual(self.store.tickets(), [])
        self.assertEqual(self.gh.posted(), ['@bot talking to myself'])
        decisions = self.decisions()
        self.assertEqual(decisions[f'github-issue|{n}'], 'ignore')
        self.assertEqual(len(decisions), 2)  # the agent's own comment is never even a Trigger
        self.assertFalse(any(str(own['id']) in k for k in decisions))
        self.assertEqual(set(decisions.values()), {'ignore'})
        reasons = {t.actor: r for t, _, r in self.store.recent_triggers()}  # why, not just what, is stored
        self.assertEqual(reasons, {'mallory': 'not addressed by a listed person', 'dependabot[bot]': 'bot'})

    async def test_waiting_question_is_posted_and_the_answer_resumes_the_same_session(self):
        n = self.gh.add_issue('Add storage', '@bot add storage')
        pi = Pi({'report': {'status': 'waiting', 'summary': 'Need a choice.', 'question': 'Which database?'}},
                {'mode': 'hang'})
        self.service(pi)
        await self.round()
        await self.settle()
        t = self.only_ticket()
        self.assertEqual(stage(t), 'person/answer')
        self.assertEqual(t.current.question, 'Which database?')
        self.assertIn('Which database?', self.gh.posted(n)[-1])
        self.assertEqual(len(self.gh.posted(n)), 2)
        self.gh.add_comment(n, 'Use SQLite.', login='alice')
        await self.round()
        await until(lambda: len(pi.runs) == 2 and pi.runs[1].messages)
        self.assertEqual(pi.runs[1].session_id(), pi.runs[0].session_id())
        self.assertEqual(pi.runs[1].messages[0], followup_message([{'text': 'Use SQLite.'}]))
        self.assertEqual(self.store.pending_inputs(t.id), [])
        t = self.store.ticket(t.id)
        self.assertEqual((len(t.assignments), t.current.state), (2, 'running'))
        self.assertEqual(len(self.gh.posted(n)), 2)  # no extra chatter

    async def test_comment_while_running_is_steered_into_the_worker(self):
        n = self.gh.add_issue('Long job', '@bot do the long job')
        pi = Pi({'mode': 'hang'})
        self.service(pi)
        await self.round()
        await until(lambda: self.svc.workers and next(iter(self.svc.workers.values())).running)
        self.gh.add_comment(n, 'Also update the docs.', login='carol')  # unlisted: context only
        await self.svc.poll_once()
        t = self.only_ticket()
        self.assertEqual(self.store.pending_inputs(t.id), [])
        self.assertEqual(len(pi.runs), 1)
        self.assertEqual(t.last_human_at, self.gh.comments[n][-1]['updated_at'])
        self.assertEqual(self.decisions()[f"github-comment|{self.gh.comments[n][-1]['id']}|"
                                          f"{self.gh.comments[n][-1]['updated_at']}"], 'context')

    async def test_stop_ends_the_ticket_and_the_worker(self):
        n = self.gh.add_issue('Big refactor', '@bot refactor everything')
        pi = Pi({'mode': 'hang'})
        self.service(pi)
        await self.round()
        await until(lambda: self.svc.workers and next(iter(self.svc.workers.values())).running)
        self.gh.add_comment(n, '@bot stop', login='alice')
        await self.round()
        t = self.only_ticket()
        self.assertEqual((t.state, t.outcome), ('ended', 'stopped'))
        self.assertIsNotNone(pi.runs[0].proc.returncode)
        await self.settle()
        posted = self.gh.posted(n)
        self.assertEqual(len(posted), 2)
        self.assertIn('Stopped', posted[1])
        self.assertEqual(self.svc.workers, {})

    async def test_run_error_fails_the_assignment_with_an_honest_reply(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'mode': 'error'}))
        await self.round()
        await self.settle()
        t = self.only_ticket()
        a = t.current
        self.assertEqual((a.state, a.report, a.end_reason), ('ended', 'failed', 'provider exploded'))
        self.assertEqual(stage(t), 'person/failed')
        self.assertIn('provider exploded', self.gh.posted(n)[-1])

    async def test_run_error_after_a_ready_report_is_not_recorded_as_settled(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'mode': 'error', 'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed.'}}))
        await self.round()
        await self.settle()
        await self.svc.run_effects()
        t = self.only_ticket()
        a = t.current
        self.assertEqual((a.state, a.report), ('ended', 'ready'))
        self.assertEqual(a.end_reason, 'provider exploded')
        self.assertEqual(len(self.gh.pulls), 1)  # the reported, committed work is still delivered
        notes = [b for b in self.gh.posted(n) if 'ended with an error' in b]
        self.assertEqual(len(notes), 1)
        self.assertIn('provider exploded', notes[0])

    async def test_report_after_the_run_ended_is_refused_with_409_and_changes_nothing(self):
        self.gh.add_issue('Fix it', '@bot fix')
        pi = Pi({'mode': 'ok'})
        self.service(pi)
        await self.round()
        await self.settle()
        before = self.only_ticket()
        self.assertEqual(before.current.report, 'failed')
        with self.assertRaises(urllib.error.HTTPError) as cm:
            await asyncio.to_thread(post_report, pi.runs[0].env, {'status': 'ready', 'summary': 'Late.'})
        self.assertEqual(cm.exception.code, 409)
        await asyncio.sleep(0.05)
        await self.svc.run_effects()
        self.assertEqual(self.only_ticket().to_dict(), before.to_dict())
        self.assertEqual(self.gh.pulls, {})

    async def test_editing_a_handled_request_after_its_ticket_ended_opens_nothing(self):
        n = self.gh.add_issue('Login', 'The login page is broken.')
        c = self.gh.add_comment(n, '@bot please fix the login page')
        self.service(Pi())
        await self.round()
        await self.settle()
        t = self.only_ticket()
        await self.svc.end_ticket(t.id, 'merged')
        c.update(body='@bot please fix the login page (typo fixed)', updated_at=self.gh._stamp())
        await self.round()
        await self.settle()
        self.assertEqual(len(self.store.tickets()), 1)
        self.assertEqual(len([b for b in self.gh.posted(n) if b.startswith('On it')]), 1)
        self.assertEqual(self.decisions()[f"github-comment|{c['id']}|{c['updated_at']}"], 'ignore')

    async def test_re_edited_old_stop_does_not_stop_a_newer_ticket(self):
        n = self.gh.add_issue('Refactor', 'Some context.')
        self.gh.add_comment(n, '@bot refactor the parser')
        self.service(Pi({'mode': 'hang'}, {'mode': 'hang'}))
        await self.round()
        first = self.only_ticket()
        stop = self.gh.add_comment(n, '@bot stop')
        await self.round()
        self.assertEqual(self.store.ticket(first.id).outcome, 'stopped')
        self.gh.add_comment(n, '@bot refactor the lexer instead')
        await self.round()
        second = next(t for t in self.store.tickets() if t.id != first.id)
        self.assertEqual(second.state, 'open')
        stop.update(body='@bot stop (edited)', updated_at=self.gh._stamp())
        await self.round()
        self.assertEqual(self.store.ticket(second.id).state, 'open')
        self.assertEqual(self.decisions()[f"github-comment|{stop['id']}|{stop['updated_at']}"], 'context')

    async def test_run_without_a_report_is_failed_as_no_report(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'mode': 'ok'}))
        await self.round()
        await self.settle()
        a = self.only_ticket().current
        self.assertEqual((a.report, a.end_reason), ('failed', 'no report'))
        self.assertIn('without reporting', self.gh.posted(n)[-1])

    async def test_ready_without_commits_replies_no_changes_and_opens_no_pr(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'report': {'status': 'ready', 'summary': 'Nothing to do.'}}))
        await self.round()
        await self.settle()
        self.assertEqual(self.gh.pulls, {})
        self.assertEqual(self.git.pushes, [])
        self.assertIn('no changes', self.gh.posted(n)[-1])
        self.assertIsNone(self.only_ticket().pr_number)

    async def test_progress_report_posts_nothing(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'mode': 'hang', 'report': {'status': 'progress', 'summary': 'Halfway.'}}))
        await self.round()
        await until(lambda: self.only_ticket().current.report == 'progress')
        await self.svc.run_effects()
        self.assertEqual(len(self.gh.posted(n)), 1)
        self.assertEqual(self.only_ticket().current.summary, 'Halfway.')

    async def test_blocked_report_replies_with_the_reason(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi({'report': {'status': 'blocked', 'summary': 'The tests need a database I cannot reach.'}}))
        await self.round()
        await self.settle()
        self.assertEqual(stage(self.only_ticket()), 'person/blocked')
        self.assertIn('cannot reach', self.gh.posted(n)[-1])

    async def test_delivery_failing_five_times_sets_delivery_error(self):
        self.gh.add_issue('Fix it', '@bot fix')
        self.git.fail_push = True
        self.service(Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Done.'}}), clock=lambda: self.now)
        await self.round()
        await self.settle()
        for _ in range(5):
            self.now += timedelta(seconds=301)
            await self.svc.run_effects()
        t = self.only_ticket()
        self.assertIn('push', t.delivery_error)
        self.assertEqual(stage(t), 'person/delivery')
        self.assertEqual(len(self.git.pushes), 5)
        self.assertEqual(self.store.failing_effects(), [])

    async def test_restart_recovers_a_running_assignment_with_a_recovery_note(self):
        self.gh.add_issue('Fix it', '@bot fix')
        pi = Pi({'mode': 'hang'}, {'mode': 'hang'})
        first = self.service(pi)
        await self.round()
        await until(lambda: pi.runs and pi.runs[0].messages)
        await first.close()  # the host goes away: the assignment stays running in the store
        t = self.only_ticket()
        self.assertEqual(t.current.state, 'running')
        self.store.add_input(t.id, 'comment', 'Please also add a test.')
        second = self.service(pi)
        second.recover()
        t = self.store.ticket(t.id)
        self.assertEqual((t.assignments[0]['state'], t.assignments[0]['end_reason']), ('ended', 'host restarted'))
        self.assertEqual(t.current.state, 'pending')
        await second.run_effects()
        await until(lambda: len(pi.runs) == 2 and pi.runs[1].messages)
        msg = pi.runs[1].messages[0]
        self.assertTrue(msg.startswith(recovery_note()))
        self.assertIn('Please also add a test.', msg)
        self.assertEqual(pi.runs[1].session_id(), t.id)

    async def test_max_workers_keeps_extra_launches_queued_without_failing_them(self):
        self.cfg.max_workers = 1
        self.gh.add_issue('First', '@bot one')
        self.gh.add_issue('Second', '@bot two')
        pi = Pi({'mode': 'hang'}, {'mode': 'hang'})
        self.service(pi)
        await self.round()
        await self.svc.run_effects()
        self.assertEqual(len(pi.runs), 1)
        self.assertEqual(len(self.svc.workers), 1)
        waiting = [e for e in self.store.due_effects() if e['kind'] == 'launch']
        self.assertEqual(len(waiting), 1)
        self.assertEqual((waiting[0]['attempts'], waiting[0]['last_error']), (0, ''))
        self.assertEqual(self.store.failing_effects(), [])

    async def test_github_token_never_reaches_workers_comments_or_logs(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        pi = Pi({'commit': True, 'report': {'status': 'ready', 'summary': 'Done.'}})
        self.service(pi)
        with self.assertLogs('superagent', level='DEBUG') as logs:
            await self.round()
            await self.settle()
            self.gh.merge(self.only_ticket().pr_number)
            await self.svc.check_pulls()
        self.assertTrue(logs.output)
        self.assertFalse(any(TOKEN in line for line in logs.output))
        env = pi.runs[0].env
        self.assertNotIn('SUPERAGENT_GITHUB_TOKEN', env)
        self.assertFalse(any(TOKEN in v for v in env.values()))
        self.assertFalse(any(TOKEN in a for a in pi.runs[0].argv))
        self.assertFalse(any(TOKEN in b for b in self.gh.posted()))
        self.assertFalse(any(TOKEN in json.dumps(p) for p in self.gh.pulls.values()))
        self.assertTrue(self.gh.posted(n))

    async def test_rate_limited_poll_is_skipped_until_it_clears(self):
        self.gh.add_issue('Fix it', '@bot fix')
        self.service(Pi(), clock=lambda: self.now)
        self.gh.rate_limited = True
        await self.svc.poll_once()
        self.gh.rate_limited = False
        calls = len(self.gh.calls)
        await self.svc.poll_once()  # still paused
        self.assertEqual(len(self.gh.calls), calls)
        self.now += timedelta(hours=1, seconds=1)
        await self.svc.poll_once()
        self.assertEqual(len(self.store.tickets()), 1)

    async def test_operator_stop_request_is_applied_by_the_running_service(self):
        n = self.gh.add_issue('Fix it', '@bot fix')
        pi = Pi({'mode': 'hang'})
        self.service(pi)
        await self.round()
        await until(lambda: self.svc.workers)
        t = self.only_ticket()
        self.store.put(f'operator:stop:{t.id}', self.now.isoformat())
        await self.round()
        t = self.store.ticket(t.id)
        self.assertEqual((t.state, t.outcome), ('ended', 'stopped'))
        self.assertIsNotNone(pi.runs[0].proc.returncode)
        self.assertFalse(self.store.get(f'operator:stop:{t.id}'))
        self.assertIn('operator', self.gh.posted(n)[-1])

    async def test_operator_retry_relaunches_a_failed_ticket(self):
        self.gh.add_issue('Fix it', '@bot fix')
        pi = Pi({'mode': 'error'}, {'mode': 'hang'})
        self.service(pi)
        await self.round()
        await self.settle()
        t = self.only_ticket()
        self.store.put(f'operator:retry:{t.id}', self.now.isoformat())
        await self.round()
        await until(lambda: len(pi.runs) == 2 and pi.runs[1].messages)
        t = self.store.ticket(t.id)
        self.assertEqual((len(t.assignments), t.current.state), (2, 'running'))
        self.assertEqual(pi.runs[1].session_id(), t.id)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-cli-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, 'config.toml')
        with open(self.config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\ntoken_env = "SA_TEST_TOKEN"\n'
                    f'[policy]\nallowed = ["alice"]\n'
                    f'[executor]\ncheckout = "{toml_path(self.tmp)}/checkout"\npi = "{toml_path(self.tmp)}/no-pi"\n'
                    f'[service]\nstate_dir = "{toml_path(self.tmp)}/state"\nreport_port = 0\n')

    def cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        environ = {**{k: v for k, v in os.environ.items() if k != 'SA_TEST_TOKEN'}, **(env or {})}
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def add_ticket(self):
        from superagent.model import Assignment, Ticket
        from dataclasses import asdict
        store = Store(os.path.join(self.tmp, 'state', 'superagent.sqlite'))
        t = Ticket(id='tk-1', name='fix-it', title='Fix it', thread_key='github:o/r#1', repo='o/r', number=1,
                   mandate={'actor': 'alice'}, created_at='2026-11-08T12:00:00+00:00',
                   assignments=[asdict(Assignment(id='as-1'))])
        store.save_ticket(t)
        return store, t

    def test_check_reports_a_missing_token_with_exit_2(self):
        code, out, err = self.cli('check')
        self.assertEqual(code, 2)
        self.assertIn('SA_TEST_TOKEN', out + err)
        self.assertNotIn('Traceback', out + err)

    def test_check_reports_other_problems_with_exit_1(self):
        code, out, err = self.cli('check', env={'SA_TEST_TOKEN': 'x'})
        self.assertEqual(code, 1)
        self.assertIn('checkout', out)
        self.assertIn('pi', out)

    def test_missing_config_is_one_line_exit_2(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', os.path.join(self.tmp, 'nope.toml'), 'status'])
        self.assertEqual(code, 2)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_tickets_json_lists_stages(self):
        self.add_ticket()
        code, out, _ = self.cli('tickets', '--json')
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([(r['id'], r['name'], r['stage'], r['number']) for r in rows],
                         [('tk-1', 'fix-it', 'working/queued', 1)])

    def test_tickets_table_and_status(self):
        self.add_ticket()
        code, out, _ = self.cli('tickets')
        self.assertEqual(code, 0)
        self.assertIn('fix-it', out)
        code, out, _ = self.cli('status')
        status = json.loads(out)
        self.assertEqual((status['repo'], status['agent_login'], status['token']), ('o/r', 'bot', 'no'))
        self.assertEqual(status['open_tickets'], {'working/queued': 1})

    def test_stop_records_a_request_for_the_service(self):
        store, t = self.add_ticket()
        code, out, _ = self.cli('stop', 'fix-it')
        self.assertEqual(code, 0)
        self.assertTrue(store.get('operator:stop:tk-1'))
        self.assertEqual(store.ticket('tk-1').state, 'open')  # the service applies it

    def test_retry_records_a_request_and_unknown_ticket_is_exit_1(self):
        store, _ = self.add_ticket()
        self.assertEqual(self.cli('retry', 'tk-1')[0], 0)
        self.assertTrue(store.get('operator:retry:tk-1'))
        code, _, err = self.cli('stop', 'nope')
        self.assertEqual(code, 1)
        self.assertIn('nope', err)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
