"""Cross-repo Tickets (S14): one request, one Ticket, one worktree per repo, one pull request per changed repo."""

import asyncio
import json
import os
import unittest

from superagent import cli
from superagent.executor import first_message
from superagent.model import Ticket
from superagent.store import Store
from tests.fake_github import FakeGitHub, FakeGitHubs
from tests.test_multirepo import ServiceBase, make_clone, write
from tests.test_service import TOKEN, Pi, git, until

READY = {'report': {'status': 'ready', 'summary': 'Changed both.'}}


def commit_in(*slugs):
    """A worker step that commits one file in each named repo folder of the ticket folder (its cwd)."""
    def step(cwd, env):
        for slug in slugs:
            repo = os.path.join(cwd, slug)
            write(os.path.join(repo, 'change.txt'), f'{slug} change\n')
            git('add', 'change.txt', cwd=repo)
            git('commit', '-m', f'change {slug}', cwd=repo)
    return step


class CrossRepoTest(ServiceBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.one_co, self.two_co = make_clone(self.tmp, 'one'), make_clone(self.tmp, 'two')
        self.one, self.two, self.other = FakeGitHub('a/one'), FakeGitHub('b/two'), FakeGitHub('x/y')
        self.fakes = FakeGitHubs(self.one, self.two, self.other)

    def start(self, *steps, one_test='', two_test=''):
        cfg = self.config(repos=[{'name': 'a/one', 'checkout': self.one_co, 'test_command': one_test},
                                 {'name': 'b/two', 'checkout': self.two_co, 'test_command': two_test}])
        self.pi = Pi(*steps)
        return self.service(cfg, self.fakes, self.pi)

    def ticket(self):
        tickets = self.store.tickets()
        self.assertEqual(len(tickets), 1)
        return tickets[0]

    async def until_effects(self, pred):
        """Run due effects until `pred` holds."""
        async def step():
            await self.svc.run_effects()
            return pred()
        for _ in range(500):
            if await step():
                return
            await asyncio.sleep(0.02)
        raise AssertionError('condition not reached')

    def replies(self):
        return [p.split('<!--')[0].strip() for p in self.one.posted(1)]

    async def test_a_request_naming_a_second_configured_repo_gets_one_ticket_with_a_worktree_per_repo(self):
        self.one.add_issue('Rename the API', '@bot rename `get` in this repo and update the client in B/Two too.')
        self.start({'report': {'status': 'waiting', 'summary': 'q', 'question': 'Which name?'}})
        await self.round()
        await until(lambda: self.pi.runs and self.pi.runs[0].messages)
        await self.drain()
        t = self.ticket()
        folder = os.path.join(self.cfg.work_dir, 'tickets', t.name)
        self.assertEqual([p['repo'] for p in t.parts], ['a/one', 'b/two'])  # the thread's repo first
        self.assertEqual((t.repo, t.number, t.worktree, t.branch), ('a/one', 1, folder, f'agent/{t.name}'))
        for part, slug, name in zip(t.parts, ('one', 'two'), ('a__one', 'b__two')):
            self.assertEqual(part['worktree'], os.path.join(folder, name))
            self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD', cwd=part['worktree']).stdout.strip(),
                             f'agent/{t.name}')
            self.assertEqual(git('remote', 'get-url', 'origin', cwd=part['worktree']).stdout.strip(),
                             os.path.join(self.tmp, f'{slug}.git'))
        self.assertEqual(os.path.realpath(self.pi.runs[0].cwd), os.path.realpath(folder))
        msg = self.pi.runs[0].messages[0]
        for part in t.parts:
            self.assertIn(part['repo'], msg)
            self.assertIn(part['worktree'], msg)
        self.assertIn('"base_branch": "main"', msg)
        self.assertIn('commit in each repo you change', msg)
        self.assertEqual(len(self.store.tickets()), 1)
        self.assertEqual((self.two.posted(), self.two.pulls), ([], {}))

    async def test_unconfigured_and_quoted_repo_names_leave_a_single_repo_ticket(self):
        self.one.add_issue('Fix it', '> earlier: b/two was mentioned here\n@bot fix it like x/y does, see a/one/b/two')
        self.start({'mode': 'hang'})
        await self.round()
        await until(lambda: self.pi.runs and self.pi.runs[0].messages)
        t = self.ticket()
        self.assertEqual(t.parts, [])
        self.assertEqual(t.worktree, os.path.join(self.cfg.work_dir, 'a__one', t.name))
        self.assertEqual(self.pi.runs[0].messages[0],
                         first_message(t, self.store.trigger(t.mandate['trigger_id']).text))

    async def deliver_both(self, **tests):
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start({'before': commit_in('a__one', 'b__two'), **READY}, **tests)
        await self.round()
        await self.drain()
        return self.ticket()

    async def test_ready_with_commits_in_both_tests_both_and_opens_two_linked_prs_and_one_reply(self):
        seen = os.path.join(self.tmp, 'seen')
        os.makedirs(seen)
        t = await self.deliver_both(one_test=f'pwd > {seen}/one; echo ok', two_test=f'pwd > {seen}/two; echo ok')
        for slug, part in zip(('one', 'two'), t.parts):
            self.assertEqual(os.path.realpath(open(f'{seen}/{slug}').read().strip()), os.path.realpath(part['worktree']))
        (a_pr,), (b_pr,) = list(self.one.pulls), list(self.two.pulls)
        self.assertEqual([p['pr_number'] for p in t.parts], [a_pr, b_pr])
        a_url, b_url = f'https://github.com/a/one/pull/{a_pr}', f'https://github.com/b/two/pull/{b_pr}'
        a_body, b_body = self.one.pulls[a_pr]['body'], self.two.pulls[b_pr]['body']
        for body, other, command in ((a_body, b_url, f'pwd > {seen}/one'), (b_body, a_url, f'pwd > {seen}/two')):
            self.assertIn('Part of a cross-repo change:', body)
            self.assertIn(other, body)
            self.assertIn(f'Tests: `{command}; echo ok` passed in ', body)
        self.assertIn('Closes #1', a_body)
        self.assertNotIn('Closes', b_body)  # #1 in b/two is another issue
        self.assertIn('https://github.com/a/one/issues/1', b_body)
        for fake, pr in ((self.one, a_pr), (self.two, b_pr)):
            self.assertEqual((fake.pulls[pr]['head'], fake.pulls[pr]['base']), (f'agent/{t.name}', 'main'))
        replies = self.replies()
        self.assertEqual(len(replies), 2)  # the ack and one reply listing every PR
        self.assertIn(a_url, replies[1])
        self.assertIn(b_url, replies[1])
        self.assertEqual(self.two.posted(), [])
        dirs = sorted(next(a for a in argv if a.startswith('--git-dir=')) for argv, _ in self.git.pushes)
        self.assertEqual(dirs, [f'--git-dir={self.state}/push/a__one.git', f'--git-dir={self.state}/push/b__two.git'])
        self.assertEqual(len(self.effect_keys('test')), 1)

        # A replayed delivery neither pushes nor opens again.
        self.store.enqueue('deliver', f'{t.id}:{t.current.id}:replay', {'ticket_id': t.id, 'assignment_id': t.current.id})
        await self.svc.run_effects()
        self.assertEqual((len(self.git.pushes), len(self.one.pulls), len(self.two.pulls)), (2, 1, 1))

    async def test_commits_in_only_one_repo_open_one_pr_and_the_reply_names_the_other_unchanged(self):
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start({'before': commit_in('a__one'), **READY})
        await self.round()
        await self.drain()
        t = self.ticket()
        (a_pr,) = list(self.one.pulls)
        self.assertEqual(self.two.pulls, {})
        self.assertEqual([p['pr_number'] for p in t.parts], [a_pr, None])
        self.assertNotIn('Part of a cross-repo change', self.one.pulls[a_pr]['body'])
        self.assertEqual(len(self.git.pushes), 1)
        reply = self.replies()[1]
        self.assertIn(f'https://github.com/a/one/pull/{a_pr}', reply)
        self.assertIn('b/two: no changes', reply)

    async def test_no_commits_anywhere_is_the_existing_no_changes_reply(self):
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start(dict(READY))
        await self.round()
        await self.drain()
        self.assertEqual((self.one.pulls, self.two.pulls, self.git.pushes), ({}, {}, []))
        self.assertEqual(self.replies()[1], 'I finished, but there were no changes to propose.')

    async def test_a_test_failure_in_one_repo_opens_no_prs_and_resumes_the_session_naming_that_repo(self):
        def fix(cwd, env):
            write(os.path.join(cwd, 'b__two', 'ok.txt'), 'ok\n')
            git('add', 'ok.txt', cwd=os.path.join(cwd, 'b__two'))
            git('commit', '-m', 'fix', cwd=os.path.join(cwd, 'b__two'))
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start({'before': commit_in('a__one', 'b__two'), **READY},
                   {'before': fix, 'report': {'status': 'ready', 'summary': 'Fixed b/two.'}},
                   one_test='echo one fine', two_test="test -f ok.txt || { echo two-broken; exit 4; }")
        await self.round()
        await self.until_effects(lambda: len(self.pi.runs) == 2 and self.pi.runs[1].messages)
        t = self.ticket()
        msg = self.pi.runs[1].messages[0]
        self.assertEqual(self.pi.runs[1].session_id(), t.id)
        self.assertIn('b/two', msg)
        self.assertIn('exit code 4', msg)
        self.assertIn('two-broken', msg)
        self.assertNotIn('one fine', msg)  # a passing repo is not a failure to fix
        self.assertEqual(t.test_failures, 1)
        self.assertEqual((self.one.pulls, self.two.pulls, self.git.pushes), ({}, {}, []))
        await self.drain()
        t = self.ticket()
        self.assertEqual((len(self.one.pulls), len(self.two.pulls), t.test_failures), (1, 1, 0))
        self.assertEqual(len(self.replies()), 2)  # the ack and the PR list: no noise about the failure

    async def test_tests_still_failing_after_the_retries_block_the_whole_ticket(self):
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start({'before': commit_in('a__one', 'b__two'), **READY}, dict(READY), dict(READY),
                   two_test='echo nope; exit 1')
        await self.round()
        await self.drain()
        t = self.ticket()
        self.assertEqual((len(self.pi.runs), t.test_failures, t.current.report), (3, 3, 'blocked'))
        self.assertEqual((self.one.pulls, self.two.pulls, self.git.pushes), ({}, {}, []))
        self.assertIn('b/two', self.replies()[-1])
        self.assertIn('tests still failing', self.replies()[-1])

    async def test_the_ticket_ends_merged_only_when_every_pr_merged(self):
        t = await self.deliver_both()
        a_pr, b_pr = (p['pr_number'] for p in t.parts)
        self.one.merge(a_pr)
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).state, 'open')  # b/two's PR is still open
        self.two.merge(b_pr)
        await self.svc.check_pulls()
        self.assertEqual((self.store.ticket(t.id).state, self.store.ticket(t.id).outcome), ('ended', 'merged'))

    async def test_the_ticket_ends_closed_when_one_pr_closed_and_the_other_merged(self):
        t = await self.deliver_both()
        a_pr, b_pr = (p['pr_number'] for p in t.parts)
        self.two.close(b_pr)
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).state, 'open')  # a/one's PR is still open
        self.one.merge(a_pr)
        await self.svc.check_pulls()
        self.assertEqual(self.store.ticket(t.id).outcome, 'closed')

    async def test_stop_ends_the_whole_ticket(self):
        self.one.add_issue('Rename the API', '@bot rename it here and in b/two')
        self.start({'mode': 'hang'})
        await self.round()
        await until(lambda: self.svc.workers and next(iter(self.svc.workers.values())).running)
        self.one.add_comment(1, '@bot stop')
        await self.round()
        t = self.ticket()
        self.assertEqual((t.state, t.outcome), ('ended', 'stopped'))
        self.assertIsNotNone(self.pi.runs[0].proc.returncode)
        self.assertEqual(self.svc.workers, {})
        self.assertIn('Stopped', self.replies()[-1])

    async def test_the_token_never_reaches_test_commands_or_posted_bodies(self):
        seen = os.path.join(self.tmp, 'seen')
        os.makedirs(seen)
        t = await self.deliver_both(one_test=f'env > {seen}/one', two_test=f'env > {seen}/two')
        for slug in ('one', 'two'):
            env = open(f'{seen}/{slug}').read()
            for secret in (TOKEN, 'gh-secret-456', 'SUPERAGENT_GITHUB_TOKEN', 'GH_TOKEN', t.token):
                self.assertNotIn(secret, env)
        bodies = [*self.one.posted(), *self.two.posted(),
                  *(p['body'] for f in (self.one, self.two) for p in f.pulls.values())]
        self.assertEqual(len(bodies), 4)
        self.assertFalse(any(TOKEN in b for b in bodies))
        for argv, env in self.git.pushes:
            self.assertFalse(any(TOKEN in a for a in argv))
            self.assertNotIn(TOKEN, json.dumps(env))  # only the base64 auth header carries it
        calls = [c for c in self.fakes.calls if c.method == 'PATCH']
        self.assertEqual(len(calls), 2)  # each PR body patched once with its cross links

    async def test_cli_rows_name_every_repo_and_pr(self):
        t = await self.deliver_both()
        a_pr, b_pr = (p['pr_number'] for p in t.parts)
        row = cli._row(self.store.ticket(t.id), self.cfg)
        self.assertEqual((row['repo'], row['pr']), ('a/one, b/two', f'a/one#{a_pr}, b/two#{b_pr}'))


class CompatibilityTest(unittest.TestCase):
    def test_a_ticket_saved_by_an_earlier_version_loads_as_single_repo(self):
        old = {'id': 'tk-old', 'name': 'fix-it', 'title': 'Fix it', 'thread_key': 'github:o/r#1', 'repo': 'o/r',
               'number': 1, 'mandate': {'actor': 'alice'}, 'created_at': '2026-11-08T12:00:00+00:00',
               'state': 'open', 'outcome': None, 'branch': 'agent/fix-it', 'worktree': '/w/fix-it', 'pr_number': 7,
               'token': 't', 'assignments': [], 'last_human_at': None, 'delivery_error': '', 'test_failures': 1,
               'test_output': 'boom', 'test_result': ''}
        s = Store(':memory:')
        s.db.execute('INSERT INTO tickets VALUES (?, ?, ?, ?)', ('tk-old', 'github:o/r#1', 'open', json.dumps(old)))
        t = s.ticket('tk-old')
        self.assertEqual((t.parts, t.worktree, t.pr_number, t.test_failures), ([], '/w/fix-it', 7, 1))
        self.assertEqual({k: v for k, v in t.to_dict().items() if k in old}, old)
        self.assertIsInstance(t, Ticket)


if __name__ == '__main__':
    unittest.main()
