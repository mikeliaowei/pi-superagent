import base64
import http.client
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from superagent import github as gh_mod
from superagent.github import GitHub, GitHubError, Gone, RateLimited
from superagent.store import Store

TOKEN = 'ghp_SECRETTOKEN123'
REPO = 'o/r'
API = 'https://api.github.com'
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


class Resp:
    def __init__(self, body, headers=None):
        self.body = json.dumps(body).encode() if body is not None else b''
        self.headers = headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(url, code, message='', headers=None):
    return urllib.error.HTTPError(url, code, 'err', headers or {}, io.BytesIO(json.dumps({'message': message}).encode()))


class FakeOpener:
    """Routes (METHOD, path) to a body, a Resp, an exception, or a callable(req) returning one of those."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, req):
        parts = urlsplit(req.full_url)
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append(SimpleNamespace(method=method, path=parts.path, query=parse_qs(parts.query), body=body,
                                          headers=dict(req.header_items()), url=req.full_url))
        out = self.routes.get((method, parts.path))
        if out is None:
            raise http_error(req.full_url, 404, 'Not Found')
        if callable(out):
            out = out(req)
        if isinstance(out, BaseException):
            raise out
        return out if isinstance(out, Resp) else Resp(out)

    def paths(self, method='GET'):
        return [c.path for c in self.calls if c.method == method]

    def query(self, path):
        return next(c.query for c in self.calls if c.path == path)


def user(login, kind='User'):
    return {'login': login, 'type': kind}


def issue(n, title='Fix it', body='@bot please', login='alice', pr=False):
    d = {'number': n, 'title': title, 'body': body, 'user': user(login), 'html_url': f'https://github.com/o/r/issues/{n}',
         'created_at': '2026-11-08T10:00:00Z', 'updated_at': '2026-11-08T11:00:00Z'}
    if pr:
        d['pull_request'] = {'url': '...'}
    return d


def comment(cid, body='@bot hi', login='alice', updated='2026-11-08T11:30:00Z', kind='User', **extra):
    return {'id': cid, 'body': body, 'user': user(login, kind), 'html_url': f'https://github.com/o/r#c{cid}',
            'updated_at': updated, **extra}


def notif(n, repo=REPO, kind='issues'):
    return {'repository': {'full_name': repo}, 'subject': {'url': f'{API}/repos/{repo}/{kind}/{n}'}}


def client(routes=None):
    opener = FakeOpener(routes)
    return GitHub(TOKEN, REPO, opener=opener), opener


class ClientTest(unittest.TestCase):
    def test_get_sends_github_headers(self):
        gh, op = client({('GET', '/repos/o/r'): {'ok': 1}})
        self.assertEqual(gh.get('/repos/o/r', per_page=5), {'ok': 1})
        h = op.calls[0].headers
        self.assertEqual(h['Accept'], 'application/vnd.github+json')
        self.assertEqual(h['User-agent'], 'pi-superagent')
        self.assertEqual(h['Authorization'], f'Bearer {TOKEN}')
        self.assertEqual(h['X-github-api-version'], '2022-11-28')
        self.assertEqual(op.calls[0].query, {'per_page': ['5']})

    def test_get_404_returns_none(self):
        gh, _ = client()
        self.assertIsNone(gh.get('/repos/o/r/issues/9'))

    def test_post_and_patch_send_json(self):
        gh, op = client({('POST', '/x'): {'id': 1}, ('PATCH', '/x'): {'id': 2}})
        self.assertEqual(gh.post('/x', {'a': 1}), {'id': 1})
        self.assertEqual(gh.patch('/x', {'b': 2}), {'id': 2})
        self.assertEqual([(c.method, c.body) for c in op.calls], [('POST', {'a': 1}), ('PATCH', {'b': 2})])

    def test_get_all_follows_link_next(self):
        page2 = f'{API}/items?page=2'
        gh, op = client({('GET', '/items'): lambda req: Resp([3], {}) if req.full_url == page2
                         else Resp([1, 2], {'Link': f'<{page2}>; rel="next", <{page2}>; rel="last"'})})
        self.assertEqual(gh.get_all('/items', per_page=2), [1, 2, 3])
        self.assertEqual(len(op.calls), 2)

    def test_get_all_caps_pages_and_never_follows_foreign_hosts(self):
        loop = Resp([1], {'Link': f'<{API}/items?page=2>; rel="next"'})
        gh, op = client({('GET', '/items'): lambda req: loop})
        self.assertEqual(len(gh.get_all('/items')), gh_mod.MAX_PAGES)
        gh, op = client({('GET', '/items'): Resp([1], {'Link': '<https://evil.example/items?page=2>; rel="next"'})})
        self.assertEqual(gh.get_all('/items'), [1])
        self.assertEqual(len(op.calls), 1)

    def test_rate_limit_maps_to_rate_limited(self):
        for code, headers in ((403, {'X-RateLimit-Remaining': '0'}), (429, {'Retry-After': '30'}),
                              (403, {'Retry-After': '5'})):
            gh, _ = client({('GET', '/x'): http_error(f'{API}/x', code, 'API rate limit exceeded', headers)})
            with self.assertRaises(RateLimited) as cm:
                gh.get('/x')
            self.assertEqual(cm.exception.status, code)

    def test_410_maps_to_gone(self):
        gh, _ = client({('GET', '/x'): http_error(f'{API}/x', 410, 'Issues are disabled')})
        with self.assertRaises(Gone):
            gh.get('/x')

    def test_other_errors_carry_status_path_message_but_never_the_token(self):
        gh, _ = client({('POST', '/x'): http_error(f'{API}/x', 422, f'Validation Failed for {TOKEN}')})
        with self.assertRaises(GitHubError) as cm:
            gh.post('/x', {})
        text = str(cm.exception) + repr(cm.exception) + repr(cm.exception.args)
        self.assertNotIsInstance(cm.exception, (RateLimited, Gone))
        self.assertIn('422', text)
        self.assertIn('/x', text)
        self.assertIn('Validation Failed', text)
        self.assertNotIn(TOKEN, text)

    def test_default_opener_has_a_timeout(self):
        with mock.patch('urllib.request.urlopen', return_value=Resp({'ok': 1})) as urlopen:
            self.assertEqual(GitHub(TOKEN, REPO).get('/x'), {'ok': 1})
        self.assertEqual(urlopen.call_args.kwargs.get('timeout'), gh_mod.TIMEOUT)
        self.assertEqual(gh_mod.TIMEOUT, 30)

    def test_network_errors_become_github_errors_without_the_token(self):
        for exc in (ConnectionResetError(f'reset {TOKEN}'), TimeoutError('timed out'),
                    http.client.RemoteDisconnected('closed'), urllib.error.URLError(f'no route {TOKEN}')):
            gh, _ = client({('GET', '/x'): exc})
            with self.assertRaises(GitHubError, msg=repr(exc)) as cm:
                gh.get('/x')
            self.assertNotIn(TOKEN, str(cm.exception))
            self.assertIn('GET /x', str(cm.exception))

    def test_plain_403_is_not_rate_limited(self):
        gh, _ = client({('GET', '/x'): http_error(f'{API}/x', 403, 'Resource not accessible')})
        with self.assertRaises(GitHubError) as cm:
            gh.get('/x')
        self.assertNotIsInstance(cm.exception, RateLimited)
        self.assertEqual(cm.exception.status, 403)


class TriggerTest(unittest.TestCase):
    def test_issue_trigger(self):
        t = gh_mod.issue_trigger(REPO, issue(7, title='Crash', body='@bot fix'), 'bot')
        self.assertEqual((t.id, t.kind, t.thread_key, t.actor, t.text), (
            'github-issue|7', 'issue', 'github:o/r#7', 'alice', 'Crash\n\n@bot fix'))
        self.assertTrue(t.human and t.addressed)
        self.assertEqual(t.url, 'https://github.com/o/r/issues/7')
        self.assertEqual(t.meta, {'number': 7, 'is_pr': False})

    def test_issue_trigger_with_empty_body_on_pr(self):
        t = gh_mod.issue_trigger(REPO, issue(8, body=None, pr=True), 'bot')
        self.assertEqual(t.text, 'Fix it\n\n')
        self.assertFalse(t.addressed)
        self.assertEqual(t.meta, {'number': 8, 'is_pr': True})

    def test_comment_trigger_and_edit_is_new_event(self):
        c = comment(55, body='@bot go')
        t = gh_mod.comment_trigger(REPO, 3, False, c, 'bot')
        self.assertEqual((t.id, t.kind, t.thread_key, t.text, t.at), (
            'github-comment|55|2026-11-08T11:30:00Z', 'comment', 'github:o/r#3', '@bot go', '2026-11-08T11:30:00Z'))
        self.assertEqual(t.meta, {'number': 3, 'is_pr': False, 'comment_id': 55})
        edited = gh_mod.comment_trigger(REPO, 3, False, comment(55, updated='2026-11-08T11:45:00Z'), 'bot')
        self.assertEqual(edited.id, 'github-comment|55|2026-11-08T11:45:00Z')

    def test_review_comment_trigger_points_at_thread_root(self):
        root = gh_mod.comment_trigger(REPO, 4, True, comment(70), 'bot', kind='review')
        reply = gh_mod.comment_trigger(REPO, 4, True, comment(71, in_reply_to_id=70), 'bot', kind='review')
        self.assertEqual((root.id, root.kind), ('github-review|70|2026-11-08T11:30:00Z', 'review'))
        self.assertEqual(root.meta, {'number': 4, 'is_pr': True, 'comment_id': 70, 'review_root': 70})
        self.assertEqual(reply.meta['review_root'], 70)

    def test_human_and_addressed_detection(self):
        def t(**kw):
            return gh_mod.comment_trigger(REPO, 1, False, comment(1, **kw), 'bot')
        self.assertFalse(t(login='ci[bot]').human)
        self.assertFalse(t(login='helper', kind='Bot').human)
        self.assertTrue(t(login='alice').human)
        self.assertFalse(t(body='hello @bot-two').addressed)
        self.assertTrue(t(body='hey @BOT, look').addressed)


class PollTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.put('github-start', '2026-11-08T00:00:00+00:00')  # the service started before the fixtures

    def routes(self, notifications=(), issues=None, comments=None, reviews=None):
        r = {('GET', '/notifications'): list(notifications)}
        for n, d in (issues or {}).items():
            r[('GET', f'/repos/o/r/issues/{n}')] = d
            r[('GET', f'/repos/o/r/issues/{n}/comments')] = (comments or {}).get(n, [])
            if isinstance(d, dict) and 'pull_request' in d:
                r[('GET', f'/repos/o/r/pulls/{n}/comments')] = (reviews or {}).get(n, [])
        return r

    def poll(self, routes, subscribed=(), now=NOW):
        gh, op = client(routes)
        return gh_mod.poll(gh, self.store, REPO, 'bot', subscribed, now=now), op

    def test_notifications_are_filtered_to_the_repo(self):
        out, op = self.poll(self.routes([notif(1), notif(2, repo='x/y'), notif(3, kind='pulls')],
                                        issues={1: issue(1), 3: issue(3, pr=True)}))
        self.assertEqual(sorted(t.id for t in out if t.kind == 'issue'), ['github-issue|1', 'github-issue|3'])
        self.assertNotIn('/repos/x/y/issues/2', op.paths())
        self.assertEqual(op.query('/notifications'), {'all': ['true'], 'per_page': ['50']})

    def test_issue_comments_and_pr_review_comments_become_triggers(self):
        out, _ = self.poll(self.routes([notif(3, kind='pulls')], issues={3: issue(3, pr=True)},
                                       comments={3: [comment(10)]}, reviews={3: [comment(20, in_reply_to_id=19)]}))
        self.assertEqual([t.id for t in out], ['github-issue|3', 'github-comment|10|2026-11-08T11:30:00Z',
                                               'github-review|20|2026-11-08T11:30:00Z'])
        self.assertEqual(out[2].meta['review_root'], 19)

    def test_agent_own_comments_and_issues_are_skipped(self):
        out, _ = self.poll(self.routes([notif(1), notif(2)], issues={1: issue(1), 2: issue(2, login='Bot')},
                                       comments={1: [comment(10, login='bot'), comment(11)]}))
        self.assertEqual([t.id for t in out], ['github-issue|1', 'github-comment|11|2026-11-08T11:30:00Z'])

    def test_subscribed_numbers_are_polled_without_notifications(self):
        out, op = self.poll(self.routes([], issues={5: issue(5)}, comments={5: [comment(12)]}), subscribed=[5])
        self.assertIn('github-comment|12|2026-11-08T11:30:00Z', [t.id for t in out])
        self.assertIn('/repos/o/r/issues/5/comments', op.paths())

    def test_issue_trigger_only_first_time_seen(self):
        routes = self.routes([notif(1)], issues={1: issue(1)})
        first, _ = self.poll(routes)
        second, _ = self.poll(routes, now=NOW + timedelta(minutes=1))
        self.assertEqual([t.id for t in first], ['github-issue|1'])
        self.assertEqual(second, [])

    def test_cursors_step_back_five_seconds_and_advance(self):
        routes = self.routes([notif(3, kind='pulls')], issues={3: issue(3, pr=True)})
        self.poll(routes)
        stepped = (NOW - timedelta(seconds=5)).isoformat(timespec='seconds')
        self.assertEqual(self.store.get('github-cursor:3'), stepped)
        self.assertEqual(self.store.get('github-since'), stepped)
        later = NOW + timedelta(minutes=1)
        _, op = self.poll(routes, now=later)
        self.assertEqual(op.query('/repos/o/r/issues/3/comments')['since'], [stepped])
        self.assertEqual(op.query('/repos/o/r/pulls/3/comments')['since'], [stepped])
        self.assertEqual(op.query('/notifications')['since'], [stepped])
        self.assertEqual(self.store.get('github-cursor:3'), (later - timedelta(seconds=5)).isoformat(timespec='seconds'))

    def test_failing_item_is_kept_for_retry_while_others_return(self):
        routes = self.routes([notif(1), notif(2)], issues={1: http_error('u', 500, 'boom'), 2: issue(2)})
        out, _ = self.poll(routes)
        self.assertEqual([t.id for t in out], ['github-issue|2'])
        self.assertEqual(self.store.get('github-retry'), {'1': NOW.isoformat(timespec='seconds')})
        self.assertIsNone(self.store.get('github-cursor:1'))
        self.assertIsNotNone(self.store.get('github-cursor:2'))
        # Next round: no notification for #1, but it is retried; still failing keeps the first failure time.
        out, op = self.poll(self.routes([], issues={1: http_error('u', 500, 'boom')}), now=NOW + timedelta(hours=1))
        self.assertIn('/repos/o/r/issues/1', op.paths())
        self.assertEqual(self.store.get('github-retry'), {'1': NOW.isoformat(timespec='seconds')})
        # Recovery clears it.
        out, _ = self.poll(self.routes([], issues={1: issue(1)}), now=NOW + timedelta(hours=2))
        self.assertEqual([t.id for t in out], ['github-issue|1'])
        self.assertEqual(self.store.get('github-retry'), {})

    def test_retry_dropped_after_24_hours(self):
        self.store.put('github-retry', {'1': NOW.isoformat(timespec='seconds')})
        out, _ = self.poll(self.routes([], issues={1: http_error('u', 500, 'boom')}), now=NOW + timedelta(hours=25))
        self.assertEqual(out, [])
        self.assertEqual(self.store.get('github-retry'), {})

    def test_gone_is_dropped_immediately(self):
        out, _ = self.poll(self.routes([notif(1)], issues={1: http_error('u', 410, 'gone')}))
        self.assertEqual(out, [])
        self.assertEqual(self.store.get('github-retry'), {})

    def test_rate_limit_aborts_the_round_without_advancing(self):
        routes = self.routes([notif(1)], issues={1: http_error('u', 403, 'rate', {'X-RateLimit-Remaining': '0'})})
        with self.assertRaises(RateLimited):
            self.poll(routes)
        self.assertIsNone(self.store.get('github-since'))
        self.assertIsNone(self.store.get('github-retry'))

    def test_first_run_starts_from_now_and_ignores_older_items(self):
        self.store = Store(':memory:')
        routes = self.routes([notif(1)], issues={1: issue(1)}, comments={1: [comment(10)]})
        out, op = self.poll(routes)
        self.assertEqual(out, [])
        start = NOW.isoformat(timespec='seconds')
        self.assertEqual((self.store.get('github-start'), self.store.get('github-since')), (start, start))
        self.assertNotIn('/repos/o/r/issues/1', op.paths())
        # Later: the old issue and its old comment stay ignored; only the comment written after the start counts.
        new = comment(11, updated=(NOW + timedelta(seconds=30)).isoformat().replace('+00:00', 'Z'))
        routes = self.routes([notif(1)], issues={1: issue(1)}, comments={1: [comment(10), new]})
        out, op = self.poll(routes, now=NOW + timedelta(minutes=1))
        self.assertEqual([t.id for t in out], [f"github-comment|11|{new['updated_at']}"])
        self.assertEqual(op.query('/repos/o/r/issues/1/comments')['since'], [start])

    def test_poll_does_not_store_triggers(self):
        self.poll(self.routes([notif(1)], issues={1: issue(1)}))
        self.assertFalse(self.store.has_trigger('github-issue|1'))


class PostCommentTest(unittest.TestCase):
    def test_posts_with_hidden_marker(self):
        gh, op = client({('GET', '/repos/o/r/issues/3/comments'): [], ('POST', '/repos/o/r/issues/3/comments'): {}})
        self.assertEqual(gh_mod.post_comment(gh, 3, 'Done', 'm-1'), 'sent')
        post = [c for c in op.calls if c.method == 'POST']
        self.assertEqual(post[0].body, {'body': 'Done\n\n<!-- superagent:m-1 -->'})

    def test_no_post_when_marker_already_exists(self):
        gh, op = client({('GET', '/repos/o/r/issues/3/comments'): [comment(1, body='x\n\n<!-- superagent:m-1 -->')]})
        self.assertEqual(gh_mod.post_comment(gh, 3, 'Done', 'm-1'), 'sent')
        self.assertEqual(op.paths('POST'), [])

    def test_review_reply_goes_to_thread_and_checks_review_comments(self):
        routes = {('GET', '/repos/o/r/issues/4/comments'): [], ('GET', '/repos/o/r/pulls/4/comments'): [],
                  ('POST', '/repos/o/r/pulls/4/comments/70/replies'): {}}
        gh, op = client(routes)
        self.assertEqual(gh_mod.post_comment(gh, 4, 'ok', 'm-2', review_root=70), 'sent')
        self.assertEqual(op.paths('POST'), ['/repos/o/r/pulls/4/comments/70/replies'])
        routes[('GET', '/repos/o/r/pulls/4/comments')] = [comment(71, body='<!-- superagent:m-2 -->')]
        gh, op = client(routes)
        self.assertEqual(gh_mod.post_comment(gh, 4, 'ok', 'm-2', review_root=70), 'sent')
        self.assertEqual(op.paths('POST'), [])

    def test_gone_on_410_or_locked(self):
        for err in (http_error('u', 410, 'deleted'), http_error('u', 403, 'Unable to create comment because issue is locked.')):
            gh, _ = client({('POST', '/repos/o/r/issues/3/comments'): err})
            self.assertEqual(gh_mod.post_comment(gh, 3, 'x', 'm'), 'gone')

    def test_other_errors_raise(self):
        gh, _ = client({('POST', '/repos/o/r/issues/3/comments'): http_error('u', 500, 'boom')})
        with self.assertRaises(GitHubError):
            gh_mod.post_comment(gh, 3, 'x', 'm')


class FakeRun:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return self.result


HARDEN = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
AUTH = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()


class GitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-git-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.push_dir = os.path.join(self.tmp, 'push.git')

    def test_push_goes_through_a_fresh_private_bare_repo_with_the_token_only_in_the_push_env(self):
        run = FakeRun()
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': TOKEN, 'GIT_CONFIG_COUNT': '1',
                                          'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/evil',
                                          'HTTPS_PROXY': 'http://proxy:9000', 'SYSTEMROOT': r'C:\Windows'}):
            gh_mod.push_branch('/w/t', 'agent/fix', TOKEN, REPO, self.push_dir, run=run)
        (init, init_kw), (fetch, fetch_kw), (push, push_kw) = run.calls
        git_dir = init[-1]
        # A new repo per push, next to push_dir: config a worker planted in an older push repo can never apply.
        self.assertNotEqual(git_dir, self.push_dir)
        self.assertEqual(os.path.dirname(git_dir), os.path.dirname(self.push_dir))
        self.assertFalse(os.path.exists(git_dir))  # removed after the push
        self.assertEqual(init, ['git', *HARDEN, 'init', '--bare', '-q', git_dir])
        self.assertEqual(fetch, ['git', f'--git-dir={git_dir}', *HARDEN, 'fetch', '--no-tags', '/w/t',
                                 '+HEAD:refs/heads/agent/fix'])
        self.assertEqual(push, ['git', f'--git-dir={git_dir}', *HARDEN,
                                '-c', 'http.proxy=http://proxy:9000', '-c', 'http.sslVerify=true',
                                '-c', 'http.https://github.com/.proxy=http://proxy:9000',
                                '-c', 'http.https://github.com/.sslVerify=true',
                                'push', '--no-verify',
                                'https://github.com/o/r.git', '+refs/heads/agent/fix:refs/heads/agent/fix'])
        for argv, kw in run.calls:
            self.assertNotIn(TOKEN, ' '.join(argv))
        for kw in (init_kw, fetch_kw):  # the worker-reachable steps never see the token
            self.assertFalse(any(TOKEN in v or AUTH in v for v in kw['env'].values()))
            self.assertNotIn('GIT_CONFIG_KEY_0', kw['env'])
            self.assertEqual((kw['env']['GIT_CONFIG_NOSYSTEM'], kw['env']['GIT_CONFIG_GLOBAL']), ('1', os.devnull))
        env = push_kw['env']
        self.assertEqual(env['SYSTEMROOT'], r'C:\Windows')  # Windows networking needs it
        self.assertLessEqual(set(env), {'PATH', 'HOME', 'SYSTEMROOT', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_TERMINAL_PROMPT',
                                        'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'})
        self.assertEqual(env['GIT_CONFIG_COUNT'], '1')
        self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.https://github.com/.extraheader')
        self.assertEqual(env['GIT_CONFIG_VALUE_0'], f'AUTHORIZATION: basic {AUTH}')
        self.assertEqual((env['GIT_TERMINAL_PROMPT'], env['GIT_CONFIG_NOSYSTEM'], env['GIT_CONFIG_GLOBAL']),
                         ('0', '1', os.devnull))

    def test_a_push_onto_an_existing_branch_never_forces(self):
        run = FakeRun()
        gh_mod.push_branch('/w/t', 'feature/x', TOKEN, REPO, self.push_dir, run=run, force=False)
        push = run.calls[-1][0]
        self.assertEqual(push[-1], 'refs/heads/feature/x:refs/heads/feature/x')

    def test_a_planted_push_repo_config_is_never_used(self):
        # A path-specific key such as http.https://github.com/o/r.git.proxy beats any command-line pin, so the
        # only safe push repo is one nobody else could have written.
        os.makedirs(self.push_dir)
        with open(os.path.join(self.push_dir, 'config'), 'w') as f:
            f.write('[http "https://github.com/o/r.git"]\n\tproxy = http://evil:8080\n\tsslVerify = false\n')
        run = FakeRun()
        gh_mod.push_branch('/w/t', 'agent/fix', TOKEN, REPO, self.push_dir, run=run)
        self.assertFalse(any(a in (self.push_dir, f'--git-dir={self.push_dir}') for argv, _ in run.calls for a in argv))
        self.assertEqual([next(a for a in argv if a in ('init', 'fetch', 'push')) for argv, _ in run.calls],
                         ['init', 'fetch', 'push'])

    def test_the_fresh_push_repo_is_removed_when_the_push_fails(self):
        run = FakeRun(1, stderr='fatal: denied')
        with self.assertRaises(GitHubError):
            gh_mod.push_branch('/w/t', 'agent/fix', TOKEN, REPO, self.push_dir, run=run)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_push_failure_raises_with_redacted_stderr(self):
        run = FakeRun(1, stderr=f'fatal: denied for {TOKEN} {AUTH}')
        with self.assertRaises(GitHubError) as cm:
            gh_mod.push_branch('/w/t', 'agent/fix', TOKEN, REPO, self.push_dir, run=run)
        self.assertIn('fatal: denied', str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(AUTH, str(cm.exception))

    def test_fetch_from_github_goes_through_a_fresh_bare_repo_with_the_token_only_in_its_github_fetch(self):
        run = FakeRun()
        checkout_env = {'PATH': '/usr/bin', 'GIT_CONFIG_GLOBAL': os.devnull}
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': TOKEN, 'HTTPS_PROXY': 'http://proxy:9000'}):
            gh_mod.fetch_from_github('/repo', REPO, TOKEN, 'dev', self.tmp, run=run, env=checkout_env)
        (init, init_kw), (remote, remote_kw), (local, local_kw) = run.calls
        git_dir = init[-1]
        self.assertEqual(os.path.dirname(git_dir), self.tmp)
        self.assertFalse(os.path.exists(git_dir))  # removed after the fetch
        self.assertEqual(init, ['git', *HARDEN, 'init', '--bare', '-q', git_dir])
        self.assertEqual(remote, ['git', f'--git-dir={git_dir}', *HARDEN,
                                  '-c', 'http.proxy=http://proxy:9000', '-c', 'http.sslVerify=true',
                                  '-c', 'http.https://github.com/.proxy=http://proxy:9000',
                                  '-c', 'http.https://github.com/.sslVerify=true',
                                  'fetch', '--no-tags', 'https://github.com/o/r.git', '+refs/heads/dev:refs/heads/dev'])
        self.assertEqual(remote_kw['env']['GIT_CONFIG_VALUE_0'], f'AUTHORIZATION: basic {AUTH}')
        # The checkout (worker-writable config) then fetches from the fresh repo, never with the token.
        self.assertEqual(local, ['git', *HARDEN, 'fetch', '--no-tags', git_dir, '+refs/heads/dev:refs/remotes/origin/dev'])
        self.assertEqual((local_kw['cwd'], local_kw['env']), ('/repo', checkout_env))
        for argv, _ in run.calls:
            self.assertNotIn(TOKEN, ' '.join(argv))
        self.assertFalse(any(TOKEN in v or AUTH in v for v in init_kw['env'].values()))

    def test_fetch_from_github_of_every_branch_prunes_the_checkouts_copy(self):
        run = FakeRun()
        gh_mod.fetch_from_github('/repo', REPO, TOKEN, '*', self.tmp, run=run, env={}, prune=True)
        init, remote, local = (argv for argv, _ in run.calls)
        self.assertEqual(remote[-1], '+refs/heads/*:refs/heads/*')
        self.assertEqual(local, ['git', *HARDEN, 'fetch', '--no-tags', '--prune', init[-1],
                                 '+refs/heads/*:refs/remotes/origin/*'])

    def test_fetch_from_github_failure_raises_with_redacted_stderr_and_removes_the_fresh_repo(self):
        run = FakeRun(128, stderr=f"fatal: couldn't find remote ref dev ({TOKEN} {AUTH})")
        with self.assertRaises(GitHubError) as cm:
            gh_mod.fetch_from_github('/repo', REPO, TOKEN, 'dev', self.tmp, run=run, env={})
        self.assertIn("couldn't find remote ref dev", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(AUTH, str(cm.exception))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_the_fresh_repo_is_removed_even_with_read_only_pack_files(self):
        # git writes its pack files read-only, which a plain rmtree cannot delete on Windows.
        class PackWriter(FakeRun):
            def __call__(self, argv, **kw):
                git_dir = next((a[len('--git-dir='):] for a in argv if a.startswith('--git-dir=')), None)
                if git_dir and not os.path.exists(os.path.join(git_dir, 'objects', 'pack', 'pack-1.pack')):
                    pack = os.path.join(git_dir, 'objects', 'pack', 'pack-1.pack')
                    os.makedirs(os.path.dirname(pack), exist_ok=True)
                    open(pack, 'w').close()
                    os.chmod(pack, 0o444)
                return super().__call__(argv, **kw)

        gh_mod.push_branch('/w/t', 'agent/fix', TOKEN, REPO, self.push_dir, run=PackWriter())
        gh_mod.fetch_from_github('/repo', REPO, TOKEN, 'dev', self.tmp, run=PackWriter(), env={})
        self.assertEqual(os.listdir(self.tmp), [])

    def test_has_new_commits_runs_hardened_without_secrets(self):
        run = FakeRun(stdout='2\n')
        with mock.patch.dict(os.environ, {'SUPERAGENT_GITHUB_TOKEN': TOKEN}):
            self.assertTrue(gh_mod.has_new_commits('/w/t', 'origin/main', run=run,
                                                   env=gh_mod.git_env({'SUPERAGENT_GITHUB_TOKEN'})))
        argv, kw = run.calls[0]
        self.assertEqual(argv, ['git', *HARDEN, '-C', '/w/t', 'rev-list', '--count', 'origin/main..HEAD'])
        self.assertNotIn('SUPERAGENT_GITHUB_TOKEN', kw['env'])
        self.assertFalse(gh_mod.has_new_commits('/w/t', 'origin/main', run=FakeRun(stdout='0\n')))

    def test_git_env_drops_secrets_and_inherited_git_config(self):
        base = {'PATH': '/bin', 'HOME': '/h', 'SUPERAGENT_GITHUB_TOKEN': TOKEN, 'OTHER': f'x{TOKEN}',
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/evil',
                'GIT_CONFIG_PARAMETERS': "'core.hooksPath'='/evil'"}
        env = gh_mod.git_env({'SUPERAGENT_GITHUB_TOKEN'}, [TOKEN], base=base)
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h', 'GIT_CONFIG_NOSYSTEM': '1',
                               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'})


class PullTest(unittest.TestCase):
    def test_open_pull_reuses_existing(self):
        gh, op = client({('GET', '/repos/o/r/pulls'): [{'number': 9}]})
        self.assertEqual(gh_mod.open_pull(gh, 'agent/fix', 'main', 'T', 'B'), 9)
        self.assertEqual(op.query('/repos/o/r/pulls'), {'state': ['open'], 'head': ['o:agent/fix'], 'per_page': ['100']})
        self.assertEqual(op.paths('POST'), [])

    def test_open_pull_creates_when_none(self):
        gh, op = client({('GET', '/repos/o/r/pulls'): [], ('POST', '/repos/o/r/pulls'): {'number': 11}})
        self.assertEqual(gh_mod.open_pull(gh, 'agent/fix', 'main', 'T', 'B'), 11)
        post = next(c for c in op.calls if c.method == 'POST')
        self.assertEqual(post.body, {'title': 'T', 'head': 'agent/fix', 'base': 'main', 'body': 'B'})

    def test_pull_state(self):
        for data, want in (({'state': 'closed', 'merged': True}, 'merged'), ({'state': 'closed', 'merged': False}, 'closed'),
                           ({'state': 'open', 'merged': False}, 'open')):
            gh, _ = client({('GET', '/repos/o/r/pulls/9'): data})
            self.assertEqual(gh_mod.pull_state(gh, 9), want)


if __name__ == '__main__':
    unittest.main()
