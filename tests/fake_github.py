"""An in-memory GitHub REST double, used as the `opener` of `superagent.github.GitHub`.

It serves just the endpoints the service touches: notifications, issues and their comments, PR review comments and
replies, and pulls. It records every request, so tests can assert on what was posted and with which headers.
"""

import io
import json
import re
import urllib.error
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit


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


class FakeGitHub:
    def __init__(self, repo='o/r', agent_login='bot'):
        self.repo, self.agent_login = repo, agent_login
        self.issues = {}  # number -> issue dict (PRs are issues with a 'pull_request' key)
        self.comments = {}  # number -> [comment]
        self.review_comments = {}  # number -> [review comment]
        self.pulls = {}  # number -> pull dict
        self.calls = []
        self.rate_limited = False
        self._ids = iter(range(1000, 10**9))
        self._clock = 0

    # Setup
    def _stamp(self):
        self._clock += 1
        return f'2026-11-08T12:{self._clock // 60:02d}:{self._clock % 60:02d}Z'

    def user(self, login):
        return {'login': login, 'type': 'Bot' if login.endswith('[bot]') else 'User'}

    def add_issue(self, title, body='', login='alice'):
        n = max([*self.issues, 0]) + 1
        self.issues[n] = {'number': n, 'title': title, 'body': body, 'user': self.user(login), 'state': 'open',
                          'html_url': f'https://github.com/{self.repo}/issues/{n}', 'created_at': self._stamp()}
        self.comments[n] = []
        return n

    def add_comment(self, number, body, login='alice'):
        c = {'id': next(self._ids), 'body': body, 'user': self.user(login), 'updated_at': self._stamp(),
             'html_url': f'https://github.com/{self.repo}/issues/{number}#c'}
        self.comments.setdefault(number, []).append(c)
        return c

    def merge(self, pr_number):
        self.pulls[pr_number].update(state='closed', merged=True, merged_at=self._stamp())

    def close(self, pr_number):
        self.pulls[pr_number].update(state='closed')

    # Inspection
    def posted(self, number=None):
        """Bodies the agent posted, in order."""
        out = []
        for n, cs in sorted({**self.comments, **self.review_comments}.items()):
            if number is None or n == number:
                out += [c['body'] for c in cs if c['user']['login'] == self.agent_login]
        return out

    def headers(self):
        return [c.headers for c in self.calls]

    # The opener
    def __call__(self, req):
        parts = urlsplit(req.full_url)
        method, path, query = req.get_method(), parts.path, parse_qs(parts.query)
        body = json.loads(req.data) if req.data else None
        self.calls.append(SimpleNamespace(method=method, path=path, query=query, body=body,
                                          headers=dict(req.header_items())))
        if self.rate_limited:
            raise http_error(req.full_url, 403, 'API rate limit exceeded', {'X-RateLimit-Remaining': '0'})
        out = self._route(method, path, query, body)
        if out is None:
            raise http_error(req.full_url, 404, 'Not Found')
        return Resp(out)

    def _route(self, method, path, query, body):
        base = f'/repos/{self.repo}'
        if method == 'GET' and path == '/notifications':
            return [{'repository': {'full_name': self.repo},
                     'subject': {'url': f'https://api.github.com{base}/issues/{n}'}} for n in sorted(self.issues)]
        if not path.startswith(base + '/'):
            return None
        rest = path[len(base):]
        if m := re.fullmatch(r'/issues/(\d+)', rest):
            return self.issues.get(int(m[1])) if method == 'GET' else None
        if m := re.fullmatch(r'/issues/(\d+)/comments', rest):
            n = int(m[1])
            if n not in self.issues:
                return None
            if method == 'GET':
                return list(self.comments.get(n, []))
            c = {'id': next(self._ids), 'body': body['body'], 'user': self.user(self.agent_login),
                 'updated_at': self._stamp(), 'html_url': ''}
            self.comments.setdefault(n, []).append(c)
            return c
        if m := re.fullmatch(r'/pulls/(\d+)/comments', rest):
            return list(self.review_comments.get(int(m[1]), [])) if method == 'GET' else None
        if m := re.fullmatch(r'/pulls/(\d+)/comments/(\d+)/replies', rest):
            c = {'id': next(self._ids), 'body': body['body'], 'user': self.user(self.agent_login),
                 'in_reply_to_id': int(m[2]), 'updated_at': self._stamp(), 'html_url': ''}
            self.review_comments.setdefault(int(m[1]), []).append(c)
            return c
        if rest == '/pulls' and method == 'GET':
            head = (query.get('head') or [''])[0]
            return [p for p in self.pulls.values()
                    if p['state'] == 'open' and f"{self.repo.split('/')[0]}:{p['head']}" == head]
        if rest == '/pulls' and method == 'POST':
            n = max([*self.issues, 0]) + 1
            self.issues[n] = {'number': n, 'title': body['title'], 'body': body['body'], 'state': 'open',
                              'user': self.user(self.agent_login), 'pull_request': {}, 'created_at': self._stamp(),
                              'html_url': f'https://github.com/{self.repo}/pull/{n}'}
            self.comments[n] = []
            self.pulls[n] = {'number': n, 'state': 'open', 'merged': False, 'head': body['head'],
                             'base': body['base'], 'title': body['title'], 'body': body['body']}
            return {'number': n}
        if m := re.fullmatch(r'/pulls/(\d+)', rest):
            return self.pulls.get(int(m[1])) if method == 'GET' else None
        return None
