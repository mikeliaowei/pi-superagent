# pi-superagent MVP

Objective: a Python service that ports dsh-superagent's ideas onto the Pi agent harness: it takes explicit GitHub requests, runs one Pi worker per Ticket, and delivers replies and PRs.
Branch: main (new repo, no commits yet). Delivery: ask-on-risk. Runner: `.venv/bin/python -m unittest discover -s tests -t .` (stdlib unittest).

## Specs

- S1 (L1) "I want to use python to create my own super-agent can use in pi agent harness". Python package `superagent`, Python >= 3.11, stdlib-first.
- S2 (L2 Sources) "GitHub only (Recommended)": poll GitHub every `poll_seconds` (default 60) for issues, issue comments and PR review comments in one configured repository. Detect requests by notifications plus per-item comment reads with a cursor that steps back 5 s; Trigger ids are deterministic (`github-comment|<id>|<updated_at>`, `github-issue|<number>`), so overlap never duplicates. Skip the agent's own comments.
- S3 (L2 Identity) "Separate bot account (Recommended)": the service comments and opens PRs as the configured `agent_login`, using a token read from an environment variable named in config. Replies carry a hidden `<!-- superagent:<msg-id> -->` marker and are skipped if already posted.
- S4 Policy (explicit-only MVP): a human on `allowed` who @-mentions `agent_login` creates a Ticket (explicit mandate). `@agent stop` from an allowed human stops the Ticket on that thread. Any human comment on a thread with a live Ticket is delivered to that Ticket as context (no new authority). Bots and the agent itself are ignored.
- S5 (L2 Model) "Claude via API key (Recommended)": workers run `pi --mode rpc` with explicit `--provider`/`--model` from config (default `anthropic` / `claude-sonnet-5-5`), never the user's Pi defaults, and without the user's extensions, skills, prompt templates and context files (`-ne -ns -np -nc`) except the super-agent's own worker skill.
- S6 (L2 Safety) "Worktree + limited tools (Recommended)": each Ticket gets a git worktree on branch `agent/<name>` under `work_dir`, created from a local `checkout` of the repo. Pi gets only the configured tool allow-list. The worker environment never contains the GitHub token or other configured secrets.
- S7 Worker protocol: a run is finished only at the RPC `agent_settled` event; an assistant `message_end` with `stopReason: "error"` is a failed run, not a finished one. Extension UI dialogs are answered `cancelled` so a worker can never block. The worker reports through a `superagent-report` CLI (status `progress|waiting|ready|blocked|failed|dropped`, summary, optional question) that posts to the service over localhost with a per-Ticket token.
- S8 Delivery: on `ready`, the service pushes the branch with its own token and opens (or reuses) a PR, then comments the link on the request thread. On `waiting`, it posts the question; a human reply on the thread resumes the same Pi session. The Ticket ends when its PR is merged or closed, or when stopped.
- S9 Durability: SQLite stores Triggers, Tickets, Inputs, and an effect queue (reply, launch, deliver) with per-(kind,key) idempotency and backoff from 5 s doubling to 300 s. After a restart, running Assignments end as "host restarted" and resume with a recovery note.
- S10 Operator surface: CLI `superagent run`, `superagent status`, `superagent tickets`, `superagent stop <ticket>`, `superagent retry <ticket>`. Panel deferred.

## Tasks

| ID | Specs | Route | Status | Evidence |
|---|---|---|---|---|
| T1 | S1 S4 S9 | inline | done | 13 tests GREEN; commit 3db9106 |
| T2 | S2 S3 S8 | delegated (parallel unit, disjoint surface) | done | 36 tests GREEN; commit 54843d1; Risk: item 2 (token handling) |
| T3 | S5 S6 S7 | delegated (parallel unit, disjoint surface) | done | 27 tests GREEN; commit 84ec9c0; Risk: item 2 (secret stripping, report auth) |
| T4 | S8 S9 S10 | delegated after T2/T3 (context backstop) | done | 26 e2e tests GREEN, 102 total; commit 3c5ae32; Risk: items 2 and 4 |
| T5 | all | inline | open | live run once an Anthropic API key or extra usage is available |

Route evidence: T2 and T3 are independent units of behavior with disjoint edit surfaces, each several hundred lines (Writer trigger, parallelism); T4 is delegated by the long-session context backstop.

## Log

- L1 (2026-11-08) User request, verbatim: "I want you use reverse engineer to under stand this repo https://github.com/deepseek-ai/dsh-superagent-prototype, I want to use python to create my own super-agent can use in pi agent harness"
- L2 (2026-11-08) User answers: Sources "GitHub only (Recommended)"; Model "Claude via API key (Recommended)"; Identity "Separate bot account (Recommended)"; Safety "Worktree + limited tools (Recommended)".
- L3 Analysis: dsh-superagent source read directly (open source TypeScript, so REA was not needed). Its `src/core` has no DSH imports; only the executor, sessions, sources' plugin glue and panel depend on DSH. Pi `--mode rpc` replaces the in-process DSH session.
- L4 Verified Pi 1.4.0 RPC live: `get_state` ok, `prompt` -> `disposition: started`, events end with `agent_settled`. A provider failure arrives as assistant `message_end` with `stopReason: "error"` (here: Anthropic OAuth "Third-party apps now draw from your extra usage"), so OAuth subscription use needs extra usage or an API key.
