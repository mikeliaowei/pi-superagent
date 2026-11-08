"""Local requests (S15): `superagent ask/show/answer`, the local outbox, and the Pi skill and prompt template."""

import asyncio
import getpass
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from superagent import cli
from superagent.executor import followup_message
from superagent.model import stage
from superagent.service import Service
from superagent.store import Store
from tests.fake_github import FakeGitHub, FakeGitHubs
from tests.test_crossrepo import commit_in
from tests.test_multirepo import ServiceBase, make_clone, write
from tests.test_service import GIT_ENV, Pi, until

WAITING = {'report': {'status': 'waiting', 'summary': 'Need a choice.', 'question': 'Which database?'}}


class CliBase(unittest.TestCase):
    """A config file with two [[repos]] and `local_user = "wei"`, and the CLI run in-process."""

    REPOS = ('a/one', 'b/two')

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-local-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, 'state')
        self.config = self.write_config(self.REPOS, local_user='wei')

    def write_config(self, repos, local_user=None):
        text = (f'[github]\nagent_login = "bot"\ntoken_env = "SA_LOCAL_TOKEN"\n[policy]\nallowed = ["alice"]\n'
                + (f'local_user = "{local_user}"\n' if local_user else '')
                + f'[service]\nstate_dir = "{self.state}"\nreport_port = 0\n'
                + ''.join(f'[[repos]]\nname = "{r}"\ncheckout = "{self.tmp}/{r.split("/")[1]}"\n' for r in repos))
        return write(os.path.join(self.tmp, 'config.toml'), text)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def store(self):
        return Store(os.path.join(self.state, 'superagent.sqlite'))


class AskTest(CliBase):
    def test_ask_queues_one_request_and_prints_its_id(self):
        code, out, err = self.cli('ask', '--repo', 'a/one', 'Fix the flaky test')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(out.splitlines()[0], 'queued request 1 for a/one')
        self.assertIn('superagent run', out)
        self.assertIn('not running', out)  # no poll recorded yet
        (r,) = self.store().local_requests()
        self.assertEqual((r['id'], r['kind'], r['text'], json.loads(r['repos']), r['actor']),
                         (1, 'ask', 'Fix the flaky test', ['a/one'], 'wei'))
        code, out, _ = self.cli('ask', '--repo', 'a/one', 'Another one')
        self.assertTrue(out.startswith('queued request 2 for a/one'))
        self.assertEqual(len(self.store().local_requests()), 2)

    def test_ask_says_the_service_looks_running_after_a_recent_poll(self):
        self.store().put('last-poll', datetime.now(timezone.utc).isoformat(timespec='seconds'))
        code, out, _ = self.cli('ask', '--repo', 'b/two', 'Do it')
        self.assertEqual(code, 0)
        self.assertIn('the running service', out)
        self.assertNotIn('not running', out)

    def test_several_repos_and_no_repo_is_an_error_listing_them(self):
        code, out, err = self.cli('ask', 'Fix it')
        self.assertEqual(code, 1)
        self.assertIn('several repos are configured; name one with --repo: a/one, b/two', err)
        self.assertEqual(self.store().local_requests(), [])

    def test_unknown_repo_is_an_error_naming_the_configured_ones(self):
        code, _, err = self.cli('ask', '--repo', 'x/y', 'Fix it')
        self.assertEqual(code, 1)
        self.assertIn('repo x/y is not configured (configured: a/one, b/two)', err)
        self.assertEqual(self.store().local_requests(), [])

    def test_repeated_repo_flags_and_repos_named_in_the_text_are_all_kept(self):
        code, out, _ = self.cli('ask', '--repo', 'B/Two', 'Rename get in b/two and in a/one')
        self.assertEqual(out.splitlines()[0], 'queued request 1 for b/two, a/one')
        code, out, _ = self.cli('ask', '--repo', 'a/one', '--repo', 'b/two', 'Rename it')
        self.assertEqual(out.splitlines()[0], 'queued request 2 for a/one, b/two')
        self.assertEqual([json.loads(r['repos']) for r in self.store().local_requests()],
                         [['b/two', 'a/one'], ['a/one', 'b/two']])

    def test_without_repo_flags_the_configured_repos_the_text_names_are_used(self):
        code, out, _ = self.cli('ask', 'fix the flaky test in b/two')
        self.assertEqual((code, out.splitlines()[0]), (0, 'queued request 1 for b/two'))

    def test_the_only_configured_repo_is_the_default_and_local_user_defaults_to_the_os_user(self):
        self.config = self.write_config(['a/one'])
        code, out, _ = self.cli('ask', 'Fix it')
        self.assertEqual(out.splitlines()[0], 'queued request 1 for a/one')
        self.assertEqual(self.store().local_requests()[0]['actor'], getpass.getuser())

    def test_show_of_a_request_not_yet_picked_up_says_it_is_queued(self):
        self.cli('ask', '--repo', 'a/one', 'Fix it')
        code, out, _ = self.cli('show', 'local:1', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {'request': 1, 'state': 'queued', 'repos': ['a/one'], 'text': 'Fix it'})
        code, _, err = self.cli('show', 'nope')
        self.assertEqual(code, 1)
        self.assertIn("no ticket 'nope'", err)


class LocalServiceTest(ServiceBase):
    async def test_a_request_interrupted_before_its_launch_is_launched_next_round(self):
        self.start({'mode': 'hang'})
        self.ask('Fix x', 'a/one')
        real = self.store.enqueue

        def flaky(kind, *a, **k):
            if kind == 'launch':
                raise sqlite3.OperationalError('database is locked')
            return real(kind, *a, **k)
        with mock.patch.object(self.store, 'enqueue', flaky):
            await self.svc.poll_once()
        await self.round()
        await until(lambda: self.pi.runs and self.pi.runs[0].messages)
        self.assertEqual(len(self.store.tickets()), 1)
        self.assertEqual(len(self.effect_keys('launch')), 1)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.one_co, self.two_co = make_clone(self.tmp, 'one'), make_clone(self.tmp, 'two')
        self.one, self.two = FakeGitHub('a/one'), FakeGitHub('b/two')
        self.fakes = FakeGitHubs(self.one, self.two)

    def start(self, *steps):
        cfg = self.config(repos=[{'name': 'a/one', 'checkout': self.one_co}, {'name': 'b/two', 'checkout': self.two_co}],
                          local_user='wei')
        self.pi = Pi(*steps)
        return self.service(cfg, self.fakes, self.pi)

    def ask(self, text, *repos):
        rid, _ = cli.ask_request(self.cfg, self.store, text, list(repos))
        return rid

    def ticket(self):
        tickets = self.store.tickets()
        self.assertEqual(len(tickets), 1)
        return tickets[0]

    def outbox(self, t):
        return [m['body'] for m in self.store.local_messages(t.id) if m['kind'] == 'reply']

    def github_writes(self):
        return [c for c in self.fakes.calls if c.method != 'GET']

    async def test_a_queued_request_becomes_exactly_one_ticket_with_an_ack_in_the_local_outbox(self):
        self.start({'mode': 'hang'})
        rid = self.ask('Fix the flaky test\nIt fails on CI.', 'a/one')
        await self.round()
        await until(lambda: self.pi.runs and self.pi.runs[0].messages)
        t = self.ticket()
        self.assertEqual((t.thread_key, t.repo, t.number, t.title), (f'local:{rid}', 'a/one', 0, 'Fix the flaky test'))
        self.assertEqual((t.mandate['actor'], t.mandate['strength']), ('wei', 'explicit'))
        (trig, decision, reason), = self.store.recent_triggers()
        self.assertEqual((trig.kind, trig.human, trig.addressed, trig.thread_key), ('local', True, True, f'local:{rid}'))
        self.assertEqual((decision, reason), ('accept', 'local request'))
        self.assertIn('Fix the flaky test', self.pi.runs[0].messages[0])
        self.assertEqual(len(self.outbox(t)), 1)
        self.assertTrue(self.outbox(t)[0].startswith(f"On it: I'll work on this as `{t.name}`"))
        self.assertEqual((self.one.posted(), self.github_writes()), ([], []))
        self.assertFalse([c for c in self.fakes.calls if c.path.endswith('/issues/0')])  # never read as an issue
        self.assertEqual(self.store.local_requests(), [])  # processed

        # Processed once, also across a restart and when the request was not marked done before a crash.
        await self.svc.close()
        self.store.db.execute('UPDATE local_requests SET done = 0')
        self.store.db.commit()
        self.service(self.cfg, self.fakes, Pi({'mode': 'hang'}))
        await self.svc.poll_once()
        self.assertEqual(len(self.store.tickets()), 1)
        self.assertEqual(len(self.outbox(self.ticket())), 1)
        self.assertEqual(self.store.local_requests(), [])

    async def test_a_waiting_question_shows_in_show_and_answer_resumes_the_same_session(self):
        self.start(WAITING, {'mode': 'hang'})
        self.ask('Add storage', 'a/one')
        await self.round()
        await self.drain()
        t = self.ticket()
        self.assertEqual(stage(t), 'person/answer')
        data = cli.show_data(self.cfg, self.store, t.name)
        self.assertEqual((data['stage'], data['repos'], data['thread']), ('person/answer', ['a/one'], t.thread_key))
        self.assertEqual([(m['from'], m['kind'], m['text']) for m in data['conversation']][0],
                         ('wei', 'request', 'Add storage'))
        self.assertEqual(data['conversation'][-1]['text'], 'Question: Which database?')
        self.assertIn('Question: Which database?', cli.show_text(data))

        line = cli.answer_request(self.cfg, self.store, t.name, 'Use SQLite.')
        self.assertIn(t.name, line)
        await self.svc.poll_once()
        await self.svc.run_effects()
        await until(lambda: len(self.pi.runs) == 2 and self.pi.runs[1].messages)
        self.assertEqual(self.pi.runs[1].session_id(), self.pi.runs[0].session_id())
        self.assertEqual(self.pi.runs[1].messages[0], followup_message([{'text': 'Use SQLite.'}]))
        t = self.store.ticket(t.id)
        self.assertEqual((len(t.assignments), t.current.state, t.last_human_at is not None), (2, 'running', True))
        convo = cli.show_data(self.cfg, self.store, t.id)['conversation']
        self.assertEqual((convo[-1]['from'], convo[-1]['kind'], convo[-1]['text']), ('wei', 'answer', 'Use SQLite.'))
        await self.svc.poll_once()  # applied once
        self.assertEqual(len(self.pi.runs), 2)
        self.assertEqual(len(self.store.inputs(t.id)), 1)
        self.assertEqual(self.github_writes(), [])

    async def test_ready_opens_a_pr_that_says_requested_locally_and_links_it_in_the_outbox(self):
        self.start({'commit': True, 'report': {'status': 'ready', 'summary': 'Fixed the test.'}})
        self.ask('Fix the flaky test', 'a/one')
        await self.round()
        await self.drain()
        t = self.ticket()
        (pr,) = list(self.one.pulls)
        body = self.one.pulls[pr]['body']
        self.assertIn('Requested locally by wei', body)
        self.assertNotIn('Closes', body)
        self.assertEqual(t.pr_number, pr)
        self.assertIn(f'https://github.com/a/one/pull/{pr}', self.outbox(t)[-1])
        self.assertEqual(self.one.posted(), [])  # no comment anywhere: replies went to the outbox
        data = cli.show_data(self.cfg, self.store, t.name)
        self.assertEqual(data['prs'], [f'https://github.com/a/one/pull/{pr}'])

    async def test_a_cross_repo_local_ask_opens_two_prs(self):
        self.start({'before': commit_in('a__one', 'b__two'), 'report': {'status': 'ready', 'summary': 'Changed both.'}})
        self.ask('Rename the API', 'a/one', 'b/two')
        await self.round()
        await self.drain()
        t = self.ticket()
        self.assertEqual(t.repos, ['a/one', 'b/two'])
        (a_pr,), (b_pr,) = list(self.one.pulls), list(self.two.pulls)
        for fake, pr in ((self.one, a_pr), (self.two, b_pr)):
            body = fake.pulls[pr]['body']
            self.assertIn('Requested locally by wei', body)
            self.assertNotIn('Closes', body)
            self.assertNotIn('/issues/', body)
            self.assertIn('Part of a cross-repo change:', body)
        reply = self.outbox(t)[-1]
        self.assertIn(f'https://github.com/a/one/pull/{a_pr}', reply)
        self.assertIn(f'https://github.com/b/two/pull/{b_pr}', reply)
        self.assertEqual((self.one.posted(), self.two.posted()), ([], []))

    async def test_operator_stop_ends_a_local_ticket_and_says_so_in_the_outbox(self):
        self.start({'mode': 'hang'})
        self.ask('Fix it', 'b/two')
        await self.round()
        t = self.ticket()
        cli.request_action(self.store, t.name, 'stop')
        await self.round()
        t = self.store.ticket(t.id)
        self.assertEqual((t.state, t.outcome), ('ended', 'stopped'))
        self.assertEqual(self.outbox(t)[-1], 'Stopped by the operator.')
        with self.assertRaisesRegex(RuntimeError, f'ticket {t.name} already ended \\(stopped\\)'):
            cli.answer_request(self.cfg, self.store, t.name, 'More')


class LocalCliTest(CliBase):
    def setUp(self):
        super().setUp()
        self.cli('ask', '--repo', 'a/one', 'Fix it')
        from superagent.config import load
        cfg = load(self.config)
        store = self.store()
        svc = Service(cfg, store, gh=None)
        svc._take_ask(store.local_requests()[0])  # what the running service does with the request
        for e in store.due_effects(kinds=('reply',)):
            asyncio.run(svc._effect_reply(e))  # the ack, into the local outbox
        self.ticket = store.tickets()[0]

    def test_tickets_lists_local_tickets_with_their_repo_and_thread(self):
        code, out, _ = self.cli('tickets')
        self.assertEqual(code, 0)
        header, row = out.splitlines()
        self.assertEqual(header.split(), ['ID', 'NAME', 'REPO', 'STAGE', '#', 'PR', 'LAST', 'REPORT'])
        self.assertEqual(row.split()[:5], [self.ticket.id, self.ticket.name, 'a/one', 'working/queued', 'local'])
        code, out, _ = self.cli('tickets', '--json')
        (row,) = json.loads(out)
        self.assertEqual((row['repo'], row['thread'], row['number']), ('a/one', 'local:1', 0))

    def test_show_json_and_text_print_the_header_and_conversation(self):
        code, out, _ = self.cli('show', self.ticket.name, '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data['id'], data['name'], data['stage'], data['repos'], data['prs'], data['requested_by']),
                         (self.ticket.id, self.ticket.name, 'working/queued', ['a/one'], [], 'wei'))
        self.assertEqual([(m['from'], m['kind']) for m in data['conversation']], [('wei', 'request'), ('superagent', 'reply')])
        code, out, _ = self.cli('show', 'local:1')
        self.assertEqual(code, 0)
        self.assertIn(f'{self.ticket.name} ({self.ticket.id})', out)
        self.assertIn('working/queued', out)
        self.assertIn('wei: Fix it', out)
        self.assertIn("superagent: On it: I'll work on this", out)
        self.assertNotIn('github.com', out)

    def test_answer_queues_an_operator_request_and_records_it_in_the_conversation(self):
        code, out, err = self.cli('answer', self.ticket.name, 'Use SQLite.')
        self.assertEqual((code, err), (0, ''))
        self.assertIn(f'answer queued for {self.ticket.name}', out)
        store = self.store()
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['ticket_id'], r['text'], r['actor']), ('answer', self.ticket.id, 'Use SQLite.', 'wei'))
        self.assertEqual(store.local_messages(self.ticket.id)[-1]['body'], 'Use SQLite.')
        self.assertEqual(store.inputs(self.ticket.id), [])  # the running service applies it

    def test_answer_on_an_ended_ticket_is_a_clear_error(self):
        store = self.store()
        t = store.ticket(self.ticket.id)
        t.state, t.outcome = 'ended', 'merged'
        store.save_ticket(t)
        code, _, err = self.cli('answer', t.name, 'More')
        self.assertEqual(code, 1)
        self.assertIn(f'ticket {t.name} already ended (merged)', err)
        self.assertEqual(store.local_requests(), [])


class StoreCompatTest(unittest.TestCase):
    OLD_SCHEMA = """
    CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS trigger_reasons (id TEXT PRIMARY KEY, reason TEXT);
    CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, thread_key TEXT, state TEXT, data TEXT);
    CREATE TABLE IF NOT EXISTS inputs (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, kind TEXT, text TEXT, trigger_id TEXT,
      delivered INTEGER DEFAULT 0, at TEXT
    );
    CREATE TABLE IF NOT EXISTS effects (
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, data TEXT, attempts INTEGER DEFAULT 0,
      next_at TEXT, done INTEGER DEFAULT 0, last_error TEXT DEFAULT '', UNIQUE (kind, key)
    );
    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-compat-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, 'db.sqlite')

    def columns(self, db):
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name != "
                                           "'sqlite_sequence' ORDER BY name")]
        return {t: [r[1] for r in db.execute(f'PRAGMA table_info({t})')] for t in tables}

    def test_an_older_database_gains_the_local_tables_without_altering_its_own(self):
        db = sqlite3.connect(self.path)
        db.executescript(self.OLD_SCHEMA)
        db.execute("INSERT INTO kv VALUES ('last-poll', '\"x\"')")
        db.commit()
        before = self.columns(db)
        db.close()
        s = Store(self.path)
        after = self.columns(s.db)
        self.assertEqual({t: c for t, c in after.items() if t in before}, before)
        self.assertEqual(set(after) - set(before), {'local_requests', 'local_messages'})
        self.assertEqual(s.get('last-poll'), 'x')
        rid = s.queue_ask('Fix it', ['o/r'], 'wei')
        self.assertEqual([r['id'] for r in s.local_requests()], [rid])

    def test_the_older_code_path_still_runs_against_a_database_with_the_local_tables(self):
        s = Store(self.path)
        s.queue_ask('Fix it', ['o/r'], 'wei')
        s.db.close()
        db = sqlite3.connect(self.path)  # what an older version does on open and while it runs
        db.executescript(self.OLD_SCHEMA)
        db.execute('INSERT INTO tickets VALUES (?, ?, ?, ?)', ('tk-1', 'github:o/r#1', 'open', '{}'))
        db.execute('INSERT INTO inputs (ticket_id, kind, text, at) VALUES (?, ?, ?, ?)', ('tk-1', 'comment', 'x', 'now'))
        db.execute("INSERT INTO effects (kind, key, data, next_at) VALUES ('reply', 'k', '{}', 'now')")
        db.commit()
        self.assertEqual(db.execute('SELECT count(*) FROM local_requests').fetchone()[0], 1)


class PiInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-pi-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.agent = os.path.join(self.tmp, 'agent')
        self.skill = os.path.join(self.agent, 'skills', 'superagent', 'SKILL.md')
        self.prompt = os.path.join(self.agent, 'prompts', 'superagent.md')

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        environ = {k: v for k, v in os.environ.items() if k != 'PI_CODING_AGENT_DIR'}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*argv])
        return code, out.getvalue(), err.getvalue()

    def read(self, path):
        with open(path) as f:
            return f.read()

    def frontmatter(self, text):
        self.assertTrue(text.startswith('---\n'))
        head = text[4:text.index('\n---', 3)]
        fields = {}
        for line in head.splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                k, _, v = line.partition(':')
                fields[k.strip()] = v.strip().strip('"')
        return fields

    def test_install_writes_the_skill_and_template_with_the_absolute_executable_path(self):
        code, out, err = self.run_cli('pi-install', '--agent-dir', self.agent)
        self.assertEqual((code, err), (0, ''))
        command = cli.superagent_command()
        self.assertTrue(os.path.isabs(command.split()[0]))
        skill, prompt = self.read(self.skill), self.read(self.prompt)
        for text in (skill, prompt):
            self.assertIn(command, text)
            self.assertIn(cli.PI_MARKER, text)
            self.assertNotIn('{{', text)
        self.assertNotIn('--config', skill)  # the default config needs no flag
        fields = self.frontmatter(skill)
        self.assertEqual(fields['name'], 'superagent')
        self.assertTrue(fields['description'])
        for cmd in (f'{command} ask --repo', f'{command} tickets --json', f'{command} show', f'{command} answer',
                    f'{command} stop', f'{command} retry'):
            self.assertIn(cmd, skill)
        self.assertIn('description', self.frontmatter(prompt))
        self.assertIn('$ARGUMENTS', prompt)
        self.assertIn(self.skill, prompt)
        self.assertIn(self.skill, out)
        self.assertIn(self.prompt, out)
        self.assertIn('/reload', out)

    def test_agent_dir_defaults_to_pi_coding_agent_dir_and_a_custom_config_is_substituted(self):
        config = os.path.join(self.tmp, 'my config.toml')
        code, _, _ = self.run_cli('--config', config, 'pi-install', env={'PI_CODING_AGENT_DIR': self.agent})
        self.assertEqual(code, 0)
        self.assertIn(f"--config '{config}'", self.read(self.skill))

    def test_a_foreign_file_is_refused_unless_forced(self):
        os.makedirs(os.path.dirname(self.prompt))
        write(self.prompt, 'my own template\n')
        code, _, err = self.run_cli('pi-install', '--agent-dir', self.agent)
        self.assertEqual(code, 1)
        self.assertIn(f'{self.prompt} exists and was not installed by superagent; use --force to overwrite it', err)
        self.assertEqual(self.read(self.prompt), 'my own template\n')
        self.assertFalse(os.path.exists(self.skill))
        code, _, _ = self.run_cli('pi-install', '--agent-dir', self.agent, '--force')
        self.assertEqual(code, 0)
        self.assertIn(cli.PI_MARKER, self.read(self.prompt))
        self.assertEqual(self.run_cli('pi-install', '--agent-dir', self.agent)[0], 0)  # its own files: reinstall ok

    def test_uninstall_removes_only_its_own_files(self):
        self.run_cli('pi-install', '--agent-dir', self.agent)
        write(self.prompt, 'replaced by the user\n')
        code, out, _ = self.run_cli('pi-install', '--agent-dir', self.agent, '--uninstall')
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.skill))
        self.assertFalse(os.path.exists(os.path.dirname(self.skill)))
        self.assertEqual(self.read(self.prompt), 'replaced by the user\n')
        self.assertIn(f'removed {self.skill}', out)
        self.assertIn(f'kept {self.prompt}', out)


if __name__ == '__main__':
    unittest.main()
