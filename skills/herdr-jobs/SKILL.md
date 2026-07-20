---
name: herdr-jobs
description: Launch and manage one or more Codex agents in background Herdr tabs. Use only when the user explicitly invokes $herdr-jobs to start, inspect, follow up with, collect from, or clean up background Codex jobs. Requires HERDR_ENV=1.
---

# Herdr Jobs

Use `scripts/herdr_jobs.py` for every operation. The script owns Herdr discovery,
IDs, launch mechanics, grouping, and cleanup safety. Do not recreate its Herdr
commands manually.

## Interpret requests

Support these operations:

```text
$herdr-jobs start [in <workspace>] <task>
$herdr-jobs status [<group>]
$herdr-jobs respond [<job>] <follow-up>
$herdr-jobs collect [<group>]
$herdr-jobs cleanup [<group>] [force]
```

For fan-out, interpret every `- label: prompt` bullet as one independent Codex
agent in one background tab. Split only on the first colon. Reject missing,
empty, or duplicate labels or prompts. For an unlabeled single task, choose a
short descriptive label.

Examples:

```text
$herdr-jobs start in tmux-setting
- config: review duplicate bindings
- plugins: check configured plugins
```

```text
$herdr-jobs collect a4f9
```

The `$herdr-jobs` text is a Codex skill invocation, not a shell command. Convert
the request into the script arguments below.

## Run the script

Resolve this skill directory and call:

```text
python3 scripts/herdr_jobs.py start [--workspace ID_OR_LABEL] \
  --task LABEL PROMPT [--task LABEL PROMPT ...] [--confirm]

python3 scripts/herdr_jobs.py status [--group TOKEN]
python3 scripts/herdr_jobs.py respond --job NAME_OR_LABEL --prompt TEXT
python3 scripts/herdr_jobs.py collect [--group TOKEN] [--lines 200]
python3 scripts/herdr_jobs.py cleanup [--group TOKEN] [--force]
```

Rules:

- Omit `--workspace` only when the user means the current Herdr workspace.
- Use fast start by default. Add `--confirm` only when the user asks to confirm
  that every agent reaches `working`.
- Do not wait for completion during `start` or `collect`.
- If the script returns `ambiguous_group`, present its candidates and ask the
  user to choose a group.
- If a fan-out partially fails, report both launched and failed tasks. Preserve
  the launched tasks.
- Treat Herdr IDs as opaque. Report the IDs returned by the script.

## Present results

For `start`, report the group token and each job's label, agent name, workspace,
tab, and pane. State whether launch was accepted or confirmed working.

For `status`, summarize each job's state:

- `working`: still running
- `blocked`: needs input
- `done`: finished and unseen
- `idle`: waiting or finished and seen
- `unknown`: not yet classified

For `collect`, synthesize completed transcripts into one concise response.
Separately list unfinished or blocked jobs. Never imply that `collect` waited.

For `respond`, confirm which job received the follow-up.

For `cleanup`, report closed and preserved tabs. Never add `--force` unless the
user explicitly requests forced cleanup. Normal cleanup must preserve
`working`, `blocked`, and `unknown` jobs.

## Boundaries

- Support Codex agents only.
- Create tabs only in existing Herdr workspaces.
- Do not create workspaces or Git worktrees.
- Do not run generic shell jobs.
- Do not focus another tab, pane, or workspace.
- Do not close tabs that are not owned by a generated `hj-<token>-<label>` job.
