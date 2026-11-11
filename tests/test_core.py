import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from superagent import config as cfgmod
from superagent.model import Assignment, Ticket, Trigger, short_name, stage
from superagent.policy import gate, is_stop, mentions
from superagent.store import Store

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def trig(actor='alice', text='@bot fix the bug', human=True, addressed=True, tid='t1'):
    return Trigger(id=tid, thread_key='github:o/r#1', actor=actor, human=human, addressed=addressed,
                   at='2026-11-08T12:00:00+00:00', text=text)


def ticket(**kw):
    base = dict(id='tk-1', name='fix-bug', title='Fix bug', thread_key='github:o/r#1', repo='o/r', number=1,
                mandate={'actor': 'alice'}, created_at='2026-11-08T12:00:00+00:00')
    return Ticket(**{**base, **kw})


def assignment(**kw):
    return Assignment(id='a1', **kw).__dict__


class PolicyTest(unittest.TestCase):
    def test_listed_person_addressing_the_agent_opens_work(self):
        self.assertEqual(gate(trig(), 'bot', ['Alice'], []).kind, 'accept')

    def test_unlisted_or_unaddressed_is_ignored_without_live_work(self):
        self.assertEqual(gate(trig(actor='mallory'), 'bot', ['alice'], []).kind, 'ignore')
        self.assertEqual(gate(trig(addressed=False), 'bot', ['alice'], []).kind, 'ignore')

    def test_bots_and_own_messages_are_ignored_even_on_live_threads(self):
        self.assertEqual(gate(trig(actor='ci[bot]', human=False), 'bot', ['alice'], ['tk-1']).reason, 'bot')
        self.assertEqual(gate(trig(actor='BOT'), 'bot', ['alice'], ['tk-1']).reason, 'own message')

    def test_stop_from_listed_person_targets_live_tickets(self):
        d = gate(trig(text='@bot stop please'), 'bot', ['alice'], ['tk-1'])
        self.assertEqual((d.kind, d.ticket_ids), ('stop', ('tk-1',)))

    def test_follow_ups_give_context_but_no_authority(self):
        self.assertEqual(gate(trig(actor='mallory', addressed=False), 'bot', ['alice'], ['tk-1']).kind, 'context')
        self.assertEqual(gate(trig(actor='mallory', text='@bot stop'), 'bot', ['alice'], ['tk-1']).kind, 'context')
        self.assertEqual(gate(trig(text='@bot also add tests'), 'bot', ['alice'], ['tk-1']).kind, 'context')

    def test_mention_matches_whole_login_only(self):
        self.assertTrue(mentions('hey @bot, look', 'bot'))
        self.assertFalse(mentions('hey @bot-two look', 'bot'))
        self.assertFalse(mentions('mail me at x@bot.com', 'bot'))

    def test_quoted_lines_neither_mention_nor_stop(self):
        self.assertFalse(mentions('> @bot fix it\nthanks for the fix', 'bot'))
        self.assertTrue(mentions('> earlier\n@bot fix it', 'bot'))
        self.assertFalse(is_stop('  > @bot stop\nI disagree with this', 'bot'))
        self.assertTrue(is_stop('> quote\n@bot stop', 'bot'))

    def test_stop_uses_the_same_mention_boundary(self):
        self.assertTrue(is_stop('@BOT stop now', 'bot'))
        self.assertFalse(is_stop('write to x@bot stop', 'bot'))
        self.assertFalse(is_stop('@bot-two stop', 'bot'))


class ModelTest(unittest.TestCase):
    def test_short_name_is_kebab_and_unique(self):
        self.assertEqual(short_name('Fix the login page crash on Safari!'), 'fix-the-login-page-crash-on-safari')
        self.assertEqual(short_name('Fix the login page', taken={'fix-the-login-page'}), 'fix-the-login-page-2')
        self.assertEqual(short_name('!!!'), 'task')

    def test_short_name_keeps_the_whole_title_up_to_8_words_and_48_characters(self):
        self.assertEqual(short_name('Fix Nav manager not showing issue'), 'fix-nav-manager-not-showing-issue')
        self.assertEqual(short_name('one two three four five six seven eight nine'), 'one-two-three-four-five-six-seven-eight')
        long = short_name('Refactor authentication middleware configuration loader properly now')
        self.assertEqual(long, 'refactor-authentication-middleware-configuration')
        self.assertLessEqual(len(long), 48)
        self.assertEqual(len(short_name('x' * 60)), 48)
        self.assertEqual(short_name('a' * 48, taken={'a' * 48}), 'a' * 45 + '-2')

    def test_stage_rows_in_order(self):
        self.assertEqual(stage(ticket()), 'working/queued')
        self.assertEqual(stage(ticket(assignments=[assignment(state='running')])), 'working')
        self.assertEqual(stage(ticket(assignments=[assignment(state='running', report='waiting', question='Which?')])),
                         'person/answer')
        ended = '2026-11-08T13:00:00+00:00'
        ready = ticket(assignments=[assignment(state='ended', report='ready', ended_at=ended)])
        self.assertEqual(stage(ready), 'person/review')
        ready.last_human_at = '2026-11-08T14:00:00+00:00'  # a person answered after the report
        self.assertEqual(stage(ready), 'idle')
        self.assertEqual(stage(ticket(assignments=[assignment(state='ended', report='failed', ended_at=ended)])),
                         'person/failed')
        self.assertEqual(stage(ticket(delivery_error='push refused', assignments=[assignment()])), 'person/delivery')
        self.assertEqual(stage(ticket(state='ended', outcome='merged')), 'ended')


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(':memory:')

    def test_trigger_dedupe_and_ticket_round_trip(self):
        self.assertTrue(self.s.add_trigger(trig(), 'accept'))
        self.assertFalse(self.s.add_trigger(trig(), 'accept'))
        t = ticket(token='secret', assignments=[assignment(state='running')])
        self.s.save_ticket(t)
        self.assertEqual(self.s.ticket('tk-1').current.state, 'running')
        self.assertEqual([x.id for x in self.s.tickets(state='open', thread_key='github:o/r#1')], ['tk-1'])
        self.assertEqual(self.s.ticket_by_token('secret').id, 'tk-1')
        self.assertIsNone(self.s.ticket_by_token(''))

    def test_inputs_are_delivered_once(self):
        self.s.add_input('tk-1', 'trigger', 'please add tests', 't2')
        pending = self.s.pending_inputs('tk-1')
        self.assertEqual([p['text'] for p in pending], ['please add tests'])
        self.s.mark_delivered([p['seq'] for p in pending])
        self.assertEqual(self.s.pending_inputs('tk-1'), [])

    def test_effects_are_idempotent_and_back_off(self):
        self.assertTrue(self.s.enqueue('reply', 'm1', {'body': 'hi'}, now=NOW))
        self.assertFalse(self.s.enqueue('reply', 'm1', {'body': 'again'}, now=NOW))
        [e] = self.s.due_effects(now=NOW)
        self.assertEqual(e['data'], {'body': 'hi'})
        self.assertEqual([self.s.effect_failed(e['id'], 'boom', now=NOW) for _ in range(8)],
                         [5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(self.s.due_effects(now=NOW), [])
        self.assertEqual(len(self.s.due_effects(now=NOW + timedelta(seconds=301))), 1)
        self.s.effect_done(e['id'])
        self.assertEqual(self.s.due_effects(now=NOW + timedelta(days=1)), [])

    def test_token_lookup_compares_in_constant_time(self):
        self.s.save_ticket(ticket(token='secret'))
        with mock.patch('superagent.store.hmac.compare_digest', wraps=__import__('hmac').compare_digest) as cmp:
            self.assertEqual(self.s.ticket_by_token('secret').id, 'tk-1')
            self.assertIsNone(self.s.ticket_by_token('secreT'))
        self.assertTrue(cmp.called)

    def test_kv(self):
        self.assertEqual(self.s.get('cursor', 'none'), 'none')
        self.s.put('cursor', {'since': 'x'})
        self.assertEqual(self.s.get('cursor'), {'since': 'x'})


class StoreThreadTest(unittest.TestCase):
    def test_report_lookups_on_other_threads_never_fail_or_miss_while_the_loop_writes(self):
        tmp = tempfile.mkdtemp(prefix='superagent-store-')
        self.addCleanup(shutil.rmtree, tmp, True)
        s = Store(os.path.join(tmp, 'db.sqlite'))
        t = ticket(token='tok-1', assignments=[assignment(state='running')])
        s.save_ticket(t)
        errors, misses = [], []

        def reader():
            for _ in range(2000):
                try:
                    if s.ticket_by_token('tok-1') is None:
                        misses.append(1)
                except Exception as e:  # noqa: BLE001 - any error is the defect
                    errors.append(repr(e))

        readers = [threading.Thread(target=reader) for _ in range(2)]
        for r in readers:
            r.start()
        for i in range(2000):
            try:
                s.put('github-since', f'2026-11-08T12:00:{i % 60:02d}+00:00')
                s.save_ticket(t)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        for r in readers:
            r.join()
        self.assertEqual(errors[:3], [])
        self.assertEqual(len(misses), 0)


class ConfigTest(unittest.TestCase):
    def test_load_validates_and_hides_secrets_from_workers(self):
        with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
            f.write('[github]\nrepo = "o/r"\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n'
                    '[executor]\ncheckout = "~/src/r"\nmodel = "claude-haiku-5-5"\n')
        try:
            c = cfgmod.load(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual((c.model, c.provider, c.poll_seconds), ('claude-haiku-5-5', 'anthropic', 60))
        self.assertTrue(os.path.isabs(c.checkout))
        env = c.worker_env({'SUPERAGENT_GITHUB_TOKEN': 'x', 'GH_TOKEN': 'y', 'GITHUB_TOKEN': 'z', 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})
        with self.assertRaisesRegex(ValueError, 'poll_seconds'):
            cfgmod.Config(repo='o/r', agent_login='b', allowed=[], checkout='.', poll_seconds=5)


class ConfigUpdateTest(unittest.TestCase):
    MULTI = ('# my config\n[github]\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n\n'
             '[executor]\n# the worker model\nmodel = "m1"  # old\n\n'
             "[[repos]]\nname = \"a/one\"\ncheckout = 'C:\\src\\one'\n\n"
             '[[repos]]\nname = "b/two"\ncheckout = "/src/two"\ntest_command = "make test"\n')

    def write_config(self, text):
        d = tempfile.mkdtemp(prefix='superagent-cfg-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'config.toml')
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        return path

    def read(self, path):
        with open(path, encoding='utf-8', newline='') as f:
            return f.read()

    def test_update_changes_settings_in_place_and_keeps_the_comments(self):
        path = self.write_config(self.MULTI)
        cfgmod.update(path, {'model': 'm2', 'thinking': 'high', 'poll_seconds': 120},
                      {'B/Two': {'test_command': 'make "check"', 'test_timeout': 60},
                       'a/one': {'checkout': 'D:\\work\\one'}})
        text = self.read(path)
        for kept in ('# my config', '# the worker model', '\n[[repos]]\nname = "a/one"\n'):
            self.assertIn(kept, text)
        self.assertIn('model = "m2"\n', text)  # replaced where it was
        c = cfgmod.load(path)
        self.assertEqual((c.model, c.thinking, c.poll_seconds), ('m2', 'high', 120))
        self.assertEqual([(r.checkout, r.test_command, r.test_timeout) for r in c.repos],
                         [(os.path.normpath('D:\\work\\one'), '', 900), (os.path.normpath('/src/two'), 'make "check"', 60)])

    def test_update_keeps_windows_line_endings_and_adds_a_missing_table(self):
        no_executor = self.MULTI.replace('[executor]\n# the worker model\nmodel = "m1"  # old\n\n', '')
        path = self.write_config(no_executor.replace('\n', '\r\n'))
        cfgmod.update(path, {'model': 'm3', 'max_workers': 4})
        self.assertNotIn('\n', self.read(path).replace('\r\n', ''))
        c = cfgmod.load(path)
        self.assertEqual((c.model, c.max_workers), ('m3', 4))

    def test_update_writes_nothing_it_cannot_do_exactly(self):
        path = self.write_config(self.MULTI)
        cases = [({'token_env': 'X'}, None, 'token_env cannot be changed here'),
                 ({'checkout': '/x'}, None, 'checkout cannot be changed here'),  # [[repos]] form: per repo
                 ({'max_workers': 99}, None, 'max_workers must be between 1 and 16'),
                 (None, {'x/y': {'checkout': '/x'}}, r'repo x/y is not a \[\[repos\]\] table'),
                 (None, {'a/one': {'name': 'B/Two'}}, 'duplicate repo in'),
                 (None, {'a/one': {'pi': '/x'}}, 'pi cannot be changed here')]
        for values, repos, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                cfgmod.update(path, values, repos)
        self.assertEqual(self.read(path), self.MULTI)
        inline = self.write_config('repos = [{name = "a/one", checkout = "/a"}]\n'
                                   '[github]\nagent_login = "bot"\n[policy]\nallowed = ["a"]\n')
        with self.assertRaisesRegex(ValueError, 'edit the config file by hand'):
            cfgmod.update(inline, None, {'a/one': {'checkout': '/b'}})

    def test_update_edits_the_single_repo_forms_checkout(self):
        path = self.write_config('[github]\nrepo = "o/r"\nagent_login = "bot"\n[policy]\nallowed = ["a"]\n'
                                 '[executor]\ncheckout = "/old"\n')
        cfgmod.update(path, {'checkout': '/new', 'base_branch': 'dev'})
        c = cfgmod.load(path)
        self.assertEqual((c.checkout, c.base_branch), (os.path.normpath('/new'), 'dev'))  # config paths are OS-normalized

    def test_add_repo_appends_a_repos_table(self):
        path = self.write_config(self.MULTI)
        cfgmod.add_repo(path, ' c/three ', 'E:\\src\\three', test_command='npm test', test_timeout=60)
        text = self.read(path)
        self.assertTrue(text.startswith(self.MULTI))  # nothing else changed
        c = cfgmod.load(path)
        self.assertEqual(c.repo_names, ['a/one', 'b/two', 'c/three'])
        r = c.repo_config('c/three')
        self.assertEqual((r.checkout, r.base_branch, r.test_command, r.test_timeout),
                         ('E:\\src\\three', 'main', 'npm test', 60))
        with self.assertRaisesRegex(ValueError, 'duplicate repo'):
            cfgmod.add_repo(path, 'A/One', '/x')
        with self.assertRaisesRegex(ValueError, 'repo must be owner/name'):
            cfgmod.add_repo(path, 'four', '/x')
        self.assertEqual(self.read(path), text)

    def test_the_worker_agent_is_pi_or_copilot_and_can_be_switched(self):
        path = self.write_config(self.MULTI)
        self.assertEqual(cfgmod.load(path).agent, 'pi')
        cfgmod.update(path, {'agent': 'copilot'})
        self.assertEqual(cfgmod.load(path).agent, 'copilot')
        with self.assertRaisesRegex(ValueError, 'agent must be one of: pi, copilot'):
            cfgmod.update(path, {'agent': 'codex'})

    def test_remove_repo_drops_its_table_and_a_rename_keeps_the_rest(self):
        path = self.write_config(self.MULTI)
        cfgmod.update(path, repos={'b/two': {'name': 'c/three'}})
        self.assertEqual(cfgmod.load(path).repo_config('c/three').test_command, 'make test')
        cfgmod.remove_repo(path, 'A/ONE')
        text = self.read(path)
        self.assertNotIn('a/one', text)
        self.assertIn('# the worker model', text)
        self.assertEqual(cfgmod.load(path).repo_names, ['c/three'])
        with self.assertRaisesRegex(ValueError, 'only repo'):
            cfgmod.remove_repo(path, 'c/three')
        with self.assertRaisesRegex(ValueError, 'not a \\[\\[repos\\]\\] table'):
            cfgmod.remove_repo(path, 'x/y')
        self.assertEqual(self.read(path), text)

    def test_add_repo_moves_a_single_repo_config_to_repos_tables(self):
        single = ('# mine\r\n[github]\r\nrepo = "o/r"  # the repo\r\nagent_login = "bot"\r\n[policy]\r\n'
                  'allowed = ["a"]\r\n[executor]\r\n# my clone\r\ncheckout = "~/src/r"\r\nbase_branch = "dev"\r\n')
        path = self.write_config(single)
        cfgmod.add_repo(path, 'x/new', '/src/new')
        text = self.read(path)
        self.assertNotIn('\n', text.replace('\r\n', ''))
        for kept in ('# mine', '# my clone', 'base_branch = "dev"'):
            self.assertIn(kept, text)
        self.assertNotIn('repo = "o/r"', text)
        c = cfgmod.load(path)
        self.assertFalse(c.legacy)
        self.assertEqual([(r.name, r.checkout, r.base_branch) for r in c.repos],
                         [('o/r', os.path.normpath(os.path.expanduser('~/src/r')), 'dev'), ('x/new', os.path.normpath('/src/new'), 'dev')])


if __name__ == '__main__':
    unittest.main()


OLD_TRIGGER_JSON = ('{"id": "old", "thread_key": "github:o/r#1", "actor": "alice", "human": true, "addressed": true, '
                    '"at": "a", "text": "x"}')


class StoreCompatibilityTest(unittest.TestCase):
    def tmpdir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_an_older_version_can_still_record_triggers_after_an_upgrade(self):
        import sqlite3
        path = os.path.join(self.tmpdir(), 's.sqlite')
        Store(path).add_trigger(trig(tid='new'), 'ignore', 'bot')
        db = sqlite3.connect(path)  # the statement released versions run
        db.execute('INSERT OR IGNORE INTO triggers VALUES (?, ?, ?, ?)', ('old', 'github:o/r#1', 'accept', '{}'))
        db.commit()
        db.close()

    def test_processes_opening_an_old_database_together_do_not_crash(self):
        import sqlite3
        import subprocess
        import sys
        import time
        tmp = self.tmpdir()
        for i in range(10):
            path = os.path.join(tmp, f'{i}.sqlite')
            db = sqlite3.connect(path)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT)')
            db.commit()
            db.close()
            go = time.time() + 1
            code = ('import sys,time\nfrom superagent.store import Store\n'
                    'while time.time()<float(sys.argv[2]): pass\nStore(sys.argv[1])')
            ps = [subprocess.Popen([sys.executable, '-c', code, path, str(go)], stderr=subprocess.PIPE)
                  for _ in range(2)]
            for p in ps:
                _, err = p.communicate()
                self.assertEqual(p.returncode, 0, err.decode())

    def test_a_database_from_before_reasons_keeps_working(self):
        import sqlite3
        path = os.path.join(self.tmpdir(), 'old.sqlite')
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT)')
        db.execute('INSERT INTO triggers VALUES (?, ?, ?, ?)', ('old', 'github:o/r#1', 'accept', OLD_TRIGGER_JSON))
        db.commit()
        db.close()
        s = Store(path)
        s.add_trigger(trig(tid='new'), 'ignore', 'not addressed by a listed person')
        rows = {t.id: (d, r) for t, d, r in s.recent_triggers()}
        self.assertEqual(rows, {'old': ('accept', ''), 'new': ('ignore', 'not addressed by a listed person')})
        columns = [r[1] for r in sqlite3.connect(path).execute('PRAGMA table_info(triggers)')]
        self.assertEqual(columns, ['id', 'thread_key', 'decision', 'data'])  # never altered

    def test_a_database_upgraded_with_a_reason_column_keeps_working(self):
        import sqlite3
        path = os.path.join(self.tmpdir(), 'upgraded.sqlite')
        db = sqlite3.connect(path)  # what 48f2ba4's in-place upgrade left behind
        db.execute("CREATE TABLE triggers (id TEXT PRIMARY KEY, thread_key TEXT, decision TEXT, data TEXT, "
                   "reason TEXT DEFAULT '')")
        db.execute('INSERT INTO triggers VALUES (?, ?, ?, ?, ?)',
                   ('old', 'github:o/r#1', 'accept', OLD_TRIGGER_JSON, 'stored in the column'))
        db.commit()
        db.close()
        s = Store(path)
        self.assertTrue(s.add_trigger(trig(tid='new'), 'ignore', 'bot'))
        self.assertFalse(s.add_trigger(trig(tid='new'), 'accept', 'again'))
        rows = {t.id: (d, r) for t, d, r in s.recent_triggers()}
        self.assertEqual(rows, {'old': ('accept', 'stored in the column'), 'new': ('ignore', 'bot')})
        Store(path)  # opening it again copies nothing twice


class StoreHistoryTest(unittest.TestCase):
    def test_recent_triggers_are_newest_first_and_capped(self):
        s = Store(':memory:')
        for i in range(5):
            s.add_trigger(trig(tid=f't{i}'), 'context', f'reason {i}')
        self.assertEqual([(t.id, r) for t, _, r in s.recent_triggers(limit=2)], [('t4', 'reason 4'), ('t3', 'reason 3')])

    def test_inputs_lists_delivered_and_pending(self):
        s = Store(':memory:')
        s.add_input('tk-1', 'trigger', 'first')
        s.add_input('tk-1', 'trigger', 'second')
        s.mark_delivered([s.pending_inputs('tk-1')[0]['seq']])
        self.assertEqual([(i['text'], i['delivered']) for i in s.inputs('tk-1')], [('first', 1), ('second', 0)])


class TicketTitleTest(unittest.TestCase):
    def test_short_titles_are_kept_with_spaces_collapsed(self):
        from superagent.model import ticket_title
        self.assertEqual(ticket_title('  Fix   the\tlogin bug '), 'Fix the login bug')
        self.assertEqual(ticket_title(''), 'Request')

    def test_long_titles_end_at_a_whole_word_with_an_ellipsis(self):
        from superagent.model import ticket_title
        text = 'add a Troubleshooting section to the README of mikeliaowei/hot-repos about the GitHub token'
        title = ticket_title(text)
        self.assertEqual(title, 'add a Troubleshooting section to the README of mikeliaowei/hot-repos about the…')
        self.assertLessEqual(len(title), 80)

    def test_one_long_word_is_cut_hard(self):
        from superagent.model import ticket_title
        title = ticket_title('x' * 200)
        self.assertEqual((len(title), title[-1]), (80, '…'))
