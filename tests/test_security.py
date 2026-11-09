"""A worker controls its worktree's git config and hooks (shared with the checkout). None of the git commands the
service runs may hand it the GitHub token, and the push must not follow the worker's config."""

import base64
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from superagent.config import Config
from superagent.github import GitHub
from superagent.report import ReportServer
from superagent.service import Service
from superagent.store import Store
from tests.fake_github import FakeGitHub
from tests.test_service import GIT_ENV, TOKEN, Pi, git, until

AUTH = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
GITHUB_URL = 'https://github.com/o/r.git'
HOOKS = ('pre-push', 'post-checkout', 'reference-transaction', 'pre-commit', 'post-commit')


class RedirectedGit:
    """Real git for every command. A push to GitHub goes to a local bare repo standing in for it instead."""

    def __init__(self, github_url):
        self.github_url, self.pushes = github_url, []

    def __call__(self, argv, **kw):
        if 'push' in argv:
            self.pushes.append(list(argv))
            argv = [self.github_url if a == GITHUB_URL else a for a in argv]
        return subprocess.run(argv, **kw)


class GitSecretsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix='superagent-sec-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {**GIT_ENV, 'SUPERAGENT_GITHUB_TOKEN': TOKEN})
        env.start()
        self.addCleanup(env.stop)
        origin, checkout = os.path.join(self.tmp, 'origin.git'), os.path.join(self.tmp, 'checkout')
        self.github, self.evil = os.path.join(self.tmp, 'github.git'), os.path.join(self.tmp, 'evil.git')
        for bare in (origin, self.github, self.evil):
            git('init', '--bare', '-q', '-b', 'main', bare, cwd=self.tmp)
        git('clone', '-q', origin, checkout, cwd=self.tmp)
        with open(os.path.join(checkout, 'README'), 'w') as f:
            f.write('hello\n')
        git('add', 'README', cwd=checkout)
        git('commit', '-q', '-m', 'init', cwd=checkout)
        git('push', '-q', 'origin', 'HEAD:main', cwd=checkout)
        self.cfg = Config(repo='o/r', agent_login='bot', allowed=['alice'], checkout=checkout,
                          state_dir=os.path.join(self.tmp, 'state'), pi='/nonexistent/pi')
        self.store = Store(self.cfg.db_path)
        self.store.put('github-start', '2000-01-01T00:00:00+00:00')
        self.gh = FakeGitHub()
        self.dump = os.path.join(self.tmp, 'hook-env.txt')
        self.run_git = RedirectedGit(pathlib.Path(self.github).as_uri())  # file:///C:/... on Windows
        self.server = ReportServer(lambda tok: self.svc.lookup(tok), lambda tid, r: self.svc.on_report(tid, r))
        self.addCleanup(self.server.close)

    def plant(self, cwd, env):
        """What a hostile worker does: hooks that dump their environment, and a push URL rewrite."""
        hooks = os.path.join(self.tmp, 'hooks')
        os.makedirs(hooks, exist_ok=True)
        for name in HOOKS:
            path = os.path.join(hooks, name)
            with open(path, 'w') as f:
                dump = self.dump.replace(os.sep, '/')  # Git for Windows runs hooks through its own sh
                f.write(f'#!/bin/sh\necho "== {name}" >> "{dump}"\nenv >> "{dump}"\n'
                        f'[ "{name}" = pre-push ] && cat > /dev/null\nexit 0\n')
            os.chmod(path, 0o755)

        def wgit(*args):
            subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)

        wgit('config', 'core.hooksPath', hooks)
        evil, github = pathlib.Path(self.evil).as_uri(), pathlib.Path(self.github).as_uri()
        wgit('config', f'url.{evil}.insteadOf', github)
        wgit('config', f'url.{evil}.insteadOf', GITHUB_URL, '--add')
        with open(os.path.join(cwd, 'fix.txt'), 'w') as f:
            f.write('fixed\n')
        wgit('add', 'fix.txt')
        wgit('commit', '-q', '-m', 'fix')

    async def test_planted_hooks_and_url_rewrites_never_see_the_token_and_the_push_reaches_github(self):
        self.gh.add_issue('Fix A', '@bot fix A')
        pi = Pi({'before': self.plant, 'report': {'status': 'ready', 'summary': 'Fixed A.'}}, {'mode': 'hang'})
        self.svc = Service(self.cfg, self.store, GitHub(TOKEN, 'o/r', opener=self.gh), spawn=pi,
                           run_git=self.run_git, report_url=self.server.url)
        self.addAsyncCleanup(self.svc.close)
        await self.svc.poll_once()
        await self.svc.run_effects()
        await until(lambda: self.svc.tasks)
        await self.svc.tasks[next(iter(self.svc.tasks))]
        await self.svc.run_effects()  # deliver: has_new_commits, then push
        # A second Ticket makes the service add a worktree in the shared checkout, where the hooks now live.
        self.gh.add_issue('Fix B', '@bot fix B')
        await self.svc.poll_once()
        await self.svc.run_effects()
        await until(lambda: len(pi.runs) == 2 and pi.runs[1].messages)

        self.assertTrue(os.path.exists(self.dump), 'the planted hooks never ran at all')  # the worker's own commit
        with open(self.dump) as f:
            dumped = f.read()
        self.assertNotIn(TOKEN, dumped)
        self.assertNotIn(AUTH, dumped)
        self.assertEqual(len(self.run_git.pushes), 1)
        push = self.run_git.pushes[0]
        self.assertIn(GITHUB_URL, push)
        git_dir = next(a for a in push if a.startswith('--git-dir='))
        self.assertTrue(git_dir.startswith(f"--git-dir={os.path.join(self.cfg.state_dir, 'push.git')}."))  # fresh per push
        ref = 'refs/heads/agent/fix-a'
        self.assertEqual(subprocess.run(['git', '--git-dir', self.github, 'rev-parse', '--verify', '-q', ref],
                                        capture_output=True).returncode, 0, 'push did not reach GitHub')
        self.assertNotEqual(subprocess.run(['git', '--git-dir', self.evil, 'rev-parse', '--verify', '-q', ref],
                                           capture_output=True).returncode, 0, 'push followed the worker URL rewrite')
        self.assertEqual(len(self.gh.pulls), 1)


if __name__ == '__main__':
    unittest.main()
