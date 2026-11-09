"""The worker log (superagent/worklog.py) and the service log file `superagent run` writes."""

import io
import json
import logging
import logging.handlers
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from superagent import cli, worklog
from tests import toml_path

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'session.jsonl')


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-worklog-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class ReadSessionTest(Tmp):
    def test_the_fixture_yields_prompt_assistant_tool_call_trimmed_result_and_error(self):
        events = worklog.read_session(FIXTURE)
        kinds = [e.kind for e in events]
        prompt = next(e for e in events if e.kind == 'prompt')
        self.assertIn('Fix the failing login test', prompt.text)
        self.assertEqual(prompt.at, '2026-01-02T10:00:04.000Z')
        self.assertIn('The login test passes now.', [e.text for e in events if e.kind == 'assistant'])
        call = next(e for e in events if e.kind == 'tool_call')
        self.assertEqual(call.title, 'bash: pytest -q tests/test_login.py')
        self.assertIn('edit: src/login.py', [e.title for e in events if e.kind == 'tool_call'])
        result = events[kinds.index('tool_call') + 1]
        self.assertEqual(result.kind, 'tool_result')
        self.assertIn('bash', result.title)
        self.assertIn('error', result.title)  # isError
        self.assertLessEqual(len(result.text), worklog.RESULT_CHARS + 100)
        self.assertTrue(result.text.startswith('FIRST LINE'))
        self.assertTrue(result.text.endswith('LAST LINE: 1 failed'))
        self.assertIn('trimmed', result.text)
        (error,) = [e for e in events if e.kind == 'error']
        self.assertIn('429 rate limit exceeded', error.text)
        self.assertIn('thinking', kinds)
        self.assertEqual(kinds[-1], 'error')

    def test_malformed_and_partial_last_lines_are_skipped(self):
        events = worklog.read_session(FIXTURE)  # the fixture holds one malformed line and a cut-off last line
        self.assertTrue(events)
        self.assertNotIn('e12', [e.id for e in events])
        path = os.path.join(self.tmp, 'x.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not json\n{"type":"message","message":{"role":"user","content":"hi"},"timestamp":"t"}\n{"ty')
        self.assertEqual([(e.kind, e.text) for e in worklog.read_session(path)], [('prompt', 'hi')])

    def test_a_big_file_is_read_from_its_tail_and_returns_the_latest_events(self):
        path = os.path.join(self.tmp, 'big.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for i in range(3000):
                f.write(json.dumps({'type': 'message', 'timestamp': f't{i}', 'message': {
                    'role': 'assistant', 'content': [{'type': 'text', 'text': f'step {i} ' + 'x' * 500}]}}) + '\n')
        events = worklog.read_session(path, max_entries=10, max_bytes=100_000)
        self.assertEqual(len(events), 10)
        self.assertTrue(events[-1].text.startswith('step 2999 '))
        self.assertTrue(events[0].text.startswith('step 2990 '))
        tail = worklog.read_session(path, max_entries=5000, max_bytes=100_000)
        self.assertLess(len(tail), 200)  # only the last 100 kB was read
        self.assertTrue(tail[0].text.startswith('step '))  # the first, cut-off line in the window is dropped
        self.assertTrue(tail[-1].text.startswith('step 2999 '))

    def test_a_missing_file_has_no_events(self):
        self.assertEqual(worklog.read_session(os.path.join(self.tmp, 'gone.jsonl')), [])


class SessionFilesTest(Tmp):
    def test_only_the_tickets_files_match_oldest_first(self):
        d = os.path.join(self.tmp, 'sessions')
        os.makedirs(d)
        for name in ('2026-01-02T10-00-00-000Z_tk-1.jsonl', '2026-01-01T10-00-00-000Z_tk-1.jsonl',
                     '2026-01-01T10-00-00-000Z_tk-12.jsonl', '2026-01-01T10-00-00-000Z_xtk-1.jsonl.bak',
                     'notes.txt'):
            open(os.path.join(d, name), 'w').close()
        files = worklog.session_files(self.tmp, 'tk-1')
        self.assertEqual([os.path.basename(f) for f in files],
                         ['2026-01-01T10-00-00-000Z_tk-1.jsonl', '2026-01-02T10-00-00-000Z_tk-1.jsonl'])

    def test_a_missing_sessions_dir_has_no_files(self):
        self.assertEqual(worklog.session_files(self.tmp, 'tk-1'), [])


class ServiceLinesTest(Tmp):
    def test_lines_are_filtered_by_ticket_name_or_id_newest_last(self):
        path = os.path.join(self.tmp, 'service.log')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('2026 INFO [fix-it] new ticket for #1 from alice\n'
                    '2026 INFO [other] new ticket for #2 from bob\n'
                    '2026 INFO answer 3 for tk-1 dropped: the ticket ended\n'
                    '2026 INFO [fix-it-two] launched\n'
                    '2026 WARNING [fix-it] deliver failed (retry in 5s): boom\n')
        ticket = SimpleNamespace(id='tk-1', name='fix-it')
        self.assertEqual(worklog.service_lines(path, ticket),
                         ['2026 INFO [fix-it] new ticket for #1 from alice',
                          '2026 INFO answer 3 for tk-1 dropped: the ticket ended',
                          '2026 WARNING [fix-it] deliver failed (retry in 5s): boom'])
        self.assertEqual(worklog.service_lines(path, ticket, limit=1),
                         ['2026 WARNING [fix-it] deliver failed (retry in 5s): boom'])

    def test_a_missing_log_has_no_lines(self):
        self.assertEqual(worklog.service_lines(os.path.join(self.tmp, 'no.log'),
                                               SimpleNamespace(id='tk-1', name='x')), [])


class MaskSecretsTest(unittest.TestCase):
    def test_each_token_pattern_is_masked_and_normal_text_is_kept(self):
        secrets = ['ghp_' + 'a1B2' * 9, 'gho_' + 'Z' * 30, 'github_pat_11ABC_' + 'x' * 40,
                   'sk-ant-api03-' + 'Q' * 30, 'sk-' + 'k' * 40, 'AKIA' + 'ABCDEFGHIJKLMNOP',
                   '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----',
                   'Bearer ' + 'e' * 40, 'x-access-token:' + 'v' * 30]
        for s in secrets:
            masked = worklog.mask_secrets(f'before {s} after')
            self.assertNotIn(s, masked, s)
            self.assertTrue(masked.startswith('before '), masked)
            self.assertTrue(masked.endswith(' after'), masked)
        normal = 'git push origin agent/fix-it; ask-ant 12 sk- tokens; Bearer me'
        self.assertEqual(worklog.mask_secrets(normal), normal)

    def test_session_events_are_masked(self):
        tmp = tempfile.mkdtemp(prefix='superagent-mask-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        token = 'ghp_' + 'S' * 36
        path = os.path.join(tmp, 's.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'message', 'timestamp': 't', 'message': {
                'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'c', 'name': 'bash',
                                                  'arguments': {'command': f'echo {token}'}}]}}) + '\n')
        (event,) = worklog.read_session(path)
        self.assertNotIn(token, event.title + event.text)


class ServiceLogFileTest(Tmp):
    def test_run_attaches_a_rotating_file_handler_at_the_config_log_path(self):
        config = os.path.join(self.tmp, 'config.toml')
        with open(config, 'w') as f:
            f.write(f'[github]\nrepo = "o/r"\nagent_login = "bot"\ntoken_env = "SA_LOG_TOKEN"\n'
                    f'[policy]\nallowed = ["alice"]\n[executor]\ncheckout = "{toml_path(self.tmp)}/checkout"\n'
                    f'[service]\nstate_dir = "{toml_path(self.tmp)}/state"\nreport_port = 0\n')
        seen = {}

        def fake_run(coro):
            coro.close()  # nothing real starts
            handlers = [h for h in logging.getLogger().handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)]
            seen['files'] = [h.baseFilename for h in handlers]
            seen['rotation'] = [(h.maxBytes, h.backupCount) for h in handlers]
            logging.getLogger('superagent').info('[fix-it] launched with SA_LOG_TOKEN set')

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {'SA_LOG_TOKEN': 'ghp_' + 'T' * 36}), \
                mock.patch('superagent.cli.asyncio.run', side_effect=fake_run), \
                mock.patch('superagent.github.GitHub'), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', config, 'run'])
        self.assertEqual(code, 0)
        log_path = cli.load_config(config).log_path
        self.assertEqual(log_path, os.path.join(self.tmp, 'state', 'logs', 'service.log'))
        self.assertEqual(seen['files'], [os.path.abspath(log_path)])
        self.assertEqual(seen['rotation'], [(2 * 1024 * 1024, 3)])
        with open(log_path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('INFO [fix-it] launched', text)
        self.assertNotIn('T' * 36, text)
        self.assertFalse([h for h in logging.getLogger().handlers
                          if isinstance(h, logging.handlers.RotatingFileHandler)])  # closed on return


if __name__ == '__main__':
    unittest.main()
