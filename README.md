# pi-superagent

A coding agent for one GitHub repository. Mention its bot account on an issue or pull request, and it opens a
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
- **Delivery.** On `ready`, the service (not the worker) pushes `agent/<name>` with its token and opens or reuses
  a PR. It first fetches the worktree's HEAD into its own bare repo `<state_dir>/push.git` and pushes from there; the body says `Closes #<n>` when the request came from an issue. No commits means a "no changes" reply
  and no PR. The Ticket ends when the PR is merged or closed, or when someone stops it.
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
   and edit it. Every field is commented there.
5. **Model access.** Workers run with the explicit `provider`/`model` from the config. For Anthropic, set
   `ANTHROPIC_API_KEY` in the service's environment, or sign Pi in with a claude.ai account that has extra usage
   turned on. Pi's subscription sign-in alone is refused for third-party use ("Third-party apps now draw from your
   extra usage"); the run then fails, and the agent says so on the thread.

Then check everything:

```sh
.venv/bin/superagent check
```

It verifies the config loads, the token variable is set, the checkout is a git clone with `origin`, `pi
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

## Operator commands

| Command | What it does |
|---|---|
| `superagent check` | Preflight; exit 2 if the config or token is missing, 1 if another check fails. |
| `superagent run` | Run the service. |
| `superagent status` | JSON: repo, agent login, whether the token is set, open Tickets by stage, failing effects, last poll time. |
| `superagent tickets [--all] [--json]` | Tickets with id, name, stage, issue number, PR and last report. |
| `superagent stop <ticket>` | Stop a Ticket (id or name). |
| `superagent retry <ticket>` | Relaunch a Ticket whose last run ended, or retry a failed delivery. |
| `superagent panel [--port 8742]` | Local web panel (see below); needs the `panel` extra. |

`stop` and `retry` write a request to the store (`operator:stop:<ticket>` / `operator:retry:<ticket>` in the kv
table). The running service applies it at its next poll round; if the service is not running, it applies it at
the next start. All commands take `--config PATH` (default `~/.config/pi-superagent/config.toml`) and `-v` for
debug logging. Exit codes: 0 ok, 1 runtime error, 2 configuration error.

## Web panel

A local web panel shows what needs a person first, in the spirit of dsh-superagent's panel: Tickets waiting for
an answer, a review, or help after a block, failure or delivery error, each with its thread link and Stop / Retry
buttons. Its other tabs list Tickets (filter by stage, open/all, text; details show the mandate, assignments,
request and pending inputs), recent Triggers with the policy decision (`accept`, `stop`, `context`, `ignore`), and
the loaded settings (whether the token is set, never its value).

```sh
pip install -e ".[panel]"
superagent panel            # http://127.0.0.1:8742, --port to change
```

It binds to 127.0.0.1 only and refreshes every 10 s. It reads the same database as `superagent run`; Stop and Retry
record the same requests as `superagent stop|retry`, which the running service applies at its next poll round.

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
  `git push` from `<state_dir>/push.git`, whose environment holds nothing but `PATH`, `HOME` and the auth header.
  Because global git config is off, `git fetch origin` in the checkout cannot use a credential helper configured
  there; use an SSH remote or a public repository for `checkout`.
- Workers get git credential helpers and prompts turned off (`credential.helper=`, `GIT_TERMINAL_PROMPT=0`,
  `GIT_ASKPASS=/usr/bin/false`, `SSH_ASKPASS=/usr/bin/false`), so their `git push` cannot fall back on your
  keychain. This only raises the bar: a worker with `bash` runs as your user and can still read any file you can
  read, including `<state_dir>`. Container isolation is the real fix.
- **Pi does not ask before running tools.** A worker can run any shell command its `bash` tool allows, as your
  user. Use a test repository and a machine you do not mind it touching. Container isolation is not built yet.

## Limits and not built yet

- Explicit requests only: no observe mode or proactive rules.
- No LLM router; the policy gate is deterministic.
- The web panel is read-only apart from Stop and Retry; no login, so keep it on localhost.
- GitHub only; other sources (Slack, email, ...) are not ported.
- One repository per service.

## License

MIT. Design credit: DeepSeek's dsh-superagent prototype (MIT).
