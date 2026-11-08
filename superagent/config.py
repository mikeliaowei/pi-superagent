"""Configuration from a TOML file. Secrets are never stored here: only the names of environment variables."""

import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_TOOLS = 'read,grep,find,ls,edit,write,bash'


@dataclass
class Config:
    repo: str  # owner/name
    agent_login: str
    allowed: list
    checkout: str  # local clone the worktrees are made from
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

    def __post_init__(self):
        self.state_dir = os.path.expanduser(self.state_dir)
        self.work_dir = os.path.expanduser(self.work_dir or os.path.join(self.state_dir, 'work'))
        self.checkout = os.path.expanduser(self.checkout)
        self.pi = os.path.expanduser(self.pi)
        if '/' not in self.repo:
            raise ValueError('repo must be owner/name')
        if not 60 <= self.poll_seconds <= 3600:
            raise ValueError('poll_seconds must be between 60 and 3600')
        if not 1 <= self.max_workers <= 16:
            raise ValueError('max_workers must be between 1 and 16')

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
    return Config(**flat)
