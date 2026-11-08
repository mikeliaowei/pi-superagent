---
name: superagent
description: Delegate coding work on a configured GitHub repository to the background super-agent, check its tickets and pull requests, and answer its questions. Use when the user asks to hand off, queue or delegate a coding task to the super-agent, asks about super-agent tickets, their status or PRs, or wants to answer, stop or retry one.
# superagent-pi-install: managed file (remove with `superagent pi-install --uninstall`)
---

# The super-agent

The super-agent is a background service (`superagent run`) that works coding requests as Tickets: one worker per
Ticket in its own git worktree, and a pull request per changed repository when the work is ready. You talk to it
only through the commands below. Run them exactly as written, with this absolute command:

```
{{SUPERAGENT}}
```

## Commands

- Queue a request: `{{SUPERAGENT}} ask --repo owner/name "what to do"`. Repeat `--repo` for a change across
  several repositories. Only configured repositories work; an error lists them. Without `--repo` the request goes
  to the only configured repository, or to the configured repositories its text names. The output says
  `queued request <id>` and whether the service is running.
- List tickets: `{{SUPERAGENT}} tickets --json` (add `--all` to include ended ones). Each row has `id`, `name`,
  `repo`, `stage`, `pr`, `thread` (`local:<id>` for requests made here) and the last report.
- One ticket: `{{SUPERAGENT}} show <ticket> --json`, where `<ticket>` is its id, name, or `local:<request id>`.
  It returns the stage, repos, PR links (`prs`), the open question, and the conversation: the request, every
  reply from the super-agent (acknowledgement, questions, reports, PR links) and every answer.
- Answer a question, or add an instruction: `{{SUPERAGENT}} answer <ticket> "the answer"`. The service resumes
  the same worker session with it (or steers the worker if it is running).
- Stop a ticket: `{{SUPERAGENT}} stop <ticket>`. Retry a failed or blocked one: `{{SUPERAGENT}} retry <ticket>`.

## How to work with it

1. Before `ask`: when the request is ambiguous (which repository, what exactly, what "done" means), ask the user
   first and confirm the exact text you will send. A clear request needs no confirmation.
2. Before `stop`: confirm with the user which ticket, unless they named it unambiguously.
3. After `ask`, tell the user the request id. After a poll round, `show local:<id> --json` gives the ticket's name.
4. When reporting status, give each ticket's name and id, its stage, any question, and PR links verbatim.
   A stage `person/answer` means the super-agent waits for an answer: show the question and offer to answer it.
5. Answer only with what the user said or approved; do not invent decisions on their behalf.
6. Never edit, commit in, or run commands inside the ticket worktrees yourself, and never push or open pull
   requests for its branches: the super-agent's worker owns them.
7. If a command fails, show its error line to the user. "not configured" errors list the valid repositories; a
   missing config means the user has to set up `superagent` first.
