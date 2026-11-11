"""Configuration from a TOML file. Secrets are never stored here: only the names of environment variables."""

import getpass
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field

DEFAULT_TOOLS = 'read,grep,find,ls,edit,write,bash'
DEFAULT_PI = '~/.pi/agent/bin/pi'
WINDOWS = os.name == 'nt'
AGENTS = ('pi', 'copilot')


def resolve_pi(path, default, windows, which):
    """The pi executable to run, and a note for `superagent check` when it is not the configured path.

    Windows only: an extensionless path (a POSIX launcher Windows cannot start) becomes its .exe or .cmd sibling, and a
    missing default path falls back to the `pi` on PATH (npm installs pi.cmd). Elsewhere the path is used as is."""
    if not windows:
        return path, ''
    if not os.path.splitext(path)[1]:
        for ext in ('.exe', '.cmd', '.bat'):
            if os.path.isfile(path + ext):
                return path + ext, ''
    if default and not os.path.isfile(path):
        found = which('pi')
        if found:
            return found, f'found on PATH; {path} does not exist'
    return path, ''



def local_path(path):
    """A config path in this OS's form: `~` expanded and separators normalized, so Windows never mixes `/` and `\\`
    (config files use forward slashes there, since backslashes are escapes in TOML strings)."""
    return os.path.normpath(os.path.expanduser(path))

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
    pi: str = DEFAULT_PI
    agent: str = 'pi'  # the worker: 'pi', or 'copilot' (GitHub Copilot CLI)
    copilot: str = 'copilot'  # the Copilot CLI, by path or on PATH
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
        self.state_dir = local_path(self.state_dir)
        self.work_dir = local_path(self.work_dir or os.path.join(self.state_dir, 'work'))
        self.checkout = local_path(self.checkout) if self.checkout else ''
        self.pi, self.pi_note = resolve_pi(os.path.expanduser(self.pi), self.pi == DEFAULT_PI, WINDOWS,
                                           shutil.which)
        if os.sep in self.copilot or '/' in self.copilot or self.copilot.startswith('~'):
            self.copilot = local_path(self.copilot)  # a path, not a name looked up on PATH
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
        if self.agent not in AGENTS:
            raise ValueError(f"agent must be one of: {', '.join(AGENTS)}")

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
            r.checkout, r.legacy = local_path(r.checkout), False
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
    def log_path(self):
        """The service log `superagent run` writes (rotated: service.log.1, .2, .3); the panel reads it."""
        return os.path.join(self.state_dir, 'logs', 'service.log')

    @property
    def token(self):
        return os.environ.get(self.token_env, '')

    def worker_env(self, base=None):
        """The environment a Pi worker gets: everything except the configured secrets."""
        hidden = {self.token_env, *self.secret_env}
        if WINDOWS:  # environment names ignore case on Windows
            hidden = {h.upper() for h in hidden}
            return {k: v for k, v in (base if base is not None else os.environ).items() if k.upper() not in hidden}
        return {k: v for k, v in (base if base is not None else os.environ).items() if k not in hidden}


def _flat(raw):
    flat = {**raw.get('github', {}), **raw.get('policy', {}), **raw.get('executor', {}), **raw.get('service', {})}
    if 'repos' in raw:
        flat['repos'] = raw['repos']
    return flat


def load(path):
    with open(os.path.expanduser(path), 'rb') as f:
        raw = tomllib.load(f)
    return Config(**_flat(raw))


# Settings the web panel may change, and the table a missing one is added to. Secrets and paths the service
# keeps state in are not among them.
EDITABLE = {'agent': 'executor', 'provider': 'executor', 'model': 'executor', 'thinking': 'executor', 'max_workers': 'executor',
            'max_test_retries': 'executor', 'poll_seconds': 'service'}
REPO_EDITABLE = ('name', 'checkout', 'base_branch', 'test_command', 'test_timeout')
LEGACY_EDITABLE = {'checkout': 'executor', 'base_branch': 'executor'}  # the single repo + checkout form
_HEADER = re.compile(r'\s*(\[\[?)\s*([A-Za-z0-9_.-]+)\s*\]\]?\s*(#.*)?$')
FLAT_TABLES = ('github', 'policy', 'executor', 'service')  # merged into one flat Config by load()


def _toml(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f'cannot write {v!r} as a setting')
    return str(v) if isinstance(v, int) else json.dumps(v, ensure_ascii=False)  # a JSON string is a TOML string


def update(path, values=None, repos=None):
    """Change settings in the config file in place, keeping its comments and layout.

    `values`: {key: value} for top-level settings (EDITABLE, or LEGACY_EDITABLE in the single-repo form); `repos`:
    {repo name: {key: value}} for REPO_EDITABLE keys of [[repos]] tables. The edited file must load and say exactly
    what was asked, or nothing is written and ValueError says why. A running service applies it after a restart."""
    path = os.path.expanduser(path)
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    raw = tomllib.loads(text)
    values, repos = dict(values or {}), {k: dict(v) for k, v in (repos or {}).items()}
    allowed = EDITABLE | (LEGACY_EDITABLE if 'repos' not in raw else {})
    for key in values:
        if key not in allowed:
            raise ValueError(f'{key} cannot be changed here')
    names = [r.get('name', '').lower() for r in raw.get('repos', [])]
    for name, changes in repos.items():
        if name.lower() not in names:
            raise ValueError(f'repo {name} is not a [[repos]] table of the config')
        if set(changes) - set(REPO_EDITABLE):
            raise ValueError(f'{", ".join(sorted(set(changes) - set(REPO_EDITABLE)))} cannot be changed here')

    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    tables = _tables(lines)

    def put(key, value, where, header):
        """Replace `key = ...` in a table `where` accepts, else add it under `header` (a line index, or None)."""
        pattern = re.compile(rf'(\s*){re.escape(key)}\s*=')
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m and where(tables[i]) and not _HEADER.match(line):
                lines[i] = f'{m.group(1)}{key} = {_toml(value)}{nl}'
                return
        new = f'{key} = {_toml(value)}{nl}'
        if header is None:
            return new
        lines.insert(header + 1, new)
        tables.insert(header + 1, tables[header])

    flat_tables = FLAT_TABLES
    appended = {}
    for key, value in values.items():
        target = allowed[key]
        header = next((i for i, t in enumerate(tables) if t == (target, -1) and _HEADER.match(lines[i])), None)
        new = put(key, value, lambda t: t[0] in flat_tables and t[1] == -1, header)
        if new:
            appended.setdefault(target, []).append(new)
    for name, changes in repos.items():
        index = names.index(name.lower())
        header = next((i for i, t in enumerate(tables) if t == ('repos', index) and _HEADER.match(lines[i])), None)
        if header is None:
            raise ValueError(f'repo {name} is not written as a [[repos]] table: edit the config file by hand')
        for key, value in changes.items():
            put(key, value, lambda t, index=index: t == ('repos', index), header)
    edited = _joined(lines, nl) + ''.join(f'{nl}[{t}]{nl}' + ''.join(keys) for t, keys in appended.items())

    expected = _flat(raw) | values
    if repos:
        expected['repos'] = [r | next((c for n, c in repos.items() if n.lower() == r.get('name', '').lower()), {})
                             for r in raw['repos']]
    _save(path, edited, expected)


def _tables(lines):
    """Per line: the table it is in, ('', -1) before any; ('repos', i) in the i-th [[repos]]."""
    tables, table, count = [], ('', -1), -1
    for line in lines:
        m = _HEADER.match(line)
        if m:
            if m.group(1) == '[[':
                count += m.group(2) == 'repos'
                table = (m.group(2), count if m.group(2) == 'repos' else -1)
            else:
                table = (m.group(2), -1)
        tables.append(table)
    return tables


def _joined(lines, nl):
    text = ''.join(lines)
    return text + nl if text and not text.endswith(('\n', '\r')) else text


def _save(path, edited, expected):
    """Write `edited` over the config file only if it loads as exactly the `expected` flat config."""
    try:
        got = _flat(tomllib.loads(edited))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f'the edited config would not be valid TOML ({e}): edit the config file by hand') from None
    if got != expected:
        raise ValueError('the config file is laid out in a way this editor does not handle: edit it by hand')
    Config(**got)  # ValueError or TypeError for a value the service would refuse
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(edited)
    os.replace(tmp, path)


def remove_repo(path, name):
    """Delete the [[repos]] table of `name` from the config file, keeping the rest of it as it is. The last repo cannot
    go. Nothing is written unless the result loads as exactly that; ValueError says why."""
    path = os.path.expanduser(path)
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    raw = tomllib.loads(text)
    names = [r.get('name', '').lower() for r in raw.get('repos', [])]
    if name.lower() not in names:
        raise ValueError(f'repo {name} is not a [[repos]] table of the config')
    if len(names) == 1:
        raise ValueError(f'{name} is the only repo: add another one before removing it')
    index = names.index(name.lower())
    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    tables = _tables(lines)
    lines = [line for line, t in zip(lines, tables, strict=True) if t != ('repos', index)]  # header to the next table
    expected = _flat(raw)
    expected['repos'] = [r for i, r in enumerate(raw['repos']) if i != index]
    _save(path, _joined(lines, nl), expected)


def add_repo(path, name, checkout, base_branch='', test_command='', test_timeout=0):
    """Add a repository to the config file as a new [[repos]] table, keeping the file's comments and layout.

    A config in the single-repo form (`repo` + `checkout`) first gets its repo moved to a [[repos]] table too; the
    service carries that repo's cursors, Triggers and Tickets over (see github._Keys). Empty `base_branch` or
    `test_command` and a 0 `test_timeout` keep the defaults. Nothing is written unless the result loads as exactly
    that; ValueError says why. A running service applies it after a restart."""
    path = os.path.expanduser(path)
    with open(path, encoding='utf-8', newline='') as f:
        text = f.read()
    raw = tomllib.loads(text)
    new = {'name': name.strip(), 'checkout': checkout.strip()}
    for key, value in (('base_branch', base_branch.strip()), ('test_command', test_command.strip()),
                       ('test_timeout', test_timeout)):
        if value:
            new[key] = value
    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    expected = _flat(raw)
    if 'repos' in raw:
        add = [new]
        expected['repos'] = [*raw['repos'], new]
    else:
        tables = _tables(lines)
        moved = {k: expected.pop(k, '') for k in ('repo', 'checkout')}
        for key in moved:  # drop the single-repo form's lines; the [[repos]] table below replaces them
            pattern = re.compile(rf'\s*{key}\s*=')
            for i in reversed(range(len(lines))):
                if pattern.match(lines[i]) and tables[i][0] in FLAT_TABLES and tables[i][1] == -1:
                    del lines[i], tables[i]
        add = [{'name': moved['repo'], 'checkout': moved['checkout']}, new]
        expected['repos'] = add
    blocks = ''.join(f'{nl}[[repos]]{nl}' + ''.join(f'{k} = {_toml(v)}{nl}' for k, v in r.items()) for r in add)
    _save(path, _joined(lines, nl) + blocks, expected)
