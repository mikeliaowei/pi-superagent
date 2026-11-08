"""GitHub side, ported from dsh-superagent's src/sources/github.ts: polling into Triggers, and delivery.

Only this module holds the token. It never appears in argv, messages or exceptions.
"""

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .model import Trigger
from .policy import mentions

MAX_PAGES = 10
STEP_BACK = timedelta(seconds=5)  # late writes land slightly before the time we read
RETRY_FOR = timedelta(hours=24)


class GitHubError(Exception):
    def __init__(self, message, status=None, gh_message=''):
        super().__init__(message)
        self.status = status
        self.gh_message = gh_message


class RateLimited(GitHubError):
    pass


class Gone(GitHubError):
    pass


class GitHub:
    def __init__(self, token, repo, opener=urllib.request.urlopen, api='https://api.github.com'):
        self._token = token
        self.repo = repo
        self.opener = opener
        self.api = api.rstrip('/')

    def get(self, path, **params):
        return self._request('GET', path, params=params)[0]

    def get_all(self, path, **params):
        items, url = [], self._url(path, params)
        for _ in range(MAX_PAGES):
            data, headers = self._request('GET', path, url=url)
            items.extend(data or [])
            url = _next_link(headers)
            # Never send the token anywhere but the API host.
            if not url or not url.startswith(self.api + '/'):
                break
        return items

    def post(self, path, body):
        return self._request('POST', path, body=body)[0]

    def patch(self, path, body):
        return self._request('PATCH', path, body=body)[0]

    def _url(self, path, params=None):
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        return self.api + path + ('?' + query if query else '')

    def _request(self, method, path, body=None, params=None, url=None):
        req = urllib.request.Request(url or self._url(path, params), method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('User-Agent', 'pi-superagent')
        req.add_header('Authorization', f'Bearer {self._token}')
        req.add_header('X-GitHub-Api-Version', '2022-11-28')
        if body is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with self.opener(req) as resp:
                raw = resp.read()
                return (json.loads(raw) if raw else None), resp.headers
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == 'GET':
                return None, {}
            raise self._error(method, path, e) from None
        except urllib.error.URLError as e:
            raise GitHubError(self._redact(f'{method} {path}: {e.reason}')) from None

    def _error(self, method, path, e):
        try:
            gh_message = str(json.loads(e.read() or b'{}').get('message', ''))
        except (ValueError, AttributeError, OSError):
            gh_message = ''
        gh_message = self._redact(gh_message)
        headers = e.headers or {}
        text = f'GitHub {e.code} on {method} {path}: {gh_message}'
        if e.code == 410:
            return Gone(text, e.code, gh_message)
        if e.code in (403, 429) and (headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After')):
            return RateLimited(text, e.code, gh_message)
        return GitHubError(text, e.code, gh_message)

    def _redact(self, text):
        return text.replace(self._token, '***') if self._token else text


def _next_link(headers):
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', (headers or {}).get('Link') or '')
    return m.group(1) if m else None


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


# Triggers

def _is_human(u):
    login = (u or {}).get('login', '')
    return (u or {}).get('type') == 'User' and not login.endswith('[bot]')


def issue_trigger(repo, issue, agent_login):
    n, u = issue['number'], issue.get('user') or {}
    text = f"{issue.get('title') or ''}\n\n{issue.get('body') or ''}"
    return Trigger(id=f'github-issue|{n}', thread_key=f'github:{repo}#{n}', actor=u.get('login', ''),
                   human=_is_human(u), addressed=mentions(text, agent_login), at=issue.get('created_at', ''),
                   text=text, url=issue.get('html_url', ''), kind='issue',
                   meta={'number': n, 'is_pr': 'pull_request' in issue})


def comment_trigger(repo, number, is_pr, c, agent_login, kind='comment'):
    """kind 'comment' for conversation comments, 'review' for inline PR review comments."""
    u, text = c.get('user') or {}, c.get('body') or ''
    meta = {'number': number, 'is_pr': is_pr, 'comment_id': c['id']}
    if kind == 'review':
        meta['review_root'] = c.get('in_reply_to_id') or c['id']
    prefix = 'github-review' if kind == 'review' else 'github-comment'
    return Trigger(id=f"{prefix}|{c['id']}|{c.get('updated_at', '')}", thread_key=f'github:{repo}#{number}',
                   actor=u.get('login', ''), human=_is_human(u), addressed=mentions(text, agent_login),
                   at=c.get('updated_at', ''), text=text, url=c.get('html_url', ''), kind=kind, meta=meta)


# Polling

def _notified_numbers(gh, store, repo):
    found = []
    for n in gh.get_all('/notifications', all='true', per_page=50, since=store.get('github-since')):
        if (n.get('repository') or {}).get('full_name', '').lower() != repo.lower():
            continue
        m = re.search(r'/(?:issues|pulls)/(\d+)$', (n.get('subject') or {}).get('url') or '')
        if m:
            found.append(int(m.group(1)))
    return found


def _read_item(gh, repo, n, cursor, agent_login):
    own = agent_login.lower()
    issue = gh.get(f'/repos/{repo}/issues/{n}')
    if issue is None:
        raise Gone(f'GitHub 404 on GET /repos/{repo}/issues/{n}', 404)
    is_pr = 'pull_request' in issue
    out = []
    if cursor is None and (issue.get('user') or {}).get('login', '').lower() != own:
        out.append(issue_trigger(repo, issue, agent_login))
    sources = [('comment', f'/repos/{repo}/issues/{n}/comments')]
    if is_pr:
        sources.append(('review', f'/repos/{repo}/pulls/{n}/comments'))
    for kind, path in sources:
        for c in gh.get_all(path, since=cursor, per_page=100):
            if (c.get('user') or {}).get('login', '').lower() != own:
                out.append(comment_trigger(repo, n, is_pr, c, agent_login, kind))
    return out


def poll(gh, store, repo, agent_login, subscribed_numbers, now=None):
    """One polling round. Returns Triggers; storing them and deciding policy belong to the service.

    A RateLimited error aborts the round with nothing advanced; the next round repeats it.
    """
    now = now or datetime.now(timezone.utc)
    stepped = _iso(now - STEP_BACK)
    retry = dict(store.get('github-retry') or {})
    numbers = sorted({*_notified_numbers(gh, store, repo), *map(int, subscribed_numbers), *map(int, retry)})
    triggers, cursors = [], {}
    for n in numbers:
        try:
            triggers.extend(_read_item(gh, repo, n, store.get(f'github-cursor:{n}'), agent_login))
        except RateLimited:
            raise
        except Gone:
            retry.pop(str(n), None)
            continue
        except GitHubError:
            first = retry.setdefault(str(n), _iso(now))
            if now - datetime.fromisoformat(first) > RETRY_FOR:
                retry.pop(str(n))
            continue
        retry.pop(str(n), None)
        cursors[n] = stepped
    for n, c in cursors.items():
        store.put(f'github-cursor:{n}', c)
    store.put('github-retry', retry)
    store.put('github-since', stepped)
    return triggers


# Delivery

def _marker(msg_id):
    return f'<!-- superagent:{msg_id} -->'


def post_comment(gh, number, body, msg_id, review_root=None):
    """Post once per msg_id: a retry finds the marker and stops. Returns 'sent' or 'gone' (deleted or locked)."""
    marker, repo = _marker(msg_id), gh.repo
    existing = gh.get_all(f'/repos/{repo}/issues/{number}/comments', per_page=100)
    if review_root:
        existing += gh.get_all(f'/repos/{repo}/pulls/{number}/comments', per_page=100)
    if any(marker in (c.get('body') or '') for c in existing):
        return 'sent'
    path = (f'/repos/{repo}/pulls/{number}/comments/{review_root}/replies' if review_root
            else f'/repos/{repo}/issues/{number}/comments')
    try:
        gh.post(path, {'body': f'{body}\n\n{marker}'})
    except Gone:
        return 'gone'
    except GitHubError as e:
        if e.status == 403 and 'locked' in e.gh_message.lower():
            return 'gone'
        raise
    return 'sent'


def push_branch(worktree, branch, token, repo, run=subprocess.run):
    """Push HEAD to <branch>. The token travels only in the environment, as an extra HTTP header."""
    auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '1',
           'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
           'GIT_CONFIG_VALUE_0': f'AUTHORIZATION: basic {auth}'}
    argv = ['git', '-C', worktree, 'push', f'https://github.com/{repo}.git', f'HEAD:refs/heads/{branch}']
    r = run(argv, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or '').replace(token, '***').replace(auth, '***') if token else (r.stderr or '')
        raise GitHubError(f'git push {branch} failed: {err.strip()}')


def has_new_commits(worktree, base_ref, run=subprocess.run):
    r = run(['git', '-C', worktree, 'rev-list', '--count', f'{base_ref}..HEAD'], capture_output=True, text=True)
    if r.returncode != 0:
        raise GitHubError(f'git rev-list {base_ref}..HEAD failed: {(r.stderr or "").strip()}')
    return int(r.stdout.strip() or 0) > 0


def open_pull(gh, branch, base, title, body):
    """Reuse the open PR for this branch, else create one. Returns its number."""
    repo, owner = gh.repo, gh.repo.split('/')[0]

    def existing():
        found = gh.get_all(f'/repos/{repo}/pulls', state='open', head=f'{owner}:{branch}', per_page=100)
        return found[0]['number'] if found else None

    number = existing()
    if number:
        return number
    try:
        return gh.post(f'/repos/{repo}/pulls', {'title': title, 'head': branch, 'base': base, 'body': body})['number']
    except GitHubError as e:
        # A concurrent or earlier create won the race.
        if e.status == 422 and 'already exists' in e.gh_message.lower() and (number := existing()):
            return number
        raise


def pull_state(gh, number):
    pr = gh.get(f'/repos/{gh.repo}/pulls/{number}')
    if pr is None:
        raise GitHubError(f'GitHub 404 on GET /repos/{gh.repo}/pulls/{number}', 404)
    if pr.get('merged') or pr.get('merged_at'):
        return 'merged'
    return 'closed' if pr.get('state') == 'closed' else 'open'
