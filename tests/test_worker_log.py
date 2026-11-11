"""The worker log (superagent/worker_log.py): a worker's activity read from its Pi or Copilot session files, its usage,
the service log's lines about a Ticket, and the service log file `superagent run` writes."""

import io
import json
import logging
import logging.handlers
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from superagent import cli, worker_log
from tests import toml_path

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'session.jsonl')


def entry(role, content, **extra):
    return {'type': 'message', 'timestamp': '2026-11-09T03:24:40.077Z',
            'message': {'role': role, 'content': content, **extra}}


SESSION = [
    {'type': 'session', 'version': 3, 'id': 'tk-1', 'timestamp': '2026-11-09T03:24:39.408Z'},
    {'type': 'model_change', 'timestamp': '2026-11-09T03:24:40.050Z', 'provider': 'p', 'modelId': 'm'},
    entry('system', ''),
    entry('user', [{'type': 'text', 'text': 'New assignment. Work it in this directory.'}]),
    entry('assistant', [{'type': 'thinking', 'thinking': 'Check the branch first.'},
                        {'type': 'text', 'text': 'Looking at the repo.'},
                        {'type': 'toolCall', 'id': 'c1', 'name': 'bash', 'arguments': {'command': 'git status'}},
                        {'type': 'toolCall', 'id': 'c2', 'name': 'read', 'arguments': {'path': 'README.md'}},
                        {'type': 'toolCall', 'id': 'c3', 'name': 'grep', 'arguments': {'pattern': 'todo'}}]),
    entry('toolResult', [{'type': 'text', 'text': 'On branch agent/fix-it'}], toolCallId='c1', toolName='bash',
          isError=False),
    entry('toolResult', [{'type': 'text', 'text': 'no such file'}], toolCallId='c2', toolName='read', isError=True),
]


class WorkerLogTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='superagent-wlog-')
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.sessions = os.path.join(self.state, 'sessions')
        os.makedirs(self.sessions)

    def write(self, name, entries, tail=''):
        path = os.path.join(self.sessions, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(json.dumps(e) + '\n' for e in entries) + tail)
        return path

    def test_finds_only_the_tickets_session_files_oldest_first(self):
        later = self.write('2026-11-09T05-00-00-000Z_tk-1.jsonl', [])
        first = self.write('2026-11-09T03-24-39-408Z_tk-1.jsonl', [])
        self.write('2026-11-09T04-00-00-000Z_tk-12.jsonl', [])
        self.assertEqual(worker_log.session_files(self.state, 'tk-1'), [first, later])
        self.assertEqual(worker_log.session_files(self.state, 'tk-none'), [])

    def test_no_sessions_folder_means_no_files(self):
        self.assertEqual(worker_log.session_files(os.path.join(self.state, 'missing'), 'tk-1'), [])

    def test_a_copilot_session_is_read_from_the_workers_copilot_home(self):
        home = worker_log.copilot_home(self.state)
        events = os.path.join(home, 'session-state', 'sid-1', 'events.jsonl')
        os.makedirs(os.path.dirname(events))
        os.makedirs(os.path.join(self.state, 'copilot-sessions'))
        with open(worker_log.copilot_session_file(self.state, 'tk-1'), 'w') as f:
            f.write('sid-1')
        at = '2026-11-09T03:24:40.077Z'
        with open(events, 'w', encoding='utf-8') as f:
            for e in [{'type': 'session.start', 'data': {}},
                      {'type': 'user.message', 'data': {'content': 'New assignment.'}},
                      {'type': 'assistant.message', 'data': {'content': 'Looking.', 'toolRequests': [
                          {'name': 'bash', 'arguments': {'command': 'git status'}}]}},
                      {'type': 'tool.execution_complete', 'data': {'success': True, 'result': {'content': 'clean'}}},
                      {'type': 'tool.execution_complete', 'data': {'success': False, 'error': {'message': 'boom'}}},
                      {'type': 'session.usage_record', 'timestamp': at, 'data': {'usage': {
                          'inputTokens': 10, 'outputTokens': 2, 'cacheReadTokens': 5, 'cacheWriteTokens': 1}}}]:
                f.write(json.dumps({'timestamp': at, **e}) + '\n')
        paths = worker_log.session_files(self.state, 'tk-1')
        self.assertEqual(paths, [events])
        self.assertEqual([(s.kind, s.title, s.text) for s in worker_log.steps(paths)],
                         [('request', 'Assignment', 'New assignment.'), ('said', 'Agent', 'Looking.'),
                          ('tool', 'bash', 'git status'), ('result', 'tool result', 'clean'),
                          ('error', 'tool failed', 'boom')])
        self.assertEqual(worker_log.usage(paths), {'input': 10, 'output': 2, 'cacheRead': 5, 'cacheWrite': 1,
                                                   'tokens': 18, 'cost': 0.0})

    def test_steps_follow_the_session_in_order(self):
        path = self.write('2026-11-09T03-24-39-408Z_tk-1.jsonl', SESSION)
        got = [(s.kind, s.title, s.text) for s in worker_log.steps([path])]
        self.assertEqual(got, [
            ('request', 'Assignment', 'New assignment. Work it in this directory.'),
            ('reasoning', 'Reasoning', 'Check the branch first.'),
            ('said', 'Agent', 'Looking at the repo.'),
            ('tool', 'bash', 'git status'),
            ('tool', 'read', 'README.md'),
            ('tool', 'grep', '{"pattern": "todo"}'),
            ('result', 'bash result', 'On branch agent/fix-it'),
            ('error', 'read failed', 'no such file'),
        ])
        self.assertEqual(worker_log.steps([path])[0].at, '2026-11-09T03:24:40.077Z')

    def test_a_half_written_last_line_is_skipped(self):
        path = self.write('2026-11-09T03-24-39-408Z_tk-1.jsonl', SESSION[:4], tail='{"type": "mess')
        self.assertEqual([s.kind for s in worker_log.steps([path])], ['request'])

    def test_secrets_are_masked_in_every_step(self):
        leaky = [entry('assistant', [{'type': 'toolCall', 'id': 'c', 'name': 'bash',
                                      'arguments': {'command': 'echo tok-SECRET'}}]),
                 entry('toolResult', [{'type': 'text', 'text': 'tok-SECRET ghp_abc'}], toolName='bash',
                       isError=False)]
        path = self.write('2026-11-09T03-24-39-408Z_tk-1.jsonl', leaky)
        texts = [s.text for s in worker_log.steps([path], secrets=['tok-SECRET', 'ghp_abc', '', None])]
        self.assertEqual(texts, ['echo ***', '*** ***'])

    def test_a_tool_call_shows_its_other_arguments_too(self):
        path = self.write('2026-11-09T03-24-39-408Z_tk-1.jsonl', [entry('assistant', [
            {'type': 'toolCall', 'id': 'c', 'name': 'edit', 'arguments': {'path': 'a.py', 'oldText': 'x', 'newText': 'y'}}])])
        (step,) = worker_log.steps([path])
        self.assertEqual((step.kind, step.title), ('tool', 'edit'))
        self.assertTrue(step.text.startswith('a.py\n'))
        self.assertEqual(json.loads(step.text.split('\n', 1)[1]), {'oldText': 'x', 'newText': 'y'})

    def test_usage_sums_every_model_call_and_can_start_at_a_time(self):
        def call(at, i, o, cr, cw, cost):
            e = entry('assistant', [], usage={'input': i, 'output': o, 'cacheRead': cr, 'cacheWrite': cw,
                                              'totalTokens': i + o + cr + cw, 'cost': {'total': cost}})
            return {**e, 'timestamp': at}
        first = self.write('2026-11-09T03-00-00-000Z_tk-1.jsonl',
                           [*SESSION, call('2026-11-09T03:30:00.000Z', 10, 20, 300, 40, 0.25)])
        later = self.write('2026-11-09T05-00-00-000Z_tk-1.jsonl',
                           [call('2026-11-09T05:30:00.000Z', 1, 2, 3, 4, 0.5),
                            entry('assistant', [], usage={'input': 'x', 'cost': None}),  # malformed: counts 0
                            entry('user', 'no usage here')], tail='{"type": "mess')
        u = worker_log.usage([first, later])
        self.assertEqual({k: u[k] for k in ('input', 'output', 'cacheRead', 'cacheWrite', 'tokens')},
                         {'input': 11, 'output': 22, 'cacheRead': 303, 'cacheWrite': 44, 'tokens': 380})
        self.assertAlmostEqual(u['cost'], 0.75)
        recent = worker_log.usage([first, later], since=datetime(2026, 11, 9, 5, tzinfo=timezone.utc))
        self.assertEqual((recent['tokens'], recent['cost']), (10, 0.5))
        self.assertEqual(worker_log.usage([])['tokens'], 0)
        later_day = self.write('2026-11-10T01-00-00-000Z_tk-1.jsonl', [call('2026-11-10T01:00:00.000Z', 5, 5, 0, 0, 0)])
        self.assertEqual(worker_log.daily_tokens([first, later, later_day]), {'2026-11-09': 380, '2026-11-10': 10})


class FixtureTest(unittest.TestCase):
    """A recorded Pi session: a malformed line, a cut-off last line, a long tool result and a provider error."""

    def test_the_fixture_yields_request_agent_tool_call_trimmed_result_and_the_stop_reason(self):
        steps = worker_log.steps([FIXTURE])
        kinds = [s.kind for s in steps]
        request = next(s for s in steps if s.kind == 'request')
        self.assertIn('Fix the failing login test', request.text)
        self.assertEqual(request.at, '2026-01-02T10:00:04.000Z')
        self.assertIn('The login test passes now.', [s.text for s in steps if s.kind == 'said'])
        call = next(s for s in steps if s.kind == 'tool')
        self.assertEqual((call.title, call.text.split('\n')[0]), ('bash', 'pytest -q tests/test_login.py'))
        self.assertIn('"timeout": 120', call.text)  # its other arguments
        self.assertIn(('edit', 'src/login.py'), [(s.title, s.text.split('\n')[0]) for s in steps if s.kind == 'tool'])
        result = steps[kinds.index('tool') + 1]
        self.assertEqual((result.kind, result.title), ('error', 'bash failed'))  # isError
        self.assertLessEqual(len(result.text), worker_log.RESULT_CHARS + 100)
        self.assertTrue(result.text.startswith('FIRST LINE'))
        self.assertTrue(result.text.endswith('LAST LINE: 1 failed'))
        self.assertIn('trimmed', result.text)
        (stopped,) = [s for s in steps if s.kind == 'stopped']
        self.assertEqual(stopped.title, 'stopped: error')
        self.assertIn('429 rate limit exceeded', stopped.text)
        self.assertIn('reasoning', kinds)
        self.assertEqual(kinds[-1], 'stopped')  # the malformed line and the cut-off last line are skipped


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-wlog-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class TailTest(Tmp):

    def test_a_big_file_is_read_from_its_tail_and_returns_the_latest_steps(self):
        path = os.path.join(self.tmp, 'big.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for i in range(3000):
                f.write(json.dumps({'type': 'message', 'timestamp': f't{i}', 'message': {
                    'role': 'assistant', 'content': [{'type': 'text', 'text': f'step {i} ' + 'x' * 500}]}}) + '\n')
        tail = worker_log.steps([path], max_bytes=100_000)
        self.assertLess(len(tail), 200)  # only the last 100 kB was read
        self.assertTrue(tail[0].text.startswith('step '))  # the first, cut-off line in the window is dropped
        self.assertTrue(tail[-1].text.startswith('step 2999 '))
        self.assertEqual(len(worker_log.steps([path], max_bytes=None)), 3000)
        self.assertEqual(worker_log.usage([path])['tokens'], 0)  # usage reads the whole file

    def test_a_missing_file_has_no_steps_and_no_usage(self):
        gone = os.path.join(self.tmp, 'gone.jsonl')
        self.assertEqual(worker_log.steps([gone]), [])
        self.assertEqual(worker_log.usage([gone])['tokens'], 0)

    def test_only_the_tickets_files_match_oldest_first(self):
        d = os.path.join(self.tmp, 'sessions')
        os.makedirs(d)
        for name in ('2026-01-02T10-00-00-000Z_tk-1.jsonl', '2026-01-01T10-00-00-000Z_tk-1.jsonl',
                     '2026-01-01T10-00-00-000Z_tk-12.jsonl', '2026-01-01T10-00-00-000Z_xtk-1.jsonl.bak',
                     'notes.txt'):
            open(os.path.join(d, name), 'w').close()
        files = worker_log.session_files(self.tmp, 'tk-1')
        self.assertEqual([os.path.basename(f) for f in files],
                         ['2026-01-01T10-00-00-000Z_tk-1.jsonl', '2026-01-02T10-00-00-000Z_tk-1.jsonl'])


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
        self.assertEqual(worker_log.service_lines(path, ticket),
                         ['2026 INFO [fix-it] new ticket for #1 from alice',
                          '2026 INFO answer 3 for tk-1 dropped: the ticket ended',
                          '2026 WARNING [fix-it] deliver failed (retry in 5s): boom'])
        self.assertEqual(worker_log.service_lines(path, ticket, limit=1),
                         ['2026 WARNING [fix-it] deliver failed (retry in 5s): boom'])

    def test_lines_mask_the_given_secrets_and_token_patterns(self):
        path = os.path.join(self.tmp, 'service.log')
        token = 'ghp_' + 'L' * 36
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f'2026 INFO [fix-it] saw {token} and tok-SECRET\n')
        (line,) = worker_log.service_lines(path, SimpleNamespace(id='tk-1', name='fix-it'), secrets=['tok-SECRET'])
        self.assertNotIn(token, line)
        self.assertNotIn('tok-SECRET', line)

    def test_a_missing_log_has_no_lines(self):
        self.assertEqual(worker_log.service_lines(os.path.join(self.tmp, 'no.log'),
                                                  SimpleNamespace(id='tk-1', name='x')), [])


class MaskSecretsTest(unittest.TestCase):
    def test_each_token_pattern_is_masked_and_normal_text_is_kept(self):
        secrets = ['ghp_' + 'a1B2' * 9, 'gho_' + 'Z' * 30, 'github_pat_11ABC_' + 'x' * 40,
                   'sk-ant-api03-' + 'Q' * 30, 'sk-' + 'k' * 40, 'AKIA' + 'ABCDEFGHIJKLMNOP',
                   '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----',
                   'Bearer ' + 'e' * 40, 'x-access-token:' + 'v' * 30]
        for s in secrets:
            masked = worker_log.mask_secrets(f'before {s} after')
            self.assertNotIn(s, masked, s)
            self.assertTrue(masked.startswith('before '), masked)
            self.assertTrue(masked.endswith(' after'), masked)
        normal = 'git push origin agent/fix-it; ask-ant 12 sk- tokens; Bearer me'
        self.assertEqual(worker_log.mask_secrets(normal), normal)

    def test_token_patterns_are_masked_in_steps_without_being_passed(self):
        tmp = tempfile.mkdtemp(prefix='superagent-mask-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        token = 'ghp_' + 'S' * 36
        path = os.path.join(tmp, 's.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'message', 'timestamp': 't', 'message': {
                'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'c', 'name': 'bash',
                                                  'arguments': {'command': f'echo {token}'}}]}}) + '\n')
        (step,) = worker_log.steps([path])
        self.assertNotIn(token, step.title + step.text)


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
