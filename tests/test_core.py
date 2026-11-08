import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from superagent import config as cfgmod
from superagent.model import Assignment, Ticket, Trigger, short_name, stage
from superagent.policy import gate, mentions
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


class ModelTest(unittest.TestCase):
    def test_short_name_is_kebab_and_unique(self):
        self.assertEqual(short_name('Fix the login page crash on Safari!'), 'fix-the-login-page')
        self.assertEqual(short_name('Fix the login page', taken={'fix-the-login-page'}), 'fix-the-login-page-2')
        self.assertEqual(short_name('!!!'), 'task')

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

    def test_kv(self):
        self.assertEqual(self.s.get('cursor', 'none'), 'none')
        self.s.put('cursor', {'since': 'x'})
        self.assertEqual(self.s.get('cursor'), {'since': 'x'})


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
        self.assertTrue(c.checkout.startswith('/'))
        env = c.worker_env({'SUPERAGENT_GITHUB_TOKEN': 'x', 'GH_TOKEN': 'y', 'GITHUB_TOKEN': 'z', 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})
        with self.assertRaisesRegex(ValueError, 'poll_seconds'):
            cfgmod.Config(repo='o/r', agent_login='b', allowed=[], checkout='.', poll_seconds=5)


if __name__ == '__main__':
    unittest.main()
