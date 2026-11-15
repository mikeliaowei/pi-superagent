"""Jira issues and Confluence pages a request names, fetched read-only by the service for its worker
(superagent/atlassian.py), with no network: a fake server answers instead."""

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from superagent import atlassian, cli
from superagent.config import Config
from tests.fake_github import FakeGitHub, FakeGitHubs
from tests.test_multirepo import ServiceBase, make_clone
from tests.test_service import Pi, until

JIRA_TOKEN, WIKI_TOKEN = 'jira-pat-not-for-workers', 'wiki-pat-not-for-workers'  # noqa: S105 - fake test tokens
ISSUE = {'key': 'RWT-1', 'fields': {
    'summary': 'Fix the nav', 'status': {'name': 'In Progress'}, 'issuetype': {'name': 'Story'},
    'priority': {'name': 'High'}, 'assignee': {'displayName': 'Ana'}, 'reporter': {'name': 'bo'},
    'labels': ['ui'], 'components': [{'name': 'umbrella'}], 'description': 'The menu overlaps.',
    'comment': {'comments': [{'author': {'displayName': f'P{i}'}, 'created': f'2026-11-0{i}T10:00:00.000',
                              'body': f'note {i}'} for i in range(1, 8)]}}}
PAGE = {'id': '42', 'title': 'Go-live', 'space': {'key': 'TCC'}, 'version': {'number': 3},
        'body': {'storage': {'value': '<h2>Steps</h2><p>Deploy &amp; check</p><ul><li>one</li><li>two</li></ul>'
                                      '<table><tr><th>A</th><td>B</td></tr></table><script>x()</script>'}}}


class FakeAtlassian:
    """urlopen stand-in: answers the Jira and Confluence REST calls, records them, refuses an unknown issue."""

    def __init__(self):
        self.calls, self.posts = [], []

    def __call__(self, req, timeout=None):
        url, auth = req.full_url, req.get_header('Authorization')
        self.calls.append((url, auth))
        if auth not in (f'Bearer {JIRA_TOKEN}', f'Bearer {WIKI_TOKEN}') and '/rest/api/user/current' not in url:
            raise urllib.error.HTTPError(url, 401, 'Unauthorized', {}, None)  # as Jira and Confluence answer a bad token
        if req.get_method() == 'PUT':  # a page update: only as the next version of the page, as Confluence takes it
            body = json.loads(req.data)
            self.posts.append((url, auth, body))
            if url.endswith('/rest/api/content/42') and body['version']['number'] == 4:  # PAGE is at version 3
                return io.BytesIO(json.dumps({'id': '42', '_links': {'webui': '/display/TCC/Go-live'}}).encode())
            raise urllib.error.HTTPError(url, 409, 'Conflict', {}, io.BytesIO(
                b'{"message": "Version must be incremented on update. Current version is: 4"}'))
        if req.get_method() == 'POST':
            body = json.loads(req.data)
            self.posts.append((url, auth, body))
            if url.endswith('/rest/api/2/issue'):
                if body['fields']['project']['key'] == 'BAD':
                    error = {'errorMessages': [], 'errors': {'project': 'valid project is required'}}
                    raise urllib.error.HTTPError(url, 400, 'Bad Request', {}, io.BytesIO(json.dumps(error).encode()))
                return io.BytesIO(b'{"id": "1", "key": "RWT-9"}')
            if url.endswith('/rest/api/2/issue/RWT-1/comment'):
                return io.BytesIO(b'{"id": "77"}')
            if url.endswith('/rest/api/content'):
                return io.BytesIO(json.dumps({'id': '500', '_links': {'webui': '/display/TCC/Design'}}).encode())
        if '/rest/api/2/issue/RWT-1?' in url and auth == f'Bearer {JIRA_TOKEN}':
            return io.BytesIO(json.dumps(ISSUE).encode())
        if url.startswith('https://wiki.example/rest/api/content/42?') and auth == f'Bearer {WIKI_TOKEN}':
            return io.BytesIO(json.dumps(PAGE).encode())
        if url.endswith('/rest/api/2/myself') and auth == f'Bearer {JIRA_TOKEN}':
            return io.BytesIO(b'{"name": "wei", "displayName": "Liao, Wei"}')
        if url.endswith('/rest/api/user/current'):  # Confluence answers a token it does not take as anonymous
            known = auth == f'Bearer {WIKI_TOKEN}'
            return io.BytesIO(json.dumps({'type': 'known' if known else 'anonymous',
                                          'displayName': 'Liao, Wei' if known else 'Anonymous'}).encode())
        if 'title=Go-live' in url and 'spaceKey=TCC' in url:
            return io.BytesIO(json.dumps({'results': [PAGE]}).encode())
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)


def config(**kw):
    """A config whose paths are never used (nothing here runs git or a worker)."""
    return Config(repo='o/r', agent_login='bot', allowed=['alice'], checkout='unused-checkout',
                  state_dir='unused-state', **{'jira_url': 'https://jira.example',
                                               'confluence_url': 'https://wiki.example', **kw})


class ReferencesTest(unittest.TestCase):
    def test_keys_and_links_are_found_once_and_standards_are_not_issues(self):
        text = ('Fix RWT-1 (see https://jira.example/browse/RWT-2 and RWT-1 again), keep UTF-8 and ISO-8601; '
                'docs: https://wiki.example/pages/viewpage.action?pageId=42 and '
                'https://wiki.example/spaces/TCC/pages/43/Some+Title and https://wiki.example/display/TCC/Go-live '
                'but not https://elsewhere.example/browse/RWT-9 path/RWT-8.txt')
        self.assertEqual(atlassian.references(config(), text),
                         [('jira', 'RWT-2'), ('jira', 'RWT-1'), ('confluence', '42'), ('confluence', '43'),
                          ('confluence-title', ('TCC', 'Go-live'))])
        self.assertEqual(atlassian.references(config(jira_url='', confluence_url=''), text), [])  # off
        many = ' '.join(f'RWT-{i}' for i in range(1, 20))
        self.assertEqual(len(atlassian.references(config(), many)), atlassian.MAX_REFS)

    def test_an_issue_and_pages_become_markdown_read_with_the_bearer_tokens(self):
        fake, env = FakeAtlassian(), {'JIRA_API_TOKEN': JIRA_TOKEN, 'CONFLUENCE_API_TOKEN': WIKI_TOKEN}
        with mock.patch.object(atlassian, 'urlopen', fake):
            name, issue = atlassian.fetch(config(), ('jira', 'RWT-1'), env)
            self.assertEqual(name, 'jira-RWT-1.md')
            self.assertTrue(issue.startswith('# RWT-1: Fix the nav\nJira issue https://jira.example/browse/RWT-1'))
            for part in ('In Progress', 'priority High', 'assignee Ana', 'Labels: ui', 'The menu overlaps.',
                         f'## Latest comments ({atlassian.COMMENTS})', 'note 7'):
                self.assertIn(part, issue)
            self.assertNotIn('note 2', issue)  # only the latest comments
            name, page = atlassian.fetch(config(), ('confluence', '42'), env)
            self.assertEqual(name, 'confluence-42.md')
            self.assertIn('space TCC · version 3', page)
            self.assertIn('## Steps\n\nDeploy & check', page)
            self.assertIn('- one\n\n- two', page)
            self.assertIn(' | A | B', page)
            self.assertNotIn('x()', page)
            self.assertEqual(atlassian.fetch(config(), ('confluence-title', ('TCC', 'Go-live')), env)[0],
                             'confluence-42.md')
            self.assertIsNone(atlassian.fetch(config(), ('jira', 'RWT-404'), env))  # not found: nothing
            self.assertIsNone(atlassian.fetch(config(), ('jira', 'RWT-1'), {}))  # no token: nothing, no request
            self.assertIsNone(atlassian.fetch(config(jira_url='file:///etc'), ('jira', 'RWT-1'), env))
        self.assertTrue(all(url.startswith('https://') for url, _ in fake.calls))
        self.assertEqual(len([c for c in fake.calls if 'RWT-1?' in c[0]]), 1)  # the refusals made no request

    def test_workers_never_get_the_tokens(self):
        env = config().worker_env({'JIRA_API_TOKEN': JIRA_TOKEN, 'CONFLUENCE_API_TOKEN': WIKI_TOKEN, 'PATH': '/bin'})
        self.assertEqual(env, {'PATH': '/bin'})

    def test_check_says_who_each_token_signs_in_as_or_why_not(self):
        good = {'JIRA_API_TOKEN': JIRA_TOKEN, 'CONFLUENCE_API_TOKEN': WIKI_TOKEN}
        with mock.patch.object(atlassian, 'urlopen', FakeAtlassian()):
            self.assertEqual(atlassian.whoami(config(), 'jira', good), ('Liao, Wei', '', ''))
            self.assertEqual(atlassian.whoami(config(), 'confluence', good), ('Liao, Wei', '', ''))
            stale = {'JIRA_API_TOKEN': 'old', 'CONFLUENCE_API_TOKEN': 'old'}
            self.assertIn('refused JIRA_API_TOKEN (HTTP 401)', atlassian.whoami(config(), 'jira', stale)[1])
            self.assertIn('refused CONFLUENCE_API_TOKEN (HTTP 401)', atlassian.whoami(config(), 'confluence', stale)[1])
            self.assertEqual(atlassian.whoami(config(), 'jira', {}),
                             ('', 'environment variable JIRA_API_TOKEN is not set', ''))
            self.assertIn('not an http(s) URL', atlassian.whoami(config(jira_url='file:///x'), 'jira', good)[1])

    def test_a_token_replaced_after_the_window_opened_is_read_from_the_windows_user(self):
        # The window (and `superagent run` started in it) still has the old token; Windows has the new one saved.
        saved = {'JIRA_API_TOKEN': JIRA_TOKEN, 'CONFLUENCE_API_TOKEN': WIKI_TOKEN}
        fake = FakeAtlassian()
        with mock.patch.object(atlassian, 'urlopen', fake), \
                mock.patch.object(atlassian, 'saved_token', saved.get), \
                mock.patch.dict(os.environ, {'JIRA_API_TOKEN': 'old', 'CONFLUENCE_API_TOKEN': WIKI_TOKEN}):
            who, error, note = atlassian.whoami(config(), 'jira')
            self.assertEqual((who, error), ('Liao, Wei', ''))
            self.assertIn('saved for your Windows user', note)
            self.assertEqual(atlassian.whoami(config(), 'confluence'), ('Liao, Wei', '', ''))  # same token: no note
            self.assertEqual(atlassian.fetch(config(), ('jira', 'RWT-1'))[0], 'jira-RWT-1.md')
            self.assertEqual(atlassian.write(config(), {'kind': 'jira-comment', 'issue': 'RWT-1', 'body': 'hi'})[0],
                             'comment on RWT-1')
        self.assertEqual([auth for _, auth, _ in fake.posts], [f'Bearer {JIRA_TOKEN}'])  # refused first: nothing written
        self.assertEqual(atlassian.tokens('JIRA_API_TOKEN', {'JIRA_API_TOKEN': 'x'}), ['x'])  # given env: no registry


class FetchedForTheWorkerTest(ServiceBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.one_co = make_clone(self.tmp, 'one')
        self.fakes = FakeGitHubs(FakeGitHub('a/one'))

    def ticket(self):
        (t,) = self.store.tickets()
        return t

    async def test_a_request_naming_an_issue_gives_its_worker_the_issue_as_a_file_once(self):
        fake = FakeAtlassian()
        self.pi = Pi({'mode': 'hang'})
        self.service(self.config(repos=[{'name': 'a/one', 'checkout': self.one_co}], local_user='wei',
                                 jira_url='https://jira.example'), self.fakes, self.pi)
        with mock.patch.object(atlassian, 'urlopen', fake), \
                mock.patch.dict(os.environ, {'JIRA_API_TOKEN': JIRA_TOKEN}):
            cli.ask_request(self.cfg, self.store, 'Fix RWT-1, and RWT-404 if it exists', ['a/one'])
            await self.round()
            await until(lambda: self.pi.runs and self.pi.runs[0].messages)
        message = self.pi.runs[0].messages[0]
        (path,) = self.store.trigger_files(self.ticket().mandate['trigger_id'])
        self.assertTrue(path.endswith('jira-RWT-1.md'))
        self.assertIn(path, message)
        with open(path, encoding='utf-8') as f:
            self.assertIn('# RWT-1: Fix the nav', f.read())
        self.assertNotIn(JIRA_TOKEN, json.dumps(self.pi.runs[0].env))
        self.assertEqual(len(fake.calls), 2)  # RWT-1 and RWT-404, each once


class WriteTest(unittest.TestCase):
    MD = ('# Design\n\nThe **menu** overlaps the *header*; see [RWT-1](https://j/browse/RWT-1) and `nav.py`.\n\n'
          '- one\n  - nested\n1. first\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n> note\n\n```python\nx = 1 < 2\n```\n---\n')

    def test_markdown_becomes_jira_wiki_markup_and_confluence_storage(self):
        self.assertEqual(atlassian.to_jira(self.MD).split('\n\n'), [
            'h1. Design', 'The *menu* overlaps the _header_; see [RWT-1|https://j/browse/RWT-1] and {{nav.py}}.',
            '* one\n** nested\n# first', '||A||B||\n|1|2|', 'bq. note', '{code:python}\nx = 1 < 2\n{code}', '----'])
        page = atlassian.to_storage(self.MD)
        for part in ('<h1>Design</h1>', '<strong>menu</strong>', '<em>header</em>', '<code>nav.py</code>',
                     '<a href="https://j/browse/RWT-1">RWT-1</a>', '<ul><li>one<ul><li>nested</li></ul></li></ul>',
                     '<ol><li>first</li></ol>', '<tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>',
                     '<blockquote><p>note</p></blockquote>', '<![CDATA[x = 1 < 2]]>', '<hr/>'):
            self.assertIn(part, page)
        self.assertIn('a &lt; b', atlassian.to_storage('a < b'))  # text is escaped, code is not

    def test_proposals_are_checked_before_anyone_sees_them(self):
        from superagent.report import validate_proposal
        ok, _ = validate_proposal({'kind': 'jira-issue', 'project': 'RWT', 'summary': ' Add x ', 'body': 'b'})
        self.assertEqual(ok, {'kind': 'jira-issue', 'project': 'RWT', 'summary': 'Add x', 'issuetype': 'Task',
                              'body': 'b'})
        for body, error in (({'kind': 'jira-delete', 'body': 'b'}, 'kind must be'),
                            ({'kind': 'jira-issue', 'project': 'rwt', 'summary': 's', 'body': 'b'}, 'project'),
                            ({'kind': 'jira-comment', 'issue': 'RWT', 'body': 'b'}, 'issue key'),
                            ({'kind': 'confluence-page', 'space': 'TCC', 'title': 't', 'parent': 'x', 'body': 'b'},
                             'parent'),
                            ({'kind': 'confluence-page', 'space': 'TCC', 'title': 't', 'body': '  '}, 'non-empty')):
            self.assertIn(error, validate_proposal(body)[1])

    def test_an_existing_page_gets_a_new_version_appended_or_replaced(self):
        from superagent.report import validate_proposal
        fake, env = FakeAtlassian(), {'CONFLUENCE_API_TOKEN': WIKI_TOKEN}
        old = PAGE['body']['storage']['value']
        with mock.patch.object(atlassian, 'urlopen', fake):
            add = validate_proposal({'kind': 'confluence-update', 'page': '42', 'body': 'Moved to **develop**.'})[0]
            self.assertEqual(add['mode'], 'append')  # the default keeps what the page has
            self.assertIn('adds to the end of Confluence page 42', atlassian.target(add))
            self.assertEqual(atlassian.write(config(), add, env),
                             ('Confluence page Go-live (version 4)', 'https://wiki.example/display/TCC/Go-live'))
            url, _, sent = fake.posts[-1]
            self.assertEqual((url, sent['version']['number'], sent['title'], sent['space'], sent['type']),
                             ('https://wiki.example/rest/api/content/42', 4, 'Go-live', {'key': 'TCC'}, 'page'))
            self.assertEqual(sent['body']['storage']['value'], old + '<p>Moved to <strong>develop</strong>.</p>')
            whole = {**add, 'mode': 'replace', 'title': 'Go-live v2', 'body': '# New'}
            self.assertIn('REPLACES the whole text', atlassian.target(whole))
            self.assertEqual(atlassian.write(config(), whole, env)[0], 'Confluence page Go-live v2 (version 4)')
            self.assertEqual(fake.posts[-1][2]['body']['storage']['value'], '<h1>New</h1>')
            # Someone saved version 3 after this read version 2: the update (as version 3) is refused, not merged.
            with mock.patch.dict(PAGE['version'], {'number': 2}), \
                    self.assertRaisesRegex(atlassian.WriteError, 'HTTP 409: Version must be incremented'):
                atlassian.write(config(), add, env)
        for body, error in (({'kind': 'confluence-update', 'page': 'TSL', 'body': 'b'}, 'id of the Confluence page'),
                            ({'kind': 'confluence-update', 'page': '42', 'mode': 'merge', 'body': 'b'}, 'mode')):
            self.assertIn(error, validate_proposal(body)[1])

    def test_an_approved_proposal_is_written_with_the_service_token(self):
        fake, env = FakeAtlassian(), {'JIRA_API_TOKEN': JIRA_TOKEN, 'CONFLUENCE_API_TOKEN': WIKI_TOKEN}
        with mock.patch.object(atlassian, 'urlopen', fake):
            self.assertEqual(atlassian.write(config(), {'kind': 'jira-issue', 'project': 'RWT', 'summary': 'S',
                                                        'issuetype': 'Story', 'body': '**b**'}, env),
                             ('Jira issue RWT-9', 'https://jira.example/browse/RWT-9'))
            self.assertEqual(fake.posts[-1][2]['fields'], {'project': {'key': 'RWT'}, 'summary': 'S',
                                                           'issuetype': {'name': 'Story'}, 'description': '*b*'})
            comment = atlassian.write(config(), {'kind': 'jira-comment', 'issue': 'RWT-1', 'body': 'hi'}, env)
            self.assertEqual(comment[1], 'https://jira.example/browse/RWT-1?focusedCommentId=77')
            what, url = atlassian.write(config(), {'kind': 'confluence-page', 'space': 'TCC', 'title': 'Design',
                                                   'parent': '42', 'body': '# D'}, env)
            self.assertEqual((what, url), ('Confluence page Design', 'https://wiki.example/display/TCC/Design'))
            sent = fake.posts[-1]
            self.assertEqual((sent[1], sent[2]['ancestors'], sent[2]['body']['storage']['value']),
                             (f'Bearer {WIKI_TOKEN}', [{'id': '42'}], '<h1>D</h1>'))
            with self.assertRaisesRegex(atlassian.WriteError, 'HTTP 400: project: valid project is required'):
                atlassian.write(config(), {'kind': 'jira-issue', 'project': 'BAD', 'summary': 'S',
                                           'issuetype': 'Task', 'body': 'b'}, env)
            with self.assertRaisesRegex(atlassian.WriteError, 'JIRA_API_TOKEN is not set'):
                atlassian.write(config(), {'kind': 'jira-comment', 'issue': 'RWT-1', 'body': 'b'}, {})
        self.assertEqual(len(fake.posts), 4)  # the missing token made no request


def propose(proposal):
    """A worker step that proposes a write over the report server, as `superagent-report propose` does."""
    def step(cwd, env):
        import urllib.request
        req = urllib.request.Request(env['SUPERAGENT_URL'] + '/propose', data=json.dumps(proposal).encode(),
                                     method='POST',
                                     headers={'Authorization': f"Bearer {env['SUPERAGENT_TICKET_TOKEN']}",
                                              'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - the test's localhost report server
            assert json.load(r)['proposal']
    return step


class ProposalFlowTest(ServiceBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.one_co = make_clone(self.tmp, 'one')
        self.fakes = FakeGitHubs(FakeGitHub('a/one'))
        self.server.on_propose = lambda tid, p: self.svc.on_propose(tid, p)

    def outbox(self):
        (t,) = self.store.tickets()
        return [m['body'] for m in self.store.local_messages(t.id) if m['kind'] == 'reply']

    async def test_a_proposed_write_waits_for_approval_then_the_service_writes_it_and_tells_the_worker(self):
        fake = FakeAtlassian()
        doc = {'kind': 'confluence-page', 'space': 'TCC', 'title': 'Design', 'body': '# Nav design\n\nUse a drawer.'}
        issue = {'kind': 'jira-issue', 'project': 'RWT', 'summary': 'Follow-up', 'body': 'Later.'}

        def both(cwd, env):
            propose(doc)(cwd, env)
            propose(issue)(cwd, env)
        self.pi = Pi({'mode': 'hang', 'before': both})
        self.service(self.config(repos=[{'name': 'a/one', 'checkout': self.one_co}], local_user='wei',
                                 jira_url='https://jira.example', confluence_url='https://wiki.example'),
                     self.fakes, self.pi)
        cli.ask_request(self.cfg, self.store, 'Write the nav design doc', ['a/one'])
        with mock.patch.object(atlassian, 'urlopen', fake), \
                mock.patch.dict(os.environ, {'CONFLUENCE_API_TOKEN': WIKI_TOKEN, 'JIRA_API_TOKEN': JIRA_TOKEN}):
            await self.round()
            await until(lambda: len(self.store.proposals()) == 2)
            await self.svc.poll_once()
            await self.svc.run_effects()  # replies go out through the effect queue
            self.assertEqual(fake.posts, [])  # nothing is written before a person approves
            page, follow_up = self.store.proposals(state='pending')
            self.assertTrue(any('I propose to write a Confluence page in space TCC: Design' in m
                                for m in self.outbox()))
            self.assertIn('approved #1', cli.decide_proposal(self.cfg, self.store, page['id'], True))
            self.assertIn('rejected #2', cli.decide_proposal(self.cfg, self.store, follow_up['id'], False, 'not now'))
            with self.assertRaisesRegex(RuntimeError, 'already approved'):
                cli.decide_proposal(self.cfg, self.store, page['id'], False)
            await self.svc.poll_once()
            await self.svc.run_effects()
            (sent,) = fake.posts
            self.assertEqual((sent[0], sent[2]['title']), ('https://wiki.example/rest/api/content', 'Design'))
            self.assertEqual(self.store.proposal(1)['state'], 'done')
            self.assertEqual(self.store.proposal(1)['result'], 'https://wiki.example/display/TCC/Design')
            self.assertTrue(any('Proposal #1 was approved and written: Confluence page Design' in m
                                for m in self.outbox()))
            notes = [i['text'] for i in self.store.pending_inputs(self.store.tickets()[0].id)]
            self.assertTrue(any('written' in n for n in notes))
            self.assertTrue(any('rejected by wei: not now' in n for n in notes))
            await self.svc.poll_once()
            self.assertEqual(len(fake.posts), 1)  # written once
