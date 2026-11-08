---
description: Queue a coding request for the background super-agent
argument-hint: "<what to do, naming owner/repo>"
# superagent-pi-install: managed file (remove with `superagent pi-install --uninstall`)
---
Read the superagent skill at {{SKILL_PATH}} and follow it.

Then queue this request for the super-agent with its `ask` command (`{{SUPERAGENT}} ask`), adding one `--repo`
for each configured repository it should work in:

$ARGUMENTS

If the request is empty or ambiguous (no clear task, or unclear which repository), ask me first instead of
queueing it. After queueing, tell me the request id and how to follow it.
