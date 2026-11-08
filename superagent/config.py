"""Configuration from a TOML file. Secrets are never stored here: only the names of environment variables."""

import getpass
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_TOOLS = 'read,grep,find,ls,edit,write,bash'


@dataclass
class RepoConfig:
    """One repository the service works in."""
    name: str  # owner/name
    checkout: str  # local clone the worktrees are made from
    base_branch: str = 'main'
    test_command: str = ''  # run in the worktree before any pull request; empty: none
    test_timeout: int = 900  # seconds
    # The single `repo` + `checkout` form: worktrees in <work_dir>/<ticket>, <state_dir>/push.git and the
    # unqualified kv keys and issue Trigger ids of the versions before [[repos]].
    legacy: bool = False

    @property
    def slug(self):
        return self.name.replace('/', '__')


@dataclass
class Config:
    agent_login: str
    allowed: list
    repo: str = ''  # owner/name: the single-repo form, with `checkout`
    checkout: str = ''  # local clone the worktrees are made from
    token_env: str = 'SUPERAGENT_GITHUB_TOKEN'
    poll_seconds: int = 60
    state_dir: str = '~/.local/share/pi-superagent'
    work_dir: str = ''  # default: <state_dir>/work
    pi: str = '~/.pi/agent/bin/pi'
    provider: str = 'anthropic'
    model: str = 'claude-sonnet-5-5'
    thinking: str = 'medium'
    tools: str = DEFAULT_TOOLS
    max_workers: int = 2
    report_port: int = 8741
    base_branch: str = 'main'
    # Extra environment variables never passed to workers, on top of token_env.
    secret_env: list = field(default_factory=lambda: ['GITHUB_TOKEN', 'GH_TOKEN'])
    # [[repos]] tables (dicts or RepoConfig); None for the single-repo form. Normalized to list[RepoConfig].
    repos: list | None = None
    # Failed fix attempts after a failing test_command before the Ticket is reported blocked.
    max_test_retries: int = 2
    # Who `superagent ask` and `answer` act as (S15): the local operator. Default: the OS user name.
    local_user: str = ''

    def __post_init__(self):
        if not self.local_user:
            try:
                self.local_user = getpass.getuser()
            except (OSError, KeyError):
                self.local_user = 'local'
        self.state_dir = os.path.expanduser(self.state_dir)
        self.work_dir = os.path.expanduser(self.work_dir or os.path.join(self.state_dir, 'work'))
        self.checkout = os.path.expanduser(self.checkout) if self.checkout else ''
        self.pi = os.path.expanduser(self.pi)
        if self.repos is None:
            if '/' not in self.repo:
                raise ValueError('repo must be owner/name')
            if not self.checkout:
                raise ValueError('checkout is required: the local clone of repo')
            self.repos = [RepoConfig(self.repo, self.checkout, self.base_branch, legacy=True)]
        else:
            self.repos = self._repo_list(self.repos)
        if not 60 <= self.poll_seconds <= 3600:
            raise ValueError('poll_seconds must be between 60 and 3600')
        if not 1 <= self.max_workers <= 16:
            raise ValueError('max_workers must be between 1 and 16')

    def _repo_list(self, tables):
        if self.repo or self.checkout:
            raise ValueError('use either [github] repo with [executor] checkout, or [[repos]] tables, not both')
        if not tables:
            raise ValueError('[[repos]] is empty: add at least one repository')
        repos, seen = [], set()
        for r in tables:
            r = r if isinstance(r, RepoConfig) else RepoConfig(**{'base_branch': self.base_branch, **r})
            if '/' not in r.name:
                raise ValueError(f'repo must be owner/name: {r.name}')
            if r.name.lower() in seen:
                raise ValueError(f'duplicate repo in [[repos]]: {r.name}')
            if not r.checkout:
                raise ValueError(f'repo {r.name} needs a checkout')
            seen.add(r.name.lower())
            r.checkout, r.legacy = os.path.expanduser(r.checkout), False
            repos.append(r)
        return repos

    @property
    def legacy(self):
        """True for the single `repo` + `checkout` form."""
        return self.repos[0].legacy

    @property
    def repo_names(self):
        return [r.name for r in self.repos]

    def repo_config(self, name):
        for r in self.repos:
            if r.name.lower() == (name or '').lower():
                return r
        raise ValueError(f'repo {name} is not configured')

    def worktree_root(self, repo):
        """Where new worktrees of `repo` (a RepoConfig) live."""
        return self.work_dir if repo.legacy else os.path.join(self.work_dir, repo.slug)

    def push_dir(self, repo):
        """The service-private bare repo `repo` (a RepoConfig) is pushed from."""
        return (os.path.join(self.state_dir, 'push.git') if repo.legacy
                else os.path.join(self.state_dir, 'push', f'{repo.slug}.git'))

    @property
    def db_path(self):
        return os.path.join(self.state_dir, 'superagent.sqlite')

    @property
    def token(self):
        return os.environ.get(self.token_env, '')

    def worker_env(self, base=None):
        """The environment a Pi worker gets: everything except the configured secrets."""
        hidden = {self.token_env, *self.secret_env}
        return {k: v for k, v in (base if base is not None else os.environ).items() if k not in hidden}


def load(path):
    with open(os.path.expanduser(path), 'rb') as f:
        raw = tomllib.load(f)
    flat = {**raw.get('github', {}), **raw.get('policy', {}), **raw.get('executor', {}), **raw.get('service', {})}
    if 'repos' in raw:
        flat['repos'] = raw['repos']
    return Config(**flat)
