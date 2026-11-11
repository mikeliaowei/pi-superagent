"""Reading a worker's activity from its Pi session files (superagent/worker_log.py)."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from superagent import worker_log


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


if __name__ == '__main__':
    unittest.main()
