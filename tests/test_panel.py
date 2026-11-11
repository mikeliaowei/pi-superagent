"""The web panel (superagent/panel.py) and the `superagent panel` command."""

import io
import json
import os
import shutil
import socket
import subprocess
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


SETTINGS_TABS = ['Repos', 'Worker and service', 'All settings', 'Service health', 'Incoming messages']


@unittest.skipIf(AppTest is None, 'streamlit is not installed (pip install -e ".[panel]")')
class PanelAppTest(unittest.TestCase):
    def tab(self, at, label):
        (tab,) = [t for t in at.tabs if t.label == label]
        return tab

    MULTI = ('[github]\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n[policy]\nallowed = ["alice"]\n'
             "[service]\nstate_dir = '{tmp}/state'\nreport_port = 0\n"
             "[[repos]]\nname = \"a/one\"\ncheckout = '{tmp}/one'\n"
             "[[repos]]\nname = \"b/two\"\ncheckout = '{tmp}/two'\ntest_command = \"make check\"\n")

    def multi_repo(self):
        with open(self.config, 'w') as f:
            f.write(self.MULTI.format(tmp=self.tmp))
    def setUp(self):
        st.cache_data.clear()
        st.cache_resource.clear()
        self.tmp = tempfile.mkdtemp(prefix='superagent-panel-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, 'config.toml')
        with open(self.config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n'
                    f'[policy]\nallowed = ["alice"]\n'
                    f'[executor]\ncheckout = \'{self.tmp}/checkout\'\nmax_workers = 3\n'
                    f'[service]\nstate_dir = \'{self.tmp}/state\'\nreport_port = 0\n')
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
        store = Store(self.db)
        self.addCleanup(store.close)  # an unclosed one warns into a later test's captured stderr
        return store

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

    def test_an_edited_store_module_gets_a_new_store_instead_of_the_cached_one(self):
        self.store()
        at = self.app()
        used = []

        class Reloaded(Store):  # what Streamlit's reload of an edited store.py makes
            def tickets(self, *args, **kwargs):
                used.append(self)
                return super().tickets(*args, **kwargs)

        with mock.patch('superagent.store.Store', Reloaded), \
                mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(used)  # not the Store cached from the old class (stale methods: queue_ask TypeError)

    def test_tickets_needing_a_person_come_first_with_their_question(self):
        store = self.store()
        self.ticket(store, 'tk-work', 'busy-work', 1)
        self.ticket(store, 'tk-ask', 'needs-answer', 2, report='waiting', question='Which database should I use?',
                    summary='Halfway')
        self.ticket(store, 'tk-done', 'old-work', 3, report='ready', state='ended', outcome='merged')
        self.ticket(store, 'tk-gone', 'given-up', 4, report='failed', state='ended', outcome='stopped')
        at = self.app()
        needs = self.tab(at, 'Overview').dataframe[0].value
        self.assertEqual(list(needs['ID']), ['needs-answer'])
        self.assertEqual(list(needs['title']), ['needs answer'])  # working and ended tickets do not need you
        self.assertEqual(list(needs['status']), ['Waiting for your answer'])
        self.assertEqual(list(needs['question or report']), ['Which database should I use?'])
        self.assertEqual(list(needs['issue']), ['https://github.com/o/r/issues/2'])
        metrics = {m.label: m.value for m in at.metric}
        self.assertEqual({k: metrics[k] for k in ('Tickets', 'Open', 'Need you', 'Succeeded', 'Not merged')},
                         {'Tickets': '4', 'Open': '2', 'Need you': '1', 'Succeeded': '1', 'Not merged': '1'})
        self.assertEqual(metrics['Running workers'], '0 / 3')
        self.assertEqual(metrics['Tickets opened (24 h)'], '4')
        self.assertEqual(len(self.tab(at, 'Overview').get('vega_lite_chart')), 4)  # status, repo, days, tokens

    def test_a_count_card_opens_its_tickets_on_the_tickets_tab(self):
        store = self.store()
        self.ticket(store, 'tk-work', 'busy-work', 1)
        self.ticket(store, 'tk-ask', 'needs-answer', 2, report='waiting', question='Which database?')
        self.ticket(store, 'tk-rev', 'needs-review', 3, report='ready', summary='Opened a PR')
        self.ticket(store, 'tk-done', 'old-work', 4, report='ready', state='ended', outcome='merged')
        at = self.app()
        self.assertTrue(at.button(key='ov-show-Need you').proto.help)
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='ov-show-Need you').click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(at.session_state['main-tabs'], 'Tickets')
        self.assertEqual(sorted(self.tab(at, 'Tickets').dataframe[0].value['ID']), ['needs-answer', 'needs-review'])
        self.open_ticket(at, 'needs-answer')
        self.assertEqual(at.text_area(key='tk-answer-text-tk-ask').label, 'Answer')  # straight to the reply
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='ov-show-Succeeded').click().run()
        self.assertEqual(list(self.tab(at, 'Tickets').dataframe[0].value['ID']), ['old-work'])

    def test_overview_counts_tickets_created_in_the_time_range_and_repos(self):
        self.multi_repo()
        store = self.store()
        old = self.ticket(store, 'tk-old', 'old-work', 1, report='ready', state='ended', outcome='merged')
        old.created_at = _iso(self.now - timedelta(days=40))
        store.save_ticket(old)
        two = self.ticket(store, 'tk-new', 'new-work', 2)
        two.repo, two.thread_key = 'b/two', 'github:b/two#2'
        store.save_ticket(two)
        at = self.app()
        count = lambda: {m.label: m.value for m in at.metric}['Tickets']  # noqa: E731
        self.assertEqual(at.selectbox(key='ov-range').value, 'Last 30 days')
        self.assertEqual(count(), '1')
        at.selectbox(key='ov-range').set_value('All time')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertEqual(count(), '2')
        at.multiselect(key='ov-repos').set_value(['a/one'])
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(count(), '0')
        at.selectbox(key='ticket-range').set_value('Last 7 days')
        at.multiselect(key='ticket-repos').set_value([])
        at.radio(key='ticket-scope').set_value('all')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertEqual(list(self.tab(at, 'Tickets').dataframe[0].value['ID']), ['new-work'])

    def select_row(self, at, rows):
        """Select rows of the Tickets table, as clicking them does."""
        table = self.tab(at, 'Tickets').dataframe[0]
        at.session_state[table.key] = {'selection': {'rows': rows, 'columns': [], 'cells': []}}
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def open_ticket(self, at, name):
        """Click the ticket's row in the Tickets table."""
        return self.select_row(at, [list(self.tab(at, 'Tickets').dataframe[0].value['ID']).index(name)])

    def test_a_clicked_ticket_shows_its_details_until_it_is_clicked_again(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)
        self.ticket(store, 'tk-2', 'other-work', 2)
        at = self.app()
        self.assertIn('Click a ticket to see its details', rendered_text(at))
        self.open_ticket(at, 'fix-it')
        self.assertIn('### fix it', '\n'.join(m.value for m in self.tab(at, 'Tickets').markdown))
        self.assertEqual([t.label for t in self.tab(at, 'Tickets').tabs],
                         ['Worker activity', 'Request', 'Runs', 'Messages to worker'])
        key = self.tab(at, 'Tickets').dataframe[0].key
        t = store.ticket('tk-1')
        t.assignments[-1]['summary'] = 'Halfway'
        store.save_ticket(t)
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        # A refresh with changed rows keeps the table's key, so the browser keeps the selection (the test harness
        # does not carry it over: clicking again stands in for that).
        self.assertEqual(self.tab(at, 'Tickets').dataframe[0].key, key)
        self.open_ticket(at, 'fix-it')
        self.assertIn('### fix it', '\n'.join(m.value for m in self.tab(at, 'Tickets').markdown))
        self.select_row(at, [])  # unselected: the details go
        self.assertNotIn('### fix it', rendered_text(at))
        self.ticket(store, 'tk-3', 'newer-work', 3)
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        # A new row shifts the rows: a new key starts the selection over rather than show another ticket.
        self.assertNotEqual(self.tab(at, 'Tickets').dataframe[0].key, key)

    def test_stop_records_the_same_request_as_the_cli(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='blocked', summary='Cannot build')
        at = self.open_ticket(self.app(), 'fix-it')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-stop-tk-1').click().run()
        self.assertTrue(store.get('operator:stop:tk-1'))
        self.assertEqual(store.ticket('tk-1').state, 'open')  # the service applies it
        self.assertIn('stop requested for fix-it (tk-1)', '\n'.join(s.value for s in at.success))

    def test_retry_records_the_same_request_as_the_cli(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='failed', summary='Crashed')
        at = self.open_ticket(self.app(), 'fix-it')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-retry-tk-1').click().run()
        self.assertTrue(store.get('operator:retry:tk-1'))
        self.assertIn('retry requested for fix-it (tk-1)', '\n'.join(s.value for s in at.success))

    def incoming(self, at):
        (table,) = [d.value for d in self.tab(at, 'Settings').dataframe if 'what it did' in d.value.columns]
        return table

    def test_settings_list_incoming_messages_with_what_was_done_and_why(self):
        store = self.store()
        store.add_trigger(Trigger(id='tr-ign', thread_key='github:o/r#5', actor='mallory', human=True, addressed=True,
                                  at=_iso(self.now), text='@bot delete everything'), 'ignore', 'not addressed by a listed person')
        store.add_trigger(Trigger(id='tr-ok', thread_key='github:o/r#6', actor='alice', human=True, addressed=True,
                                  at=_iso(self.now - timedelta(minutes=1)), text='@bot add tests'), 'accept')
        at = self.app()
        self.assertEqual([t.label for t in at.tabs], ['Overview', 'Tickets', 'New request', 'Settings', *SETTINGS_TABS])
        table = self.incoming(at)
        self.assertEqual(dict(zip(table['from'], table['what it did'], strict=True)),
                         {'mallory': 'Ignored', 'alice': 'Started work'})
        self.assertEqual(dict(zip(table['from'], table['why'], strict=True))['mallory'],
                         'not addressed by a listed person')
        at.multiselect(key='trigger-decisions').set_value(['ignore'])
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        table = self.incoming(at)
        self.assertEqual(list(table['what it did']), ['Ignored'])
        self.assertEqual(list(table['text']), ['@bot delete everything'])
        self.assertIn('Last poll: never', rendered_text(at))

    def test_ticket_detail_shows_mandate_assignments_and_request(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='ready', summary='Opened a PR')
        store.add_input('tk-1', 'comment', 'also update docs')
        store.add_input('tk-1', 'comment', 'use the new API')
        store.mark_delivered([i['seq'] for i in store.pending_inputs('tk-1')][:1])
        at = self.open_ticket(self.app(), 'fix-it')
        tab = self.tab(at, 'Tickets')
        self.assertIn('ID `fix-it` · asked by alice', '\n'.join(c.value for c in tab.caption))
        self.assertIn('**Ready for review** · o/r · branch `agent/fix-it`', '\n'.join(m.value for m in tab.markdown))
        self.assertIn('@bot please fix-it', rendered_text(at))
        self.assertIn('also update docs', rendered_text(at))
        self.assertIn('Opened a PR', tab.dataframe[1].value.to_csv())
        inputs = tab.dataframe[2].value
        self.assertEqual(dict(zip(inputs['text'], inputs['delivered'])),
                         {'also update docs': 'yes', 'use the new API': 'no'})

    def session(self, tid, entries):
        folder = os.path.join(self.tmp, 'state', 'sessions')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f'2026-11-09T03-24-39-408Z_{tid}.jsonl'), 'w', encoding='utf-8') as f:
            f.writelines(json.dumps({'type': 'message', 'timestamp': _iso(self.now), 'message': m}) + '\n'
                         for m in entries)

    def test_ticket_detail_shows_the_workers_activity_with_secrets_masked(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)
        self.session('tk-1', [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'private chain of thought'},
                {'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'just test'}}]},
            {'role': 'toolResult', 'toolName': 'bash', 'isError': False,
             'content': [{'type': 'text', 'text': f'4 passed {TICKET_TOKEN}'}]}])
        at = self.open_ticket(self.app(), 'fix-it')
        text = rendered_text(at)
        self.assertIn('Worker activity', text)
        self.assertIn('just test', text)
        self.assertIn('4 passed ***', text)
        self.assertNotIn(TICKET_TOKEN, text)
        self.assertNotIn('private chain of thought', text)  # reasoning is hidden until asked for
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.toggle(key='worker-reasoning-tk-1').set_value(True).run()
        self.assertIn('private chain of thought', rendered_text(at))

    def test_ticket_detail_shows_only_the_latest_worker_steps_until_asked_for_all(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)
        self.session('tk-1', [{'role': 'assistant', 'content': [
            {'type': 'toolCall', 'id': f'c{i}', 'name': 'bash', 'arguments': {'command': f'step-{i:03d}'}}]}
            for i in range(60)])
        at = self.open_ticket(self.app(), 'fix-it')
        text = rendered_text(at)
        self.assertIn('step-059', text)
        self.assertNotIn('step-000', text)
        self.assertIn('Showing the last 50 of 60 steps', text)
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.toggle(key='worker-all-tk-1').set_value(True).run()
        self.assertIn('step-000', rendered_text(at))

    def test_ticket_detail_without_a_worker_session_says_so(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)
        self.assertIn('No worker activity yet', rendered_text(self.open_ticket(self.app(), 'fix-it')))

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
        self.multi_repo()
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 4, report='ready', summary='Opened a PR')
        t.repo, t.thread_key, t.pr_number = 'b/two', 'github:b/two#4', 9
        store.save_ticket(t)
        at = self.app()
        text = rendered_text(at)
        self.assertIn('https://github.com/b/two/issues/4', text)
        self.assertIn('https://github.com/b/two/pull/9', text)
        self.assertNotIn('https://github.com/o/r/', text)
        self.assertEqual(list(self.tab(at, 'Tickets').dataframe[0].value['repo']), ['b/two'])
        settings = self.tab(at, 'Settings').dataframe[0].value
        values = dict(zip(settings['setting'], settings['value']))
        self.assertEqual(values['repos'], 'a/one, b/two')
        self.assertEqual(at.text_input(key='set-b__two-test').value, 'make check')
        self.assertEqual(at.text_input(key='set-a__one-checkout').value, f'{self.tmp}/one')
        self.assertTrue(at.text_input(key='set-b__two-name').disabled)  # its ticket refers to it by name
        self.assertFalse(at.text_input(key='set-a__one-name').disabled)

    def test_a_cross_repo_ticket_shows_every_repo_and_pr_link(self):
        with open(self.config, 'w') as f:
            f.write(f'[github]\nagent_login = "bot"\ntoken_env = "SA_PANEL_TOKEN"\n[policy]\nallowed = ["alice"]\n'
                    f'[service]\nstate_dir = \'{self.tmp}/state\'\nreport_port = 0\n'
                    f'[[repos]]\nname = "a/one"\ncheckout = \'{self.tmp}/one\'\n'
                    f'[[repos]]\nname = "b/two"\ncheckout = \'{self.tmp}/two\'\n')
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 4, report='ready', summary='Opened PRs')
        t.repo, t.thread_key, t.worktree = 'a/one', 'github:a/one#4', f'{self.tmp}/work/tickets/fix-it'
        t.parts = [{'repo': 'a/one', 'worktree': f'{t.worktree}/a__one', 'pr_number': 5, 'test_result': '',
                    'pushed': 'as-tk-1'},
                   {'repo': 'b/two', 'worktree': f'{t.worktree}/b__two', 'pr_number': 2, 'test_result': '',
                    'pushed': 'as-tk-1'}]
        store.save_ticket(t)
        at = self.open_ticket(self.app(), 'fix-it')
        text = rendered_text(at)
        for url in ('https://github.com/a/one/pull/5', 'https://github.com/b/two/pull/2'):
            self.assertIn(url, text)
        self.assertIn(f'{t.worktree}/b__two', text)
        self.assertEqual(list(self.tab(at, 'Tickets').dataframe[0].value['repo']), ['a/one, b/two'])
        for repo in ('a/one', 'b/two'):  # a cross-repo ticket matches each of its repos
            at.multiselect(key='ticket-repos').set_value([repo])
            with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
                at.run()
            self.assertEqual(list(self.tab(at, 'Tickets').dataframe[0].value['ID']), ['fix-it'])

    def test_tickets_can_be_filtered_by_repo_and_status(self):
        self.multi_repo()
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 4, report='waiting', question='Which table?')
        t.repo, t.thread_key = 'b/two', 'github:b/two#4'
        store.save_ticket(t)
        self.ticket(store, 'tk-2', 'other-work', 5)
        at = self.app()
        names = lambda: list(self.tab(at, 'Tickets').dataframe[0].value['ID'])  # noqa: E731
        self.assertEqual(names(), ['other-work', 'fix-it'])
        at.multiselect(key='ticket-repos').set_value(['b/two'])
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertEqual(names(), ['fix-it'])
        at.multiselect(key='ticket-repos').set_value([])
        at.multiselect(key='ticket-stages').set_value(['Queued'])
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertEqual(names(), ['other-work'])

    def test_tokens_and_cost_show_per_ticket_and_for_the_last_24_hours(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)
        self.ticket(store, 'tk-2', 'no-worker-yet', 2)
        usage = {'input': 100, 'output': 2000, 'cacheRead': 30000, 'cacheWrite': 4000, 'cost': {'total': 1.25}}
        self.session('tk-1', [{'role': 'assistant', 'content': [], 'usage': usage},
                              {'role': 'assistant', 'content': [], 'usage': {**usage, 'cost': {'total': 0.5}}}])
        at = self.app()
        cost = [m for m in at.metric if m.label == 'Worker cost (24 h)'][0]
        self.assertEqual(cost.value, '$1.75')
        table = self.tab(at, 'Tickets').dataframe[0].value.set_index('ID')
        self.assertEqual((table.loc['fix-it', 'tokens'], table.loc['fix-it', 'cost']), (72200, 1.75))
        self.assertEqual(table.loc['no-worker-yet', 'tokens'], 0)
        self.open_ticket(at, 'fix-it')
        self.assertIn('Usage: 72,200 tokens (input 200, output 4,000, cache read 60,000, cache write 8,000) · '
                      'cost $1.75', '\n'.join(c.value for c in at.caption))

    def save_settings(self, at):
        (save,) = [b for b in at.button if b.label == 'Save settings']
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            save.click().run()

    def save_repo(self, at, slug):
        (save,) = [b for b in at.button if b.label == 'Save' and b.form_id == f'repo-form-{slug}']
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            save.click().run()

    def test_settings_can_be_edited_and_are_saved_into_the_config_file(self):
        self.multi_repo()
        self.store()
        at = self.app()
        at.text_input(key='set-model').input('claude-opus-5-5')
        at.selectbox(key='set-thinking').set_value('high')
        at.number_input(key='set-max-workers').set_value(4)
        at.radio(key='set-agent').set_value('copilot')
        self.save_settings(at)
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn('Saved agent, model, thinking, max_workers. Restart `superagent run`',
                      '\n'.join(s.value for s in at.success))
        at.text_input(key='set-b__two-test').input('make test')
        self.save_repo(at, 'b__two')
        self.assertIn('Saved test_command of b/two. Restart `superagent run`', '\n'.join(s.value for s in at.success))
        c = cli.load_config(self.config)
        self.assertEqual((c.agent, c.model, c.thinking, c.max_workers, c.repo_config('b/two').test_command),
                         ('copilot', 'claude-opus-5-5', 'high', 4, 'make test'))
        settings = self.tab(at, 'Settings').dataframe[0].value  # the panel reloaded the config
        self.assertEqual(dict(zip(settings['setting'], settings['value'], strict=True))['model'], 'claude-opus-5-5')
        with open(self.config) as f:
            self.assertIn('[[repos]]\nname = "b/two"', f.read())

    def test_a_repo_can_be_renamed_moved_and_removed(self):
        self.multi_repo()
        self.store()
        moved = os.path.join(self.tmp, 'moved')
        os.makedirs(moved)
        at = self.app()
        at.text_input(key='set-b__two-name').input('not a repo')
        self.save_repo(at, 'b__two')
        self.assertIn('b/two not saved: give the repo as owner/name', '\n'.join(e.value for e in at.error))
        at.text_input(key='set-b__two-name').input('c/three')
        at.text_input(key='set-b__two-checkout').input(moved)
        self.save_repo(at, 'b__two')
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn('Saved name, checkout of c/three.', '\n'.join(s.value for s in at.success))
        c = cli.load_config(self.config)
        self.assertEqual([(r.name, r.checkout, r.test_command) for r in c.repos],
                         [('a/one', f'{self.tmp}/one', ''), ('c/three', moved, 'make check')])
        (remove,) = [b for b in at.button if b.label == 'Yes, remove a/one']
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            remove.click().run()
        self.assertIn('Removed a/one.', '\n'.join(s.value for s in at.success))
        self.assertEqual(cli.load_config(self.config).repo_names, ['c/three'])
        self.assertFalse([b for b in at.button if b.label.startswith('Yes, remove')])  # the last repo stays

    def test_a_copilot_worker_offers_copilots_models_with_their_cost(self):
        with open(self.config) as f:
            cfg_text = f.read().replace('[executor]\n', '[executor]\nagent = "copilot"\nmodel = "gpt-6"\n')
        with open(self.config, 'w') as f:
            f.write(cfg_text)
        self.store()
        offered = [('auto', 'Let Copilot pick'), ('gpt-6', '1x premium requests'), ('opus', '15x premium requests')]
        with mock.patch('superagent.cli.copilot_models', return_value=offered) as ask:
            at = self.app()
            self.assertEqual(at.selectbox(key='new-request-model').options,
                             ['auto · Let Copilot pick', 'gpt-6 · 1x premium requests', 'opus · 15x premium requests'])
            self.assertEqual(at.selectbox(key='set-model').value, 'gpt-6')
            at.selectbox(key='set-model').set_value('opus')
            self.save_settings(at)
        self.assertEqual(ask.call_count, 1)  # asked once, then cached
        self.assertEqual(cli.load_config(self.config).model, 'opus')

    def test_settings_saving_nothing_or_a_bad_value_writes_nothing(self):
        self.store()
        with open(self.config) as f:
            before = f.read()
        at = self.app()
        self.save_settings(at)
        self.assertIn('Nothing changed.', '\n'.join(i.value for i in at.info))
        at.text_input(key='set-o__r-checkout').input('')
        self.save_repo(at, 'o__r')
        self.assertIn('o/r not saved: checkout folder (empty) does not exist', '\n'.join(e.value for e in at.error))
        with open(self.config) as f:
            self.assertEqual(f.read(), before)

    def add_repo(self, at, name, checkout):
        at.text_input(key='add-repo-name').input(name)
        at.text_input(key='add-repo-checkout').input(checkout)
        (add,) = [b for b in at.button if b.label == 'Add repo']
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            add.click().run()

    def test_adding_a_repo_keeps_the_same_tabs_and_lets_tickets_filter_by_it(self):
        self.store()
        at = self.app()
        self.assertEqual([t.label for t in at.tabs], ['Overview', 'Tickets', 'New request', 'Settings', *SETTINGS_TABS])
        self.assertIn('moves o/r into a [[repos]] table', rendered_text(at))
        with open(self.config) as f:
            before = f.read()
        self.add_repo(at, 'b/two', os.path.join(self.tmp, 'missing'))
        self.assertIn('Not added: checkout folder', '\n'.join(e.value for e in at.error))
        self.add_repo(at, 'not a repo', self.tmp)
        self.assertIn('Not added: give the repo as owner/name', '\n'.join(e.value for e in at.error))
        with open(self.config) as f:
            self.assertEqual(f.read(), before)
        two = os.path.join(self.tmp, 'two')
        os.makedirs(two)
        self.add_repo(at, 'b/two', two)
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn('Added b/two. Restart `superagent run`', '\n'.join(s.value for s in at.success))
        self.assertEqual([t.label for t in at.tabs], ['Overview', 'Tickets', 'New request', 'Settings', *SETTINGS_TABS])
        self.assertEqual(at.multiselect(key='new-request-repos').options, ['o/r', 'b/two'])
        c = cli.load_config(self.config)
        self.assertEqual([(r.name, r.checkout) for r in c.repos], [('o/r', f'{self.tmp}/checkout'), ('b/two', two)])
        self.assertEqual(c.max_workers, 3)  # the rest of the config is kept

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
        at = self.open_ticket(self.app(), 'add-storage')
        text = rendered_text(at)
        self.assertIn('Question: Which database?', text)
        self.assertIn("On it: I'll work on this", text)
        self.assertIn('local request `local:1`', '\n'.join(m.value for m in self.tab(at, 'Tickets').markdown))
        self.assertNotIn('issues/0', text)
        at.text_area(key='tk-answer-text-tk-1').input('Use SQLite.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-answer-tk-1').click().run()
        self.assertIn('answer queued for add-storage (tk-1)', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['ticket_id'], r['text']), ('answer', 'tk-1', 'Use SQLite.'))
        self.assertEqual(store.local_messages('tk-1')[-1]['body'], 'Use SQLite.')

    def test_a_question_asks_for_an_answer(self):
        store = self.store()
        self.local_ticket(store)
        at = self.open_ticket(self.app(), 'add-storage')
        self.assertEqual(at.text_area(key='tk-answer-text-tk-1').label, 'Answer')
        self.assertEqual(at.button(key='tk-answer-tk-1').label, 'Answer')

    def test_a_blocked_ticket_without_a_question_takes_an_instruction_for_the_worker(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='blocked', summary='Cannot build')
        at = self.open_ticket(self.app(), 'fix-it')
        self.assertEqual(at.text_area(key='tk-answer-text-tk-1').label, 'Instruction for the worker')
        at.text_area(key='tk-answer-text-tk-1').input('Target feature/memory-history.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-answer-tk-1').click().run()
        self.assertIn('queued for fix-it (tk-1)', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['ticket_id'], r['text']), ('answer', 'tk-1', 'Target feature/memory-history.'))

    def test_ticket_detail_takes_an_instruction_for_any_open_github_ticket(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1)  # working, no question
        at = self.open_ticket(self.app(), 'fix-it')
        tab = self.tab(at, 'Tickets')
        self.assertEqual(tab.text_area(key='tk-answer-text-tk-1').label, 'Instruction for the worker')
        self.assertEqual(tab.button(key='tk-answer-tk-1').label, 'Send instruction')
        tab.text_area(key='tk-answer-text-tk-1').input('Also update the README.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            tab.button(key='tk-answer-tk-1').click().run()
        (r,) = store.local_requests()
        self.assertEqual((r['ticket_id'], r['text']), ('tk-1', 'Also update the README.'))

    def test_an_ended_ticket_takes_an_instruction_that_reopens_it(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='ready', state='ended', outcome='merged')
        at = self.app()
        at.radio(key='ticket-scope').set_value('all')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.open_ticket(at, 'fix-it')
        self.assertEqual(at.text_area(key='tk-answer-text-tk-1').label, 'Instruction to reopen this ticket')
        self.assertEqual(at.button(key='tk-answer-tk-1').label, 'Reopen with instruction')
        at.text_area(key='tk-answer-text-tk-1').input('The PR needs a rebase.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='tk-answer-tk-1').click().run()
        self.assertIn('reopens', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['ticket_id'], r['text']), ('tk-1', 'The PR needs a rebase.'))

    def test_the_tickets_table_shows_open_tickets_with_their_branch_and_status_in_plain_words(self):
        store = self.store()
        t = self.ticket(store, 'tk-1', 'fix-it', 1)
        t.assignments[-1]['state'] = 'running'
        store.save_ticket(t)
        self.ticket(store, 'tk-2', 'needs-review', 2, report='ready', summary='Opened a PR')
        self.ticket(store, 'tk-3', 'old-work', 3, report='ready', state='ended', outcome='merged')
        at = self.app()
        table = self.tab(at, 'Tickets').dataframe[0].value
        self.assertEqual(list(table['ID']), ['needs-review', 'fix-it'])  # ended tickets wait for Show: all
        self.assertEqual(list(table['branch']), ['agent/needs-review', 'agent/fix-it'])
        self.assertEqual(list(table['status']), ['Ready for review', 'Working'])
        at.radio(key='ticket-scope').set_value('all')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertEqual(self.tab(at, 'Tickets').dataframe[0].value['status'].iloc[0], 'Merged')

    def test_overview_says_when_nothing_needs_you_and_tickets_when_none_is_open(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='ready', state='ended', outcome='merged')
        at = self.app()
        self.assertIn('Nothing needs you right now.', '\n'.join(i.value for i in self.tab(at, 'Overview').info))
        self.assertIn('No open tickets match', '\n'.join(i.value for i in self.tab(at, 'Tickets').info))

    def test_a_new_request_can_continue_an_open_ticket_on_its_branch(self):
        store = self.store()
        self.ticket(store, 'tk-1', 'fix-it', 1, report='ready', summary='Opened a PR')
        at = self.app()
        at.selectbox(key='new-request-continue').set_value('tk-1')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertIn('agent/fix-it', rendered_text(at))
        self.assertFalse([w for w in at.multiselect if w.key == 'new-request-repos'])
        at.text_area(key='new-request-text').input('Raise the PR against feature/memory-history.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='new-request-submit').click().run()
        self.assertIn('queued for fix-it (tk-1)', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['ticket_id'], r['text']),
                         ('answer', 'tk-1', 'Raise the PR against feature/memory-history.'))
        self.assertEqual(len(store.tickets()), 1)  # no second ticket, so no second branch

    def test_the_new_request_form_queues_the_same_request_as_ask(self):
        import getpass
        store = self.store()
        at = self.app()
        self.assertEqual(at.multiselect(key='new-request-repos').value, ['o/r'])  # the only configured repo
        self.assertEqual(at.text_area(key='new-request-text').label, 'Requirements')
        at.text_area(key='new-request-text').input('Fix the flaky test\nIt fails on CI.')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='new-request-submit').click().run()
        self.assertIn('Give the request a title', '\n'.join(e.value for e in at.error))
        self.assertEqual(store.local_requests(), [])
        at.text_input(key='new-request-title').input('Flaky CI test')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='new-request-submit').click().run()
        self.assertIn('queued request 1 for o/r', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual((r['kind'], r['text'], r['repos'], r['actor']),
                         ('ask', 'Fix the flaky test\nIt fails on CI.', '["o/r"]', getpass.getuser()))
        self.assertEqual(store.ask_branch(r['id']), '')  # a new branch by default
        self.assertEqual(store.ask_options(r['id']), {'title': 'Flaky CI test', 'new_branch': '', 'skills': [],
                                                      'model': '', 'thinking': ''})

    def checkout(self, *setup):
        co = os.path.join(self.tmp, 'checkout')
        for args in (['init', '-q', '-b', 'main', co],
                     ['-C', co, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '--allow-empty',
                      '-m', 'init'], *setup):
            subprocess.run(['git', *args], check=True, capture_output=True)
        return co

    def test_a_new_request_can_name_its_new_branch_and_pick_skills_for_its_worker(self):
        self.checkout()
        home, agent = os.path.join(self.tmp, 'home'), os.path.join(self.tmp, 'pi-agent')
        skill = os.path.join(agent, 'skills', 'grilling')
        os.makedirs(skill)
        with open(os.path.join(skill, 'SKILL.md'), 'w') as f:
            f.write('---\nname: grilling\n---\n')
        env = {'SUPERAGENT_CONFIG': self.config, 'PI_CODING_AGENT_DIR': agent, 'HOME': home, 'USERPROFILE': home}
        store = self.store()
        at = self.app(env=env)
        at.text_input(key='new-request-title').input('Memory history page')
        with mock.patch.dict(os.environ, env):
            at.run()
        self.assertEqual(at.text_input(key='new-request-new-branch').placeholder, 'agent/memory-history-page')
        box = at.multiselect(key='new-request-skills')
        self.assertEqual(box.options, ['grilling · Pi'])
        box.set_value([skill])
        at.text_input(key='new-request-new-branch').input('feature/memory-page')
        at.text_area(key='new-request-text').input('Show the memory history')
        with mock.patch.dict(os.environ, env):
            at.button(key='new-request-submit').click().run()
        self.assertIn('on new branch feature/memory-page', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual(store.ask_options(r['id']), {'title': 'Memory history page',
                                                      'new_branch': 'feature/memory-page', 'skills': [skill],
                                                      'model': '', 'thinking': ''})

    def test_a_new_request_can_work_on_an_existing_branch(self):
        self.checkout(['-C', os.path.join(self.tmp, 'checkout'), 'branch', 'feature/memory-history'],
                      ['-C', os.path.join(self.tmp, 'checkout'), 'update-ref', 'refs/remotes/origin/feature/on-github',
                       'HEAD'])
        store = self.store()
        at = self.app()
        self.assertFalse([w for w in at.selectbox if w.key == 'new-request-branch'])
        at.radio(key='new-request-branch-kind').set_value('Existing branch')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.run()
        self.assertFalse([w for w in at.text_input if w.key == 'new-request-new-branch'])
        box = at.selectbox(key='new-request-branch')
        self.assertEqual(box.options, ['feature/memory-history · local', 'feature/on-github · GitHub'])
        self.assertTrue([b for b in at.button if b.key == 'new-request-branch-refresh'])
        box.set_value('feature/memory-history')
        at.text_input(key='new-request-title').input('Unit test')
        at.text_area(key='new-request-text').input('Add a unit test')
        with mock.patch.dict(os.environ, {'SUPERAGENT_CONFIG': self.config}):
            at.button(key='new-request-submit').click().run()
        self.assertIn('on existing branch feature/memory-history', '\n'.join(s.value for s in at.success))
        (r,) = store.local_requests()
        self.assertEqual(store.ask_branch(r['id']), 'feature/memory-history')


class PanelCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-panel-cli-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, 'config.toml')
        with open(self.config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\n[policy]\nallowed = ["alice"]\n'
                    f'[executor]\ncheckout = \'{self.tmp}/checkout\'\n[service]\nstate_dir = \'{self.tmp}/state\'\n')

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
