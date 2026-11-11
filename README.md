# pi-superagent

[![tests](https://github.com/mikeliaowei/pi-superagent/actions/workflows/tests.yml/badge.svg)](https://github.com/mikeliaowei/pi-superagent/actions/workflows/tests.yml)

A coding agent for one or more GitHub repositories. Mention its bot account on an issue or pull request, or ask it
from your terminal, a [Pi](https://github.com/earendil-works/pi) chat or DSH, and it opens a Ticket, runs a Pi
worker in its own git worktree, runs your tests, asks you when it needs a decision, and delivers a pull request.

> **Status: early.** It works end to end, but workers run as your OS user with no container isolation. Point it at
> test repositories you do not mind it touching, and read the [Safety model](#safety-model) first.

This is a Python port of the design of DeepSeek's
[dsh-superagent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) (MIT), moved from the DSH
harness onto Pi's RPC mode. The domain (Triggers, Tickets, Assignments, Reports, stages), the explicit-mandate
policy, the effect queue and the "confirm once, then stay quiet" reply style come from that project.

## How it works

```
 GitHub (one repo)                    superagent run                                Pi workers
 ─────────────────                    ──────────────                                ──────────
 issues, comments,   ── poll ──▶  Triggers ─▶ policy gate ─▶ Ticket ─▶ launch ──▶  pi --mode rpc
 review comments      (60 s)       (deduped)   accept/stop/            (worktree     in <work_dir>/<name>
                                               context/ignore          agent/<name>)       │
                                                                                           │ superagent-report
 replies, PRs     ◀── effect queue (reply, launch, deliver) ◀── handle_report ◀── localhost:8741
                       SQLite, retried with backoff
```

- **Triggers.** Each poll reads notifications plus the issues the service already follows. Every issue, comment
  and review comment becomes a Trigger with a deterministic id, so seeing it twice does nothing. The very first
  poll only records the start time (kv `github-start` and `github-since`): issues and comments written before the
  service first ran never become work, even when a later comment brings their thread back.
- **Policy.** A login on `allowed` who mentions `@<agent_login>` opens a Ticket (an explicit mandate).
  `@<agent_login> stop` from an allowed login stops the Ticket on that thread. Any other human comment on a
  thread with a live Ticket reaches its worker as context, never as new authority. Bots and the agent itself are
  ignored. Quoted lines (`> ...`) never count as a mention or a stop. Editing a comment that already opened or
  stopped a Ticket does not do it again: the edit is context for a live Ticket on that thread, otherwise ignored.
- **Workers.** One Pi process per running Assignment, with the Ticket's id as its session id, so a later launch
  resumes the same conversation. The worker reports with `superagent-report --status
  progress|waiting|ready|blocked|failed|dropped`.
- **Replies.** The agent acknowledges once. After that it speaks only to ask a question (`waiting`), link a pull
  request (`ready`), or say honestly that it is blocked, failed, or dropped the request. `progress` posts nothing.
- **Tests.** When the Ticket's repo has a `test_command`, a `ready` report first runs it in the worktree
  (`sh -c`, or `cmd.exe /c` on Windows, with the worker's environment: no token, no `secret_env`, no git credential helpers; its own process
  group; killed after `test_timeout`). A failure opens no PR: the command, its exit code (or the timeout) and the
  last 3,000 characters of output go back to the same Pi session, which fixes and reports ready again. After
  `max_test_retries` failed fixes the Ticket is reported blocked ("tests still failing: <command>").
- **Delivery.** On `ready` (and passing tests), the service (not the worker) pushes `agent/<name>` with its token
  (or, for a request on an existing branch, that branch, never with force)
  and opens or reuses a PR. For every push it creates a new private bare repo next to `<state_dir>/push.git`
  (single-repo form) or `<state_dir>/push/<owner>__<name>.git` (`[[repos]]`), fetches the worktree's HEAD into
  it, pushes from it, and deletes it, so nothing a worker wrote earlier can change how the token is sent. The
  body has a Summary (the worker's report), Details (the description the worker passes with
  `superagent-report --details-file`, when it does), Commits (the branch's commit subjects) and Tests
  (``Tests: `<command>` passed in <N>s`` or `Tests: no test command configured for <repo>.`) section, names the
  worker's model and thinking level, and says `Closes #<n>` when the request came from an issue. No commits means a
  "no changes" reply
  and no PR. The Ticket ends when the PR is merged or closed, or when someone stops it.
- **Cross-repo Tickets.** With `[[repos]]`, a request that names other configured repos as `owner/name` (not in
  quoted lines; unconfigured repos are ignored) gets one Ticket for all of them. Its ticket folder
  `<work_dir>/tickets/<ticket>/` holds one worktree per repo (`<owner>__<name>`, each on `agent/<ticket>` from
  that repo's base branch); the worker runs in the folder and its first message lists each repo, path, base
  branch and test command. On `ready`, every repo with new commits runs its `test_command`; any failure opens no
  PR at all and sends the failing repos' output back to the same session (retries count per Ticket). When all
  pass, each changed repo is pushed from its own push repo and gets its own PR; each PR body lists the others
  under "Part of a cross-repo change:", and one reply on the request thread lists every PR (repos without
  commits as "no changes"). Only the request's own repo PR says `Closes #<n>`. The Ticket ends when none of its
  PRs is open: `merged` if all merged, else `closed`. Stop and retry act on the whole Ticket.
- **Durability.** Every side effect is written to the SQLite effect queue before it runs, retried with backoff
  (5 s doubling to 300 s), and replies carry a hidden marker so a retry never posts twice. After a restart,
  running Assignments end as "host restarted" and relaunch with a recovery note.

## Install

You need:

- Python 3.11 or newer (the service uses only the standard library; the web panel needs `streamlit`).
- git, and a worker agent, either one (`[executor] agent`, also on the panel's Settings tab):
  - the [Pi coding agent](https://github.com/earendil-works/pi) signed in to at least one model provider
    (`pi auth check --provider <name>`): `agent = "pi"`, the default; or
  - [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/use-copilot-agents/use-copilot-cli)
    (`npm install -g @github/copilot`, then `copilot login`): `agent = "copilot"`. It runs as a server over the Agent
    Client Protocol (`copilot --acp`, in public preview) with your Copilot license, the config's `model` as Copilot
    names it (for example `claude-sonnet-5.5`) and its `thinking` as the reasoning effort. `superagent check`
    checks whichever is chosen.
- A GitHub account the agent can act as, with a token that can push to your repositories (see Setup).

```sh
git clone https://github.com/mikeliaowei/pi-superagent.git
cd pi-superagent
python3 -m venv .venv
.venv/bin/pip install -e .              # add ".[panel]" for the web panel
```

This installs `superagent` (the service and operator CLI) and `superagent-report` (what workers call) into the
venv. Workers find `superagent-report` because the service puts its own `bin` directory first on their `PATH`.

On Windows, see [Windows (native, no WSL)](#windows-native-no-wsl): the same features, installed with `py` and
run from cmd or PowerShell.

## Windows (native, no WSL)

`superagent` runs natively on Windows 10/11: no WSL, no Ubuntu. Each command below says whether it is for
**cmd** or **PowerShell**; where both are shown, use either.

**Prerequisites**

- Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/) (keep the `py` launcher
  ticked), or `winget install Python.Python.3.13`.
- [Git for Windows](https://git-scm.com/download/win).
- Pi installed and signed in, following Pi's own instructions. Check a provider, for example:
  `pi auth check --provider github-copilot --json`.
- Find the Pi executable with `where pi` (cmd) or `where.exe pi` (PowerShell), for example
  `C:\Users\me\AppData\Roaming\npm\pi.cmd`. If `~/.pi/agent/bin/pi` does not exist, `superagent` uses the `pi` on
  `PATH` and `superagent check` says which one. To pin it, set `pi` in the config's `[executor]` with forward slashes
  or a TOML literal string (single quotes keep backslashes):

  ```toml
  pi = 'C:\Users\me\AppData\Roaming\npm\pi.cmd'
  # or: pi = "C:/Users/me/AppData/Roaming/npm/pi.cmd"
  ```

**Install** (cmd and PowerShell alike)

```bat
git clone https://github.com/mikeliaowei/pi-superagent.git
cd pi-superagent
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[panel]"
```

**Config.** It lives in `%USERPROFILE%\.config\pi-superagent\config.toml`; the database, Pi sessions and worktrees
go to `%USERPROFILE%\.local\share\pi-superagent` unless you set `state_dir`.

cmd:

```bat
mkdir "%USERPROFILE%\.config\pi-superagent"
copy config.example.toml "%USERPROFILE%\.config\pi-superagent\config.toml"
notepad "%USERPROFILE%\.config\pi-superagent\config.toml"
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\pi-superagent"
Copy-Item config.example.toml "$env:USERPROFILE\.config\pi-superagent\config.toml"
notepad "$env:USERPROFILE\.config\pi-superagent\config.toml"
```

Write Windows paths in the config with forward slashes (`checkout = "C:/src/your-repo"`) or as TOML literal strings
in single quotes (`checkout = 'C:\src\your-repo'`); in a double-quoted TOML string a backslash is an escape.

**Token.** Never in the config file.

- For every new terminal (cmd or PowerShell): `setx SUPERAGENT_GITHUB_TOKEN github_pat_...`, then open a **new**
  terminal; `setx` does not change the one you typed it in. It is stored in your user environment.
- For the current PowerShell only: `$env:SUPERAGENT_GITHUB_TOKEN = 'github_pat_...'`
- For the current cmd only: `set SUPERAGENT_GITHUB_TOKEN=github_pat_...`

**Check and run** (cmd and PowerShell alike)

```bat
.venv\Scripts\superagent check
.venv\Scripts\superagent run
.venv\Scripts\superagent ask --repo owner/name "add a CONTRIBUTING.md"
.venv\Scripts\superagent panel
```

`superagent run` stops cleanly on Ctrl+C (or Ctrl+Break): workers are closed and relaunch with a recovery note at
the next start. The panel is at http://127.0.0.1:8742; on Windows `superagent panel` runs Streamlit as a child
process and Ctrl+C stops both.

**Tests run through cmd.exe.** A repo's `test_command` runs as `cmd.exe /c <command>` in the worktree, in its own
process group; on timeout the whole process tree is killed (`taskkill /F /T`). Write it in cmd syntax (`&&`
between steps, `\` in paths), for example:

```toml
test_command = '.venv\Scripts\python -m pytest -q'
# or the repo's own command, e.g. "npm test" or "gradlew.bat test"
```

**Pi skill.** `superagent pi-install` writes to `%USERPROFILE%\.pi\agent` by default (or `--agent-dir`, or
`PI_CODING_AGENT_DIR`). The commands it writes use `superagent.exe` with forward slashes.

**Long paths.** Worktrees nest deep (`<state_dir>\work\<owner>__<name>\<ticket>\...`), and Windows' 260-character
limit can break checkouts. Turn on long paths for git, in your global config (used by the workers' own git) and in
each checkout (the service's own git commands ignore global config):

```bat
git config --global core.longpaths true
git -C C:/src/your-repo config core.longpaths true
```

Also turn on Windows long paths once (PowerShell as administrator), or keep `state_dir` short (`state_dir = "C:/sa"`):

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

**What differs from macOS/Linux, and limits**

- Workers' git cannot use stored credentials or prompt: `credential.helper=` clears Git Credential Manager,
  `GCM_INTERACTIVE=never`, `GIT_TERMINAL_PROMPT=0`, and `GIT_ASKPASS`/`SSH_ASKPASS` point to
  `<state_dir>\deny-askpass.cmd`, which fails without output (Windows has no `/usr/bin/false`; leaving askpass unset
  would let an inherited one, such as VS Code's, answer instead).
- The service's own git commands turn hooks off with `core.hooksPath=C:/Windows/System32/cmd.exe` (a file, so no
  hook can exist below it) instead of `/dev/null`: Git for Windows would look `/dev/null/<hook>` up as
  `C:\dev\null\<hook>`, a folder any user can create.
- A Copilot worker (`agent = "copilot"`): npm installs `copilot.cmd`, which is found on `PATH`; to pin it, set
  `copilot = "C:/Users/me/AppData/Roaming/npm/copilot.cmd"` in `[executor]`. It gets the same environment as a Pi
  worker (no token, no `secret_env`, case-insensitively, as Windows treats variable names).
- Behind an HTTPS proxy, set it for the service the same way as the token, for example
  `setx HTTPS_PROXY http://proxy.example:8080` (see [Safety model](#safety-model)).
- A test command's background processes, and the processes a worker's tools started, are killed through the
  process tree when the command ends or the worker stops. One whose parent already exited is no longer in that tree and
  can survive.
- There is no SIGTERM: stopping `superagent run` from Task Manager ends it at once; the next start recovers the
  running Assignments as after any restart.
- Verified on GitHub's `windows-latest` runners (the test suite), not yet on a real company laptop. Workers still run
  as your user without isolation, as on every platform (see [Safety model](#safety-model)). Antivirus scanning can
  make git and worktree creation noticeably slower; excluding `state_dir` from scanning helps if policy allows it.

## Quick start

```sh
mkdir -p ~/.config/pi-superagent && cp config.example.toml ~/.config/pi-superagent/config.toml
# edit it: [[repos]] name + checkout, agent_login, allowed, provider/model
export SUPERAGENT_GITHUB_TOKEN=...      # never put the token in the config file
.venv/bin/superagent check              # fix anything marked FAIL
.venv/bin/superagent run                # leave running
.venv/bin/superagent ask --repo owner/name "add a CONTRIBUTING.md"   # in another terminal
.venv/bin/superagent panel              # http://127.0.0.1:8742
```

Each section below explains a step in detail.

### Windows

The same steps in PowerShell, with four differences: venv scripts live in `.venv\Scripts`, the config needs the
full path to Pi's `.cmd` launcher, Windows paths go in single-quoted TOML strings, and the token is typed at a
prompt.

```powershell
git clone https://github.com/mikeliaowei/pi-superagent.git
cd pi-superagent
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[panel]"
New-Item -ItemType Directory -Force "$HOME\.config\pi-superagent" | Out-Null
Copy-Item config.example.toml "$HOME\.config\pi-superagent\config.toml"
```

In `%USERPROFILE%\.config\pi-superagent\config.toml`:

```toml
[executor]
checkout = 'C:\src\your-repo'                              # single quotes: TOML reads backslashes literally
pi = 'C:\Users\<you>\AppData\Roaming\npm\pi.cmd'           # (Get-Command pi).Source, with .cmd instead of .ps1
provider = "github-copilot"                                # any provider `pi --list-models` shows
model = "claude-opus-5.5"
```

- **Pi launcher.** An npm install of Pi puts `pi`, `pi.cmd` and `pi.ps1` in `%APPDATA%\npm`, and the default
  `~/.pi/agent/bin/pi` does not exist. Point `pi` at `pi.cmd`: Python cannot start `pi.ps1`
  (`OSError: [WinError 193] %1 is not a valid Win32 application`).
- **Token.** Set it in the terminal that runs `superagent`, typed at the prompt so it stays out of your shell
  history:

  ```powershell
  $env:SUPERAGENT_GITHUB_TOKEN = Read-Host -AsSecureString "GitHub PAT" | ConvertFrom-SecureString -AsPlainText
  ```

  This lasts until the terminal closes. To keep it for new terminals, also run
  `[Environment]::SetEnvironmentVariable("SUPERAGENT_GITHUB_TOKEN", $env:SUPERAGENT_GITHUB_TOKEN, "User")` (it is
  stored unencrypted in your user registry; VS Code terminals see it only after VS Code restarts). Check it with
  `[bool]$env:SUPERAGENT_GITHUB_TOKEN`, which prints `True` without showing the value.
- **`test_command`** runs with `sh -c`, which is not on a default Windows `PATH`. Before `superagent run`, add
  Git for Windows' shell: `$env:PATH = "C:\Program Files\Git\usr\bin;$env:PATH"`. Repos without `test_command`
  don't need it.
- **Your own GitHub account as `agent_login`.** Comments by `agent_login` are ignored as the agent's own, so your
  @-mentions don't start work. Ask with `superagent ask` or the panel's New request instead, or use a separate bot
  account.

Then run `.venv\Scripts\superagent check`, `.venv\Scripts\superagent run` and, in a second terminal,
`.venv\Scripts\superagent panel`.

## Setup

1. **Bot account.** Create a separate GitHub account for the agent and give it write access to the repository.
2. **Token.** As the bot, create a fine-grained personal access token for that one repository with
   *Contents*, *Issues* and *Pull requests* set to read and write. Export it under the name in `token_env`:
   `export SUPERAGENT_GITHUB_TOKEN=github_pat_...`. Turn on the bot's notifications for the repository (watch it)
   so new mentions show up.
3. **Local clone.** `git clone https://github.com/<owner>/<repo>.git ~/src/<repo>`; worktrees are created from
   it, starting at `origin/<base_branch>`.
4. **Config.** `mkdir -p ~/.config/pi-superagent && cp config.example.toml ~/.config/pi-superagent/config.toml`
   and edit it. Every field is commented there. For several repositories, replace `repo` and `checkout` with one
   `[[repos]]` table per repository (`name`, `checkout`, optional `base_branch`, `test_command`, `test_timeout`);
   the one token must cover all of them. Mixing the two forms, a duplicate name, or an empty list is refused.
   With `[[repos]]`, new worktrees live in `<work_dir>/<owner>__<name>/<ticket>`, and polling cursors and issue
   Trigger ids name the repo. Moving an existing single-repo config to `[[repos]]` keeps working on the same
   database: the original repo reads its old cursors, handled issues are not reopened, and existing Tickets keep
   their worktrees. Ticket names are unique across all repos.
5. **Model access.** Workers run with the explicit `provider`/`model` from the config. For Anthropic, set
   `ANTHROPIC_API_KEY` in the service's environment, or sign Pi in with a claude.ai account that has extra usage
   turned on. Pi's subscription sign-in alone is refused for third-party use ("Third-party apps now draw from your
   extra usage"); the run then fails, and the agent says so on the thread.

   Any provider Pi is already signed in to works the same way, with no key in this config: workers use the
   logins stored in Pi's agent dir (`~/.pi/agent`). See which are ready with
   `~/.pi/agent/bin/pi auth check --provider <name> --json`, list models with `pi --list-models`, then set for
   example `provider = "ollama"` and `model = "gemma4:31b-cloud"`. A GitHub Copilot sign-in works only for models
   your Copilot plan has enabled; otherwise Pi answers `The requested model is not supported`. Smaller models get
   more tickets `blocked` or `failed` on long multi-step tasks.

Then check everything:

```sh
.venv/bin/superagent check
```

It verifies the config loads, the token variable is set, every repo's checkout is a git clone with `origin`, `pi
--version` runs, and the report port is free, and prints a fix for each problem.

## Run

```sh
.venv/bin/superagent run
```

Ctrl+C (or SIGTERM) stops it gracefully: workers are closed and their Assignments stay "running" in the store, so
the next `superagent run` relaunches them with a recovery note. Logs go to stderr, one line per event, with the
Ticket's name, and to `<state_dir>/logs/service.log` (rotated at 2 MB, three old files kept as `service.log.1`..`.3`),
which the panel's worker log reads; the token is never logged.

## Use it on GitHub

- Open an issue, or comment on one or on a pull request, mentioning the bot: `@your-bot the login page crashes
  on empty passwords, please fix`. The bot replies once that it is on it.
- If it needs a decision it asks one question on the thread. Answer with a normal comment; the same Pi session
  resumes with your answer.
- Comments while it is working are passed to the running worker as context.
- When it is done it comments a link to its pull request. Merge or close the PR to end the Ticket.
- `@your-bot stop` ends the Ticket on that thread and stops its worker.
- With several repos configured, name the others in the request to change them together, e.g. `@your-bot rename
  the endpoint here and update the client in acme/web-client`: one Ticket, one PR per changed repo.

## Ask it from this machine or from Pi

You do not need GitHub to make a request. Whoever can run `superagent` on this machine is the operator, so a local
request carries an explicit mandate from `local_user` (config `[policy] local_user`, default your OS user name):

```sh
superagent ask --repo acme/api "fix the flaky test in tests/test_login.py"   # prints: queued request 1 for acme/api
superagent ask --repo acme/api --branch feature/login "add a unit test"     # work on an existing branch
superagent ask --repo acme/api --title "Login page" --new-branch feature/login-page \
  --skill ~/.agents/skills/tdd "rebuild the login page"                    # title, branch name, extra worker skill
superagent ask --repo acme/api --model github-copilot/claude-opus-5.5 --thinking high "fix the flaky test"
superagent show local:1            # the Ticket once `superagent run` picked it up: stage, PRs, conversation
superagent answer add-storage "Use SQLite."   # answer its question; the same Pi session resumes
superagent stop add-storage
```

`--repo` can repeat for a cross-repo change; without it the request goes to the only configured repo, or to the
configured repos its text names. A request gets a new branch `agent/<name>` from the base branch, unless `--branch`
names a branch that already exists on GitHub or only in the repo's checkout (one repo only, not the base branch): the
worktree then starts from that branch (from GitHub's copy when there is one; on a local `agent/<name>` branch, so it
never clashes with your own checkout of it), the commits are pushed
onto it without force (GitHub refuses anything but a fast-forward, so nobody's commits are overwritten; a local-only
branch is published by that push), and the
branch's open pull request is reused, or one is opened against the base branch. The running service picks requests up each poll round. A local Ticket's thread is
`local:<id>`: its replies (the acknowledgement, questions, reports and PR links) go to a local outbox that
`superagent show` and the panel display, never to GitHub. Its pull requests say "Code change requested by super
agent on behalf of <user>."
instead of closing an issue. Everything else (worker, tests, cross-repo PRs, stop and retry) works as for GitHub.

`--title` names the Ticket: its name (and so its default branch `agent/<name>`) is made from the title instead of
the first line of the request. `--new-branch NAME` names the new branch instead (not with `--branch`): it must be a
valid name, not the base branch, not a branch the repo's checkout knows (on GitHub as of its last fetch, or local),
and not the branch of an open Ticket, since the service force-pushes its new branches. The worktree's local branch
stays `agent/<name>`. `--skill PATH` (repeatable) gives the worker a skill folder (with a `SKILL.md`) or `.md` file
besides its own `superagent-worker` skill; it still discovers no other skills, extensions or prompts.
`--model provider/model` (as `pi --list-models` shows; a bare model uses the config's provider) and
`--thinking off|minimal|low|medium|high|xhigh|max` set the worker's model and thinking level instead of the config's.
The Ticket's name keeps up to 8 words and 48 characters of the title, cut at a whole word.

### From a Pi chat

```sh
superagent pi-install        # writes ~/.pi/agent/skills/superagent/SKILL.md and ~/.pi/agent/prompts/superagent.md
```

Then run `/reload` in Pi (or restart it) and try:

- `/superagent fix the flaky test in owner/repo`: queues the request with `superagent ask`.
- "what are my super-agent tickets?": Pi runs `superagent tickets --json` and reports names, stages and PR links.
- "answer its question: use SQLite": Pi runs `superagent answer <ticket> "use SQLite"`.

The skill uses the absolute path of the `superagent` that ran `pi-install` (plus `--config` when you passed one), and
tells Pi to confirm ambiguous requests and stops with you and never to touch the Tickets' worktrees itself.
`--agent-dir DIR` installs elsewhere (default: `$PI_CODING_AGENT_DIR` or `~/.pi/agent`). It refuses to overwrite
files it did not write unless you pass `--force`; `superagent pi-install --uninstall` removes only its own files.

### Look inside a worker's Pi session

Each Ticket is one Pi session named after its id, stored in `<state_dir>/sessions`. To read what a worker did,
open the Ticket in the web panel: its **Worker log** tab lists the session step by step (see Web panel).
To continue it by hand, open it in Pi from the Ticket's worktree (the panel's ticket details show the path),
only while that Ticket's worker is not running:

```sh
cd <worktree shown in the panel>
~/.pi/agent/bin/pi --session-dir ~/.local/share/pi-superagent/sessions --session-id <ticket-id>
```

## Operator commands

| Command | What it does |
|---|---|
| `superagent check` | Preflight; exit 2 if the config or token is missing, 1 if another check fails. |
| `superagent run` | Run the service. |
| `superagent status` | JSON: repo (`repos` with `[[repos]]`), agent login, whether the token is set, open Tickets by stage, failing effects, last poll time. |
| `superagent tickets [--all] [--json]` | Tickets with id, name, repo (with `[[repos]]`; every repo of a cross-repo Ticket), stage, issue number (`local` for a local Ticket), PR (`owner/name#<n>, ...` for a cross-repo Ticket) and last report; `--json` adds each Ticket's `thread` (`github:owner/name#<n>` or `local:<id>`). |
| `superagent ask [--repo R ...] [--title T] [--branch B \| --new-branch N] [--skill PATH ...] [--model M] [--thinking L] TEXT` | Queue a local request (see above); `--branch` works on an existing branch, `--new-branch` names the new one, `--skill` adds worker skills, `--model` and `--thinking` set up the worker. |
| `superagent show <ticket> [--json]` | A Ticket's stage, repos, PR links and conversation (request, local replies, answers); `local:<id>` also finds a request not picked up yet. |
| `superagent answer <ticket> TEXT` | Give a Ticket an answer or instruction: it steers a running worker or resumes the same session. On an ended Ticket it reopens it first (unless another Ticket is open on its thread); one that ended merged or closed gets a new pull request on its next delivery. A new run also clears a failed delivery. |
| `superagent pi-install [--agent-dir DIR] [--force] [--uninstall]` | Install the Pi skill and `/superagent` prompt template (see above). |
| `superagent stop <ticket>` | Stop a Ticket (id or name). Its worker ends with every process its tools started. |
| `superagent retry <ticket>` | Relaunch a Ticket whose last run ended, or retry a failed delivery. |
| `superagent panel [--port 8742]` | Local web panel (see below); needs the `panel` extra. |

`stop` and `retry` write a request to the store (`operator:stop:<ticket>` / `operator:retry:<ticket>` in the kv
table); `ask` and `answer` queue theirs in the `local_requests` table. The running service applies them at its next
poll round; if the service is not running, at the next start. All commands take `--config PATH` (default `~/.config/pi-superagent/config.toml`) and `-v` for
debug logging. Exit codes: 0 ok, 1 runtime error, 2 configuration error.

## Web panel

A local web panel with four tabs.

- **Overview**: every Ticket at a glance. Counts of all, open, needing you, succeeded (pull request merged) and not
  merged (closed or stopped); a **Needs you** list (waiting for your answer or review, blocked, failed, or not
  delivered); charts of Tickets by status and per repo, Tickets opened and ended per day and worker tokens per day
  over the last 30 days; and the last 24 hours (running workers, opened, ended, worker cost).
- **Tickets**: one table of Tickets (open, or all), filtered by repo, status and text, with plain-language status,
  branch, issue and PR links, tokens and cost. Click a row to open its details below, and click it again to hide
  them; they stay open through the 10 s refreshes, and close when a new ticket or another filter changes the rows:
  status, repo, branch, links, who asked, its question or latest report, a box for an
  answer or instruction (the same as `superagent answer`: a running worker gets it right away, a finished one is
  resumed with it, an ended Ticket is reopened with it), **Stop** and **Retry**, and four tabs. **Worker log**
  shows, read only, the worker's activity from its Pi or Copilot session files (the end of each file, 5 MB): the
  assignment, what the agent said, each tool call with its arguments, each result or error (folded, long output
  trimmed to its head and tail), and a failed or aborted model call with its error and stop reason, each with its
  time, newest at the bottom; the last 50 steps (**Show all steps**), reasoning hidden (**Show reasoning**). Below it,
  the **Service log** lines about this Ticket (launch, tests, delivery, reports), read from the rotating log file
  `superagent run` writes (`<state_dir>/logs/service.log`, 2 MB, 3 rotations). The GitHub token and the Ticket's
  report token are masked as `***`, and strings that look like tokens or keys (GitHub, Anthropic, OpenAI, AWS,
  bearer tokens, private keys) are masked too. **Request** (a local
  Ticket's whole **Conversation**), **Runs** (each worker start and how it ended) and **Messages to worker** (marked
  delivered or not).
- **New request**: the same as `superagent ask`. A **Title** (required; it names the Ticket) and **Requirements** open
  a new Ticket on a new branch (`agent/<name>`, or the **New branch name** you give) or, with **Existing branch**, on
  a branch you pick from the repo's GitHub and local branches (**Refresh from GitHub** runs `git fetch --prune origin`
  in its checkout) or type. **Skills for the worker** adds skills from `~/.pi/agent/skills`, `~/.agents/skills` and
  the chosen repos' `.agents/skills`, `.pi/skills` and `.claude/skills`. **Model** and **Thinking level** set up the
  worker; empty keeps the config's. With **Continue ticket** naming an open one, the text goes to that Ticket's
  worker on its branch and PR instead, like `superagent answer`.
- **Settings**, in five sub-tabs. **Repos**: each repo in its own card: its name (owner/name), checkout folder, base
  branch, test command and test
  timeout, all editable, each with its own **Save**, and **Remove** (not the last repo). A repo with Tickets keeps its
  name and cannot be removed, since they refer to it. **Add a repo** adds one as a new `[[repos]]` table; a config in
  the single-repo form (`repo` + `checkout`) is moved to `[[repos]]` tables first, and that repo's Tickets, history
  and GitHub read positions carry over. The bot token must reach the new repo too. **Worker and service** changes
  the agent (Pi or GitHub Copilot CLI), model, thinking level, max workers, test fix attempts and poll interval. Every
  change rewrites only its lines of the config file, keeping its comments, and refuses (writing nothing) a value the
  service would reject or a file laid out in a way it cannot edit exactly; restart `superagent run` to apply it. **All
  settings** lists the config read-only (whether the token is set, never its value). **Service health** shows the
  last poll and failing effects; **Incoming messages** lists every GitHub issue and comment the service read and what
  it did with it (started work, added as context, stopped a ticket, or ignored) and why: the place to look when the
  agent did not answer a comment.

Tokens and cost are summed from the worker's session files. The cost is Pi's figure (for some providers an estimate
from its price table, 0 where Pi has no price); Copilot workers show 0, since Copilot bills premium requests.
`superagent panel` replaces itself with Streamlit, so Ctrl+C or `kill` stops the panel completely (on Windows it runs
Streamlit as a child, and Ctrl+C stops both).
Databases from earlier versions gain new tables when opened (`trigger_reasons`, `local_requests`,
`local_messages`); existing tables are never altered, so an older version keeps working on the same database.
Triggers recorded before reasons were kept show no reason.

```sh
pip install -e ".[panel]"
superagent panel            # http://127.0.0.1:8742, --port to change
```

The worker log reads only the selected Ticket's files (`<state_dir>/sessions/*_<ticket-id>.jsonl`, the last 5 MB,
and `<state_dir>/logs/service.log`) and masks strings that look like tokens or keys (GitHub, Anthropic, OpenAI,
AWS, bearer tokens, private keys) before showing them; masking is pattern-based, so treat the panel as private anyway.

It binds to 127.0.0.1 only and refreshes every 10 s. It reads the same database as `superagent run`; Stop and Retry
record the same requests as `superagent stop|retry` (Answer and New request as `answer|ask`), which the running service applies at its next poll round.

## Safety model

- Each Ticket works in its own git worktree on `agent/<name>`; Pi gets only the `tools` allow-list.
- Workers get no credentials: the GitHub token and every variable in `secret_env` are removed from their
  environment. Only the service pushes and opens pull requests.
- Workers run with explicit provider and model, and without your Pi extensions, skills, prompt templates or
  context files (`-ne -ns -np -nc`), except the agent's own worker skill.
- Workers report over localhost with a per-Ticket token. A report is accepted only while that Ticket's current
  run is going; a late one gets 409 and changes nothing.
- Worktrees share `.git/config` and hooks with the checkout, so a worker can plant hooks or URL rewrites there.
  Every git command the service runs therefore has hooks off (`-c core.hooksPath=/dev/null`; on Windows a path
  below `C:/Windows/System32/cmd.exe`), no system or global
  git config, and no token or `secret_env` variable in its environment. The token is used only by the final
  `git push`, and by the fetch of a checkout whose origin is `https://github.com/<owner>/<name>`: each runs from a
  bare repo created for that command alone (mode 0700, deleted afterwards), and the checkout then fetches from that
  repo without the token. The token-bearing command has proxy and TLS settings pinned on its command line
  (`http.proxy` and `http.https://github.com/.proxy`, `sslVerify=true`) and an environment holding nothing but
  `PATH`, `HOME`, `SYSTEMROOT` (on Windows also `WINDIR`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `TEMP`, `TMP`,
  `PATHEXT`, `COMSPEC`) and the auth header. A long-lived push repo would not be safe: a worker could plant a
  repo-specific `http.<url>.proxy` there, which beats any pin.
- **HTTPS proxy.** Behind a corporate proxy, start `superagent run` with `HTTPS_PROXY` (or `https_proxy`) set, for
  example `HTTPS_PROXY=http://proxy.example:8080`. The GitHub API calls use it, and the token-bearing push and fetch
  pin `http.proxy` to exactly that value (empty when it is unset), so only the service's own environment decides
  the proxy: nothing a worker can write (a worktree's or the checkout's git config, a push repo) can point the token
  at another proxy or turn TLS verification off. The proxy URL is visible in that git command's arguments, so do not
  put a proxy password in it. API calls verify the certificate chain and host name with the system's CAs; only
  Python 3.13's extra strict X.509 check is relaxed, which TLS-inspecting corporate proxies often fail.
- Other origins (SSH, or a non-GitHub host) are fetched with plain `git fetch origin` in the checkout; since global
  git config is off there, it cannot use a credential helper configured there: use an SSH remote or a public
  repository.
- Workers get git credential helpers and prompts turned off (`credential.helper=`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/usr/bin/false`, `SSH_ASKPASS=/usr/bin/false`; on Windows also `GCM_INTERACTIVE=never` and a
  failing `deny-askpass.cmd`), so their `git push` cannot fall back on your keychain. This only raises the bar: a worker with `bash` runs as your user and can still read any file you can
  read, including `<state_dir>`. Container isolation is the real fix.
- Workers can use Pi's stored provider logins (that is how signed-in providers work), and a worker with `bash`
  could also read that file. Treat model credentials Pi holds as reachable by workers.
- A Copilot worker (`agent = "copilot"`) runs `copilot --acp` with every tool allowed, without your plugins,
  AGENTS.md files or Copilot's GitHub MCP server (`--disable-builtin-mcps`, so it cannot call GitHub with your
  login), and gets the worker prompt and skill in its first message. It runs with its own `COPILOT_HOME`
  (`<state_dir>/copilot-home`), not your `~/.copilot`, which can add well over 100k tokens to every model call; your
  skills in `~/.agents/skills` and `~/.claude/skills` still load, but only on use. `tools` does not apply. It signs
  in with your stored `copilot login` (the OS credential store); where the CLI keeps the login in `~/.copilot`
  instead, sign the workers in once with `COPILOT_HOME=<state_dir>/copilot-home copilot login`. A worker with a
  shell could use that login too. It cannot take a message mid-run: one arrives with its next run. Its activity on
  the panel is read from `<state_dir>/copilot-home/session-state`; its cost shows as 0, since Copilot bills premium
  requests.
- **Pi does not ask before running tools.** A worker can run any shell command its `bash` tool allows, as your
  user. Use a test repository and a machine you do not mind it touching. Container isolation is not built yet.

## Limits and not built yet

- Explicit requests only: no observe mode or proactive rules.
- No LLM router; the policy gate is deterministic.
- The web panel is read-only apart from Stop, Retry, Answer / Send instruction, New request and Settings (which
  writes the config file); no login, so keep it on localhost.
- Sources are GitHub and local requests (`superagent ask`, the Pi skill); others (Slack, email, ...) are not ported.
- Anyone who can run `superagent` as you (or write its database) can request work: local requests are not checked
  against `allowed`.
- A Ticket can span several configured repositories only when the request names them; a later comment cannot
  add a repo to a running Ticket.
- Rolling back to an earlier version: the database stays readable and writable by it, and Tickets that never used
  test retries, cross-repo parts or local requests load there unchanged. Tickets that did use them only load in
  this version or later.

## Development

```sh
.venv/bin/pip install -e ".[panel]"
.venv/bin/python -m unittest discover -s tests -t .
```

The tests use a fake Pi and a fake GitHub, real git in temporary directories, and no network. Issues and pull
requests are welcome; please run the tests first. `odd/tasks/` holds the design log the project was built from.

## License

MIT. Design credit: DeepSeek's dsh-superagent prototype (MIT).
