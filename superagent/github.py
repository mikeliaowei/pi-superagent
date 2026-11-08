"""GitHub side, ported from dsh-superagent's src/sources/github.ts: polling into Triggers, and delivery.

Only this module holds the token. It never appears in argv, messages or exceptions.
"""

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .model import Trigger
from .policy import mentions

MAX_PAGES = 10
STEP_BACK = timedelta(seconds=5)  # late writes land slightly before the time we read
RETRY_FOR = timedelta(hours=24)
TIMEOUT = 30  # seconds for one API call
# Every git command the service runs: no hooks and no fsmonitor, whatever the (worker-writable) repo config says.
GIT_HARDEN = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false']
# A worker runs as the same user and could edit push.git/config, so the token-bearing push pins its network
# settings on the command line. URL-specific keys are pinned too: they win over the plain ones.
PUSH_PINS = ['-c', 'http.proxy=', '-c', 'http.sslVerify=true',
             '-c', 'http.https://github.com/.proxy=', '-c', 'http.https://github.com/.sslVerify=true']
NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException)


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
    def __init__(self, token, repo, opener=None, api='https://api.github.com'):
        self._token = token
        self.repo = repo
        self.opener = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))
        self.api = api.rstrip('/')

    def for_repo(self, repo):
        """A client for another repository with the same token, opener and API host."""
        if repo.lower() == self.repo.lower():
            return self
        return GitHub(self._token, repo, opener=self.opener, api=self.api)

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
        except NETWORK_ERRORS as e:  # HTTPError is a URLError, but is handled above
            reason = getattr(e, 'reason', None) or e or type(e).__name__
            raise GitHubError(self._redact(f'{method} {path}: {type(e).__name__}: {reason}')) from None

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


def issue_trigger(repo, issue, agent_login, qualified=False):
    """`qualified` ids name the repo (`github-issue|owner/name#<n>`): issue numbers repeat across repos."""
    n, u = issue['number'], issue.get('user') or {}
    text = f"{issue.get('title') or ''}\n\n{issue.get('body') or ''}"
    return Trigger(id=f'github-issue|{repo}#{n}' if qualified else f'github-issue|{n}', thread_key=f'github:{repo}#{n}', actor=u.get('login', ''),
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

def _notified_numbers(gh, store, repos):
    """{repo: [number]} for one notifications call, keeping only `repos` (matched case-insensitively)."""
    names = {r.lower(): r for r in repos}
    found = {r: [] for r in repos}
    for n in gh.get_all('/notifications', all='true', per_page=50, since=store.get('github-since')):
        repo = names.get((n.get('repository') or {}).get('full_name', '').lower())
        if repo is None:
            continue
        m = re.search(r'/(?:issues|pulls)/(\d+)$', (n.get('subject') or {}).get('url') or '')
        if m:
            found[repo].append(int(m.group(1)))
    return found


def _before(stamp, floor):
    """True when GitHub time `stamp` is earlier than `floor` (an ISO time); unknown stamps are not dropped."""
    try:
        return datetime.fromisoformat(stamp) < datetime.fromisoformat(floor)
    except (TypeError, ValueError):
        return False


def _read_item(gh, repo, n, cursor, agent_login, floor=None, qualified=False, old_issue_id=None):
    """`floor` is the service's first start: an item seen for the first time yields nothing older than it.

    `old_issue_id` is this issue's Trigger id from before ids named the repo; when stored, the issue was handled.
    """
    own = agent_login.lower()
    issue = gh.get(f'/repos/{repo}/issues/{n}')
    if issue is None:
        raise Gone(f'GitHub 404 on GET /repos/{repo}/issues/{n}', 404)
    is_pr = 'pull_request' in issue
    out = []
    old_issue = floor and _before(issue.get('created_at'), floor)
    if cursor is None and not old_issue and (issue.get('user') or {}).get('login', '').lower() != own \
            and not old_issue_id:
        out.append(issue_trigger(repo, issue, agent_login, qualified))
    sources = [('comment', f'/repos/{repo}/issues/{n}/comments')]
    if is_pr:
        sources.append(('review', f'/repos/{repo}/pulls/{n}/comments'))
    since = cursor or floor
    for kind, path in sources:
        for c in gh.get_all(path, since=since, per_page=100):
            if cursor is None and floor and _before(c.get('updated_at'), floor):
                continue
            if (c.get('user') or {}).get('login', '').lower() != own:
                out.append(comment_trigger(repo, n, is_pr, c, agent_login, kind))
    return out


class _Keys:
    """The kv keys and Trigger ids for one repository's polling state.

    The single-repo form keeps the original unqualified keys (`github-cursor:<n>`, `github-retry`, `github-issue|<n>`).
    With [[repos]], keys name the repo; the repo that owned the unqualified keys (`heir`) reads them while its own
    are missing, so moving a config to [[repos]] neither re-reads old comments nor reopens handled issues.
    """

    def __init__(self, store, repo, legacy, heir):
        self.store, self.repo, self.legacy, self.heir = store, repo, legacy, heir

    def cursor_key(self, n):
        return f'github-cursor:{n}' if self.legacy else f'github-cursor:{self.repo}#{n}'

    def cursor(self, n):
        c = self.store.get(self.cursor_key(n))
        if c is None and self.heir:
            c = self.store.get(f'github-cursor:{n}')
        return c

    @property
    def retry_key(self):
        return 'github-retry' if self.legacy else f'github-retry:{self.repo}'

    def retry(self):
        r = self.store.get(self.retry_key)
        if r is None and self.heir:
            r = self.store.get('github-retry')
        return dict(r or {})

    def old_issue_id(self, n):
        """The unqualified Trigger id of this repo's issue `n`, when one is stored (from before [[repos]])."""
        if self.legacy:
            return None
        old = self.store.trigger(f'github-issue|{n}')
        return old.id if old and old.thread_key.lower() == f'github:{self.repo}#{n}'.lower() else None


def _legacy_owner(store, repos):
    """The repo the unqualified kv keys belong to: the repo of the oldest stored Trigger (the single-repo era came
    first), else the only configured repo."""
    oldest = store.oldest_trigger()
    if oldest and oldest.thread_key.startswith('github:'):
        owner = oldest.thread_key[len('github:'):].rsplit('#', 1)[0]
        return next((r for r in repos if r.lower() == owner.lower()), None)
    return repos[0] if len(repos) == 1 else None


def poll(gh, store, repo, agent_login, subscribed_numbers, now=None):
    """One polling round of the single-repo form (unqualified kv keys and issue ids)."""
    return poll_repos(gh, store, [repo], agent_login, {repo: subscribed_numbers}, now, legacy=True)


def poll_repos(gh, store, repos, agent_login, subscribed, now=None, legacy=False):
    """One polling round over `repos` (owner/name list): one notifications call, then each item per repo.

    `subscribed` maps a repo to item numbers to read whatever the notifications say. Returns Triggers; storing them
    and deciding policy belong to the service. `legacy` is the single-repo form: exactly one repo, unqualified keys.
    A RateLimited error aborts the round with nothing advanced; the next round repeats it.
    The very first round only records the start time: requests written before the service ever ran are not work.
    """
    now = now or datetime.now(timezone.utc)
    if store.get('github-start') is None and store.get('github-since') is None:
        store.put('github-start', _iso(now))
        store.put('github-since', _iso(now))
        return []
    floor = store.get('github-start')
    stepped = _iso(now - STEP_BACK)
    owner = None if legacy else _legacy_owner(store, repos)
    subscribed = {k.lower(): v for k, v in (subscribed or {}).items()}
    notified = _notified_numbers(gh, store, repos)
    triggers, writes = [], []
    for repo in repos:
        keys = _Keys(store, repo, legacy, repo == owner)
        retry = keys.retry()
        numbers = sorted({*notified[repo], *map(int, subscribed.get(repo.lower(), ())), *map(int, retry)})
        cursors = {}
        for n in numbers:
            try:
                triggers.extend(_read_item(gh, repo, n, keys.cursor(n), agent_login, floor, not legacy,
                                           keys.old_issue_id(n)))
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
        writes += [(keys.cursor_key(n), c) for n, c in cursors.items()] + [(keys.retry_key, retry)]
    for k, v in writes:
        store.put(k, v)
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


def git_env(hidden=(), secrets=(), base=None):
    """The environment for git commands the service runs in repositories a worker can write.

    Worktrees share `.git/config` and hooks with the checkout, so a worker controls that config. These commands
    therefore never carry a secret: `hidden` names and any variable holding one of `secrets` are dropped, inherited
    `GIT_CONFIG_*` is dropped, and system and global config are off. Hooks are off through GIT_HARDEN in argv.
    """
    secrets = [x for x in secrets if x]
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in hidden and not k.startswith('GIT_CONFIG') and not any(x in v for x in secrets)}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    return env


def push_branch(worktree, branch, token, repo, push_dir, run=subprocess.run, env=None):
    """Push the worktree's HEAD to <branch> on GitHub.

    The worker can write its worktree's git config and hooks, so the token never runs there. HEAD is first fetched,
    without the token, into a bare repo the service creates for this push alone (next to `push_dir`, mode 0700,
    removed afterwards); the push runs from it with hooks off and an environment holding nothing but the token's
    extra HTTP header (never argv) and the basics. A long-lived push repo would not be enough: a worker running as
    the same user could plant a path-specific `http.<url>.proxy` in its config, which beats any command-line pin.
    """
    auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
    env = git_env(secrets=[token, auth]) if env is None else env

    def git(step, argv, step_env):
        r = run(argv, env=step_env, capture_output=True, text=True)
        if r.returncode != 0:
            err = r.stderr or ''
            for secret in (token, auth):
                err = err.replace(secret, '***') if secret else err
            raise GitHubError(f'git push {branch} failed ({step}): {err.strip()}')

    parent = os.path.dirname(os.path.abspath(push_dir))
    os.makedirs(parent, exist_ok=True)
    git_dir = tempfile.mkdtemp(prefix=os.path.basename(push_dir) + '.', dir=parent)  # 0700, unpredictable name
    try:
        git('init', ['git', *GIT_HARDEN, 'init', '--bare', '-q', git_dir], env)
        in_push_repo = ['git', f'--git-dir={git_dir}', *GIT_HARDEN]
        git('fetch', [*in_push_repo, 'fetch', '--no-tags', worktree, f'+HEAD:refs/heads/{branch}'], env)
        push_env = {k: os.environ[k] for k in ('PATH', 'HOME') if k in os.environ}
        push_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                        GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                        GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {auth}')
        git('push', [*in_push_repo, *PUSH_PINS, 'push', '--no-verify', f'https://github.com/{repo}.git',
                     f'+refs/heads/{branch}:refs/heads/{branch}'], push_env)
    finally:
        shutil.rmtree(git_dir, ignore_errors=True)


def has_new_commits(worktree, base_ref, run=subprocess.run, env=None):
    r = run(['git', *GIT_HARDEN, '-C', worktree, 'rev-list', '--count', f'{base_ref}..HEAD'],
            env=git_env() if env is None else env, capture_output=True, text=True)
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


def update_pull_body(gh, number, body):
    gh.patch(f'/repos/{gh.repo}/pulls/{number}', {'body': body})


def pull_state(gh, number):
    pr = gh.get(f'/repos/{gh.repo}/pulls/{number}')
    if pr is None:
        raise GitHubError(f'GitHub 404 on GET /repos/{gh.repo}/pulls/{number}', 404)
    if pr.get('merged') or pr.get('merged_at'):
        return 'merged'
    return 'closed' if pr.get('state') == 'closed' else 'open'
