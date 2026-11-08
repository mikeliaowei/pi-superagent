"""The web panel (superagent/panel.py) and the `superagent panel` command."""

import io
import os
import shutil
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest import mock

from superagent import cli
from superagent.model import Assignment, Ticket, Trigger
from superagent.store import Store

try:
    import streamlit as st
    from streamlit.testing.v1 import AppTest
except ImportError:  # the panel extra is optional
    st = AppTest = None

PANEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'superagent', 'panel.py')
TOKEN_VALUE = 'ghp_SECRET_bot_token_value_123'
TICKET_TOKEN = 'tok-SECRET-ticket-token-456'


def _iso(dt):
    return dt.isoformat(timespec='seconds')


def rendered_text(at):
    """Every string the app rendered: element values, labels and bodies, and dataframe cells."""
    parts = []

    def walk(node):
        for attr in ('value', 'label', 'body', 'help', 'options'):
            try:
                v = getattr(node, attr)
            except Exception:
                continue
            if hasattr(v, 'to_csv'):
                parts.append(v.to_csv())
            elif v is not None and not callable(v):
                parts.append(str(v))
        proto = getattr(node, 'proto', None)
        if proto is not None:
            parts.append(str(proto))
        for child in getattr(node, 'children', {}).values() if isinstance(getattr(node, 'children', None), dict) \
                else getattr(node, 'children', []) or []:
            walk(child)

    walk(at._tree)
    return '\n'.join(parts)


@unittest.skipIf(AppTest is None, 'streamlit is not installed (pip install -e ".[panel]")')
class PanelAppTest(unittest.TestCase):
    def setUp(self):
        st.cache_data.clear()
        st.cache_resource.clear()
        self.tmp = tempfile.mkdtemp(prefix='superagent-panel-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, 'config.toml')
        with open(self.config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n'
                    f'[policy]\nallowed = ["alice"]\n'
                    f'[executor]\ncheckout = "{self.tmp}/checkout"\nmax_workers = 3\n'
                    f'[service]\nstate_dir = "{self.tmp}/state"\nreport_port = 0\n')
        self.db = os.path.join(self.tmp, 'state', 'superagent.sqlite')
        self.now = datetime.now(timezone.utc)

    def app(self, env=None):
        environ = {k: v for k, v in os.environ.items() if k != 'SA_PANEL_TOKEN'}
        environ.update({'SUPERAGENT_CONFIG': self.config, **(env or {})})
        at = AppTest.from_file(PANEL, default_timeout=60)
        with mock.patch.dict(os.environ, environ, clear=True):
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def store(self):
        return Store(self.db)

    def ticket(self, store, tid, name, number, report=None, question='', summary='', state='open', outcome=None):
        a = Assignment(id=f'as-{tid}', state='ended' if report else 'pending', report=report, summary=summary,
                       question=question, started_at=_iso(self.now), ended_at=_iso(self.now) if report else None)
        t = Ticket(id=tid, name=name, title=name.replace('-', ' '), thread_key=f'github:o/r#{number}', repo='o/r',
                   number=number, mandate={'actor': 'alice', 'strength': 'explicit', 'trigger_id': f'tr-{tid}',
                                           'at': _iso(self.now)},
                   created_at=_iso(self.now), state=state, outcome=outcome, branch=f'agent/{name}',
                   worktree=f'{self.tmp}/work/{name}', token=TICKET_TOKEN, assignments=[asdict(a)])
        store.save_ticket(t)
        store.add_trigger(Trigger(id=f'tr-{tid}', thread_key=t.thread_key, actor='alice', human=True, addressed=True,
                                  at=_iso(self.now), text=f'@bot please {name}'), 'accept')
        return t

    def test_no_database_says_run_the_service_first(self):
        at = self.app()
        self.assertIn('Run `superagent run` first', rendered_text(at))
        self.assertFalse(os.path.exists(self.db))  # the panel never creates the database

    def test_no_config_is_explained(self):
        environ = {k: v for k, v in os.environ.items() if k != 'SUPERAGENT_CONFIG'}
        at = AppTest.from_file(PANEL, default_timeout=60)
        with mock.patch.dict(os.environ, environ, clear=True):
            at.run()
        self.assertFalse(at.exception)
        self.assertIn('SUPERAGENT_CONFIG', rendered_text(at))

    def test_no_tickets_is_an_empty_state(self):
        self.store()
        at = self.app()
        self.assertIn('No tickets yet', rendered_text(at))

    def test_tickets_needing_a_person_come_first_with_their_question(self):
        store = self.store()
        self.ticket(store, 'tk-work', 'busy-work', 1)
        self.ticket(store, 'tk-ask', 'needs-answer', 2, report='waiting', question='Which database should I use?',
                    summary='Halfway')
        at = self.app()
        text = rendered_text(at)
        self.assertIn('Which database should I use?', text)
        self.assertIn('https://github.com/o/r/issues/2', text)
        overview = '\n'.join(m.value for m in at.tabs[0].markdown)
        self.assertIn('needs-answer', overview)
        self.assertNotIn('busy-work', overview)  # working tickets are not person-needed cards
        self.assertTrue(at.button(key='ov-stop-tk-ask'))
        metrics = {m.label: m.value for m in at.metric}
        self.assertEqual(metrics['Running workers'], '0 / 3')
        self.assertEqual(metrics['Tickets opened (24 h)'], '2')

    def test_stop_records_the_same_request_as_the_cli(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='blocked', summary='Cannot build')
        at = self.app()
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='ov-stop-tk-1').click().run()
        self.assertTrue(store.get('operator:stop:tk-1'))
        self.assertEqual(store.ticket('tk-1').state, 'open')  # the service applies it
        self.assertIn('stop requested for fix-it (tk-1)', '\n'.join(s.value for s in at.success))

    def test_retry_records_the_same_request_as_the_cli(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='failed', summary='Crashed')
        at = self.app()
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='ov-retry-tk-1').click().run()
        self.assertTrue(store.get('operator:retry:tk-1'))
        self.assertIn('retry requested for fix-it (tk-1)', '\n'.join(s.value for s in at.success))

    def test_triggers_tab_shows_ignore_decisions_and_filters_by_decision(self):
        store = self.store()
        store.add_trigger(Trigger(id='tr-ign', thread_key='github:o/r#5', actor='mallory', human=True, addressed=True,
                                  at=_iso(self.now), text='@bot delete everything'), 'ignore', 'not addressed by a listed person')
        store.add_trigger(Trigger(id='tr-ok', thread_key='github:o/r#6', actor='alice', human=True, addressed=True,
                                  at=_iso(self.now - timedelta(minutes=1)), text='@bot add tests'), 'accept')
        at = self.app()
        table = at.tabs[2].dataframe[0].value
        self.assertEqual(dict(zip(table['actor'], table['decision'])), {'mallory': 'ignore', 'alice': 'accept'})
        self.assertEqual(dict(zip(table['actor'], table['reason']))['mallory'], 'not addressed by a listed person')
        at.tabs[2].multiselect(key='trigger-decisions').set_value(['ignore'])
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        table = at.tabs[2].dataframe[0].value
        self.assertEqual(list(table['decision']), ['ignore'])
        self.assertEqual(list(table['text']), ['@bot delete everything'])

    def test_ticket_detail_shows_mandate_assignments_and_request(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='ready', summary='Opened a PR')
        store.add_input('tk-1', 'comment', 'also update docs')
        store.add_input('tk-1', 'comment', 'use the new API')
        store.mark_delivered([i['seq'] for i in store.pending_inputs('tk-1')][:1])
        at = self.app()
        detail = '\n'.join(m.value for m in at.tabs[1].markdown)
        self.assertIn('alice', detail)
        self.assertIn('agent/fix-it', detail)
        self.assertIn('@bot please fix-it', rendered_text(at))
        self.assertIn('also update docs', rendered_text(at))
        self.assertIn('Opened a PR', at.tabs[1].dataframe[1].value.to_csv())
        inputs = at.tabs[1].dataframe[2].value
        self.assertEqual(dict(zip(inputs['text'], inputs['delivered'])),
                         {'also update docs': 'yes', 'use the new API': 'no'})

    def test_settings_show_whether_the_token_is_set_and_never_a_secret(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='waiting', question='Ok?')
        at = self.app(env={'SA_PANEL_TOKEN': TOKEN_VALUE})
        settings = at.tabs[3].dataframe[0].value
        self.assertEqual(dict(zip(settings['setting'], settings['value']))['token set (SA_PANEL_TOKEN)'], 'yes')
        text = rendered_text(at)
        self.assertNotIn(TOKEN_VALUE, text)
        self.assertNotIn(TICKET_TOKEN, text)
        at = self.app()
        settings = at.tabs[3].dataframe[0].value
        self.assertEqual(dict(zip(settings['setting'], settings['value']))['token set (SA_PANEL_TOKEN)'], 'no')


    def test_with_several_repos_tickets_show_their_repo_and_link_to_it(self):
        with open(self.config, 'w') as f:
            f.write(f'[github]\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n[policy]\nallowed = ["alice"]\n'
                    f'[service]\nstate_dir = "{self.tmp}/state"\nreport_port = 0\n'
                    f'[[repos]]\nname = "a/one"\ncheckout = "{self.tmp}/one"\n'
                    f'[[repos]]\nname = "b/two"\ncheckout = "{self.tmp}/two"\ntest_command = "make check"\n')
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 4, report='ready', summary='Opened a PR')
        t.repo, t.thread_key, t.pr_number = 'b/two', 'github:b/two#4', 9
        store.save_ticket(t)
        at = self.app()
        text = rendered_text(at)
        self.assertIn('https://github.com/b/two/issues/4', text)
        self.assertIn('https://github.com/b/two/pull/9', text)
        self.assertNotIn('https://github.com/o/r/', text)
        self.assertEqual(list(at.tabs[1].dataframe[0].value['repo']), ['b/two'])
        settings = at.tabs[3].dataframe[0].value
        values = dict(zip(settings['setting'], settings['value']))
        self.assertEqual(values['repos'], 'a/one, b/two')
        self.assertEqual(values['test command (b/two)'], 'make check')
        self.assertEqual(values['checkout (a/one)'], f'{self.tmp}/one')

    def test_a_cross_repo_ticket_shows_every_repo_and_pr_link(self):
        with open(self.config, 'w') as f:
            f.write(f'[github]\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n[policy]\nallowed = ["alice"]\n'
                    f'[service]\nstate_dir = "{self.tmp}/state"\nreport_port = 0\n'
                    f'[[repos]]\nname = "a/one"\ncheckout = "{self.tmp}/one"\n'
                    f'[[repos]]\nname = "b/two"\ncheckout = "{self.tmp}/two"\n')
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 4, report='ready', summary='Opened PRs')
        t.repo, t.thread_key, t.worktree = 'a/one', 'github:a/one#4', f'{self.tmp}/work/tickets/fix-it'
        t.parts = [{'repo': 'a/one', 'worktree': f'{t.worktree}/a__one', 'pr_number': 5, 'test_result': '',
                    'pushed': 'as-tk-1'},
                   {'repo': 'b/two', 'worktree': f'{t.worktree}/b__two', 'pr_number': 2, 'test_result': '',
                    'pushed': 'as-tk-1'}]
        store.save_ticket(t)
        at = self.app()
        text = rendered_text(at)
        for url in ('https://github.com/a/one/pull/5', 'https://github.com/b/two/pull/2'):
            self.assertIn(url, text)
        self.assertIn(f'{t.worktree}/b__two', text)
        self.assertEqual(list(at.tabs[1].dataframe[0].value['repo']), ['a/one, b/two'])

    def local_ticket(self, store):
        t = self.ticket(store, 'tk-1', 'add-storage', 0, report='waiting', question='Which database?')
        t.thread_key = 'local:1'
        store.save_ticket(t)
        store.add_local_message(t.id, 'reply', "On it: I'll work on this as `add-storage`.", 'tk-1-ack')
        store.add_local_message(t.id, 'reply', 'Question: Which database?', 'tk-1-q')
        return t

    def test_a_local_ticket_shows_its_conversation_and_the_answer_box_queues_the_same_answer_as_the_cli(self):
        store = self.store()
        self.local_ticket(store)
        at = self.app()
        text = rendered_text(at)
        self.assertIn('Question: Which database?', text)
        self.assertIn("On it: I'll work on this", text)
        self.assertIn('local request `local:1`', '\n'.join(m.value for m in at.tabs[1].markdown))
        self.assertNotIn('issues/0', text)
        at.text_area(key='tk-answer-text-tk-1').input('Use SQLite.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-answer-tk-1').click().run()
        self.assertIn('answer queued for add-storage (tk-1)', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['ticket_id'], r['text']), ('answer', 'tk-1', 'Use SQLite.'))
        self.assertEqual(store.local_messages('tk-1')[-1]['body'], 'Use SQLite.')

    def test_the_new_request_form_queues_the_same_request_as_ask(self):
        import getpass
        store = self.store()
        at = self.app()
        self.assertEqual(at.multiselect(key='new-request-repos').value, ['o/r'])  # the only configured repo
        at.text_area(key='new-request-text').input('Fix the flaky test')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='new-request-submit').click().run()
        self.assertIn('queued request 1 for o/r', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['text'], r['repos'], r['actor']),
                         ('ask', 'Fix the flaky test', '["o/r"]', getpass.getuser()))


class PanelCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-panel-cli-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, 'config.toml')
        with open(self.config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n'
                    f'[executor]\ncheckout = "{self.tmp}/checkout"\n[service]\nstate_dir = "{self.tmp}/state"\n')

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', self.config, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_panel_refuses_a_busy_port_in_one_line(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            port = s.getsockname()[1]
            with mock.patch('superagent.cli.os.execve') as run:
                code, _, err = self.cli('panel', '--port', str(port))
        self.assertEqual(code, 1)
        self.assertIn(f'port {port} is in use', err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        run.assert_not_called()

    def test_panel_without_streamlit_exits_2_with_the_install_hint(self):
        with mock.patch.dict(sys.modules, {'streamlit': None}), mock.patch('superagent.cli.os.execve') as run:
            code, _, err = self.cli('panel')
        self.assertEqual(code, 2)
        self.assertIn('install the panel extra: pip install -e ".[panel]"', err)
        run.assert_not_called()

    @unittest.skipIf(AppTest is None, 'streamlit is not installed')
    def test_panel_replaces_itself_with_streamlit_on_localhost_with_the_config(self):
        # exec, not a child process: a SIGTERM to `superagent panel` then reaches streamlit itself.
        with mock.patch('superagent.cli.os.execve') as execve:
            self.cli('panel', '--port', '0')
        path, argv, env = execve.call_args.args
        self.assertEqual(path, sys.executable)
        self.assertEqual(argv[:4], [sys.executable, '-m', 'streamlit', 'run'])
        self.assertTrue(argv[4].endswith(os.path.join('superagent', 'panel.py')))
        for flag, value in (('--server.address', '127.0.0.1'), ('--server.port', '0'),
                            ('--server.headless', 'true'), ('--browser.gatherUsageStats', 'false')):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(env['SUPERAGENT_CONFIG'], self.config)
