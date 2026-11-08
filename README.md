# pi-superagent

A coding agent for one or more GitHub repositories. Mention its bot account on an issue or pull request, and it opens a
Ticket, runs a [Pi](https://github.com/earendil-works/pi) worker in its own git worktree, asks you when it needs a
decision, and delivers a pull request.

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
  (`sh -c`, with the worker's environment: no token, no `secret_env`, no git credential helpers; its own process
  group; killed after `test_timeout`). A failure opens no PR: the command, its exit code (or the timeout) and the
  last 3,000 characters of output go back to the same Pi session, which fixes and reports ready again. After
  `max_test_retries` failed fixes the Ticket is reported blocked ("tests still failing: <command>").
- **Delivery.** On `ready` (and passing tests), the service (not the worker) pushes `agent/<name>` with its token
  and opens or reuses a PR. For every push it creates a new private bare repo next to `<state_dir>/push.git`
  (single-repo form) or `<state_dir>/push/<owner>__<name>.git` (`[[repos]]`), fetches the worktree's HEAD into
  it, pushes from it, and deletes it, so nothing a worker wrote earlier can change how the token is sent. The
  body states the tests (``Tests: `<command>` passed in <N>s`` or `Tests: no test command configured for
  <repo>.`) and says `Closes #<n>` when the request came from an issue. No commits means a "no changes" reply
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

Python 3.11 or newer, standard library only.

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs `superagent` (the service and operator CLI) and `superagent-report` (what workers call) into the
venv. Workers find `superagent-report` because the service puts its own `bin` directory first on their `PATH`.

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
Ticket's name; the token is never logged.

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
superagent show local:1            # the Ticket once `superagent run` picked it up: stage, PRs, conversation
superagent answer add-storage "Use SQLite."   # answer its question; the same Pi session resumes
superagent stop add-storage
```

`--repo` can repeat for a cross-repo change; without it the request goes to the only configured repo, or to the
configured repos its text names. The running service picks requests up each poll round. A local Ticket's thread is
`local:<id>`: its replies (the acknowledgement, questions, reports and PR links) go to a local outbox that
`superagent show` and the panel display, never to GitHub. Its pull requests say "Requested locally by <user>"
instead of closing an issue. Everything else (worker, tests, cross-repo PRs, stop and retry) works as for GitHub.

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
or continue it by hand, open it in Pi from the Ticket's worktree (the panel's ticket details show the path),
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
| `superagent ask [--repo R ...] TEXT` | Queue a local request (see above). |
| `superagent show <ticket> [--json]` | A Ticket's stage, repos, PR links and conversation (request, local replies, answers); `local:<id>` also finds a request not picked up yet. |
| `superagent answer <ticket> TEXT` | Give a Ticket an answer or instruction: it steers a running worker or resumes the same session. |
| `superagent pi-install [--agent-dir DIR] [--force] [--uninstall]` | Install the Pi skill and `/superagent` prompt template (see above). |
| `superagent stop <ticket>` | Stop a Ticket (id or name). |
| `superagent retry <ticket>` | Relaunch a Ticket whose last run ended, or retry a failed delivery. |
| `superagent panel [--port 8742]` | Local web panel (see below); needs the `panel` extra. |

`stop` and `retry` write a request to the store (`operator:stop:<ticket>` / `operator:retry:<ticket>` in the kv
table); `ask` and `answer` queue theirs in the `local_requests` table. The running service applies them at its next
poll round; if the service is not running, at the next start. All commands take `--config PATH` (default `~/.config/pi-superagent/config.toml`) and `-v` for
debug logging. Exit codes: 0 ok, 1 runtime error, 2 configuration error.

## Web panel

A local web panel shows what needs a person first, in the spirit of dsh-superagent's panel: Tickets waiting for
an answer, a review, or help after a block, failure or delivery error, each with its thread link and Stop / Retry
buttons, plus an Answer box for a local Ticket waiting on a question and a New request form (the same as
`superagent ask`). Its other tabs list Tickets (filter by stage, open/all, text; details show the mandate, assignments,
request (a local Ticket's whole conversation) and every message for the worker, marked delivered or not), recent Triggers with the policy decision
(`accept`, `stop`, `context`, `ignore`) and why it was made, and the loaded settings (whether the token is set,
never its value). `superagent panel` replaces itself with Streamlit, so Ctrl+C or `kill` stops the panel completely.
Databases from earlier versions gain new tables when opened (`trigger_reasons`, `local_requests`,
`local_messages`); existing tables are never altered, so an older version keeps working on the same database.
Triggers recorded before reasons were kept show no reason.

```sh
pip install -e ".[panel]"
superagent panel            # http://127.0.0.1:8742, --port to change
```

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
  Every git command the service runs therefore has hooks off (`-c core.hooksPath=/dev/null`), no system or global
  git config, and no token or `secret_env` variable in its environment. The token is used only by the final
  `git push`, which runs from a bare repo created for that push alone (mode 0700, deleted afterwards) with proxy
  and TLS settings pinned and an environment holding nothing but `PATH`, `HOME` and the auth header. A long-lived
  push repo would not be safe: a worker could plant a repo-specific `http.<url>.proxy` there, which beats any pin.
  Because global git config is off, `git fetch origin` in the checkout cannot use a credential helper configured
  there; use an SSH remote or a public repository for `checkout`.
- Workers get git credential helpers and prompts turned off (`credential.helper=`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/usr/bin/false`, `SSH_ASKPASS=/usr/bin/false`), so their `git push` cannot fall back on your
  keychain. This only raises the bar: a worker with `bash` runs as your user and can still read any file you can
  read, including `<state_dir>`. Container isolation is the real fix.
- Workers can use Pi's stored provider logins (that is how signed-in providers work), and a worker with `bash`
  could also read that file. Treat model credentials Pi holds as reachable by workers.
- **Pi does not ask before running tools.** A worker can run any shell command its `bash` tool allows, as your
  user. Use a test repository and a machine you do not mind it touching. Container isolation is not built yet.

## Limits and not built yet

- Explicit requests only: no observe mode or proactive rules.
- No LLM router; the policy gate is deterministic.
- The web panel is read-only apart from Stop, Retry, Answer and New request; no login, so keep it on localhost.
- Sources are GitHub and local requests (`superagent ask`, the Pi skill); others (Slack, email, ...) are not ported.
- Anyone who can run `superagent` as you (or write its database) can request work: local requests are not checked
  against `allowed`.
- A Ticket can span several configured repositories only when the request names them; a later comment cannot
  add a repo to a running Ticket.
- Rolling back to an earlier version: the database stays readable and writable by it, and Tickets that never used
  test retries, cross-repo parts or local requests load there unchanged. Tickets that did use them only load in
  this version or later.

## License

MIT. Design credit: DeepSeek's dsh-superagent prototype (MIT).
