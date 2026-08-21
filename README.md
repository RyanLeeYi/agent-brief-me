# agent-brief-me

agent-brief-me is an async decision inbox for Claude Code agents. A working
session that hits a decision only you can make, or that finishes a piece of
work, appends a record to a shared JSONL inbox instead of blocking on you
mid-run. You review the inbox in batches instead of babysitting every
session live.

The daily loop:

1. **Before bed**, run `/brief-me`. It shows unread reports, walks you through
   pending questions grouped by project (high severity first), then offers
   to dispatch a headless follow-up session for any project that has a
   fresh, unconsumed answer.
2. **Dispatch**: each project you confirm gets a background `claude -p`
   session, carrying its unconsumed answers, spawned by
   `scripts/dispatch.py`. Run `/brief-me --watch` instead to open each
   worker as a visible interactive window you can follow along with.
3. **Sleep**, while the dispatched sessions keep working unattended.
4. **In the morning**, run `/brief-me` again to review what came in overnight
   and answer whatever is still pending. Every report and question carries
   the worker's `session_id`; if one is unclear, `cd` into that project and
   run the shown `claude --resume <session_id>` to ask the worker directly.

The only command you run day to day is `/brief-me`.

Two honest limitations:

- This only supports Claude Code today. A standalone MCP server, so other
  agent runtimes could submit to the same inbox, is future work.
- An overnight dispatch only keeps running if the machine it lands on stays
  awake (no sleep/hibernate) for the duration; nothing here wakes the
  machine up.

## Install

agent-brief-me is a [skills-directory plugin](https://code.claude.com/docs/en/plugins):
clone it straight into your personal skills directory and Claude Code loads
it automatically on the next session, no marketplace registration needed.

```sh
git clone https://github.com/RyanLeeYi/agent-brief-me.git ~/.claude/skills/agent-brief-me
```

Start (or restart) a Claude Code session anywhere, then run:

```
/agent-brief-me:brief-init
```

to create `~/.agent-brief/`, register the projects you want tracked, and
choose how workers are launched (see "Dispatch settings").
From then on, day to day, run `/agent-brief-me:brief-me` (called `/brief-me`
throughout the rest of this README). To confirm the install itself, run
`claude plugin list`; a working install lists `agent-brief-me@skills-dir`
under "Skills-directory plugins".

To update later: `git -C ~/.claude/skills/agent-brief-me pull`. Skills are
read fresh each session, so no reinstall is needed.

## Skills

- `brief-init` -- one-time, idempotent setup: creates `~/.agent-brief/`,
  registers projects, sets dispatch options, and runs a smoke test. Safe to
  rerun to change settings.
- `brief-me` -- the daily review pass: unread reports, pending questions,
  optional dispatch. The file handling lives in `scripts/brief_me.py`
  (`load` / `read` / `answer` / `unconsumed`), so the skill prompt stays
  small and the inbox never enters the model's context wholesale.
- `brief-submit` -- used by dispatched sessions themselves (not by you
  directly) to file a question or a report into the inbox without blocking.

## Dispatch settings

`brief-init` writes a `dispatch` object into `~/.agent-brief/config.json`;
`scripts/dispatch.py` reads it when launching workers. To change settings
later, edit that file directly in any editor - it costs no tokens and takes
effect on the next dispatch, no restart needed. Rerunning `brief-init` also
works (it skips existing files and re-asks the settings) but spends a few
thousand tokens on the walkthrough. Missing keys fall back to the defaults
shown below; an unknown `permission_mode` falls back to `auto`.

```json
"dispatch": {
  "watch": false,
  "permission_mode": "auto",
  "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep,Skill",
  "model": null,
  "delegate": true
}
```

- `watch` -- `true` opens an interactive `claude` window per project instead
  of a headless `claude -p` (same as passing `--watch` to `dispatch.py` or
  `/brief-me --watch`).
- `permission_mode` -- `auto` (default, combined with `allowed_tools`; blocked
  actions are silently denied in headless mode) or `bypassPermissions`.
  `~/.agent-brief` is always passed via `--add-dir` so `brief-submit` can
  write the inbox from inside a project.
- `model` -- `--model` for the worker session; `null` keeps Claude Code's
  default.
- `delegate` -- `false` tells workers to work solo and blocks the `Agent`
  tool; use it if your repos have no subagent-delegation rules.

> **Warning: `bypassPermissions`** launches workers with
> `--dangerously-skip-permissions`. Every command -- `rm -rf`, `git push
> --force`, anything -- runs without confirmation, unattended, possibly
> overnight. The only safety left is whatever hooks and guards your repos
> configure themselves. Prefer `auto` plus a tight `allowed_tools`.

## Collector hook

`~/.agent-brief/config.json`'s `collector` field is an argv array. When set,
every `/brief-me` run executes it (cwd `~/.agent-brief`) before reading the
inbox, so it can pull structured to-dos from any source and file them as
`question` or `report` records per `docs/schema.md`'s atomic append rule
(append-mode open, hold an exclusive lock, write the whole line in a single
`write()` call, release the lock -- see the `atomic_append_line` helper in
`docs/schema.md`). A non-zero exit is reported as a warning; `/brief-me`
proceeds regardless.

Generic example: file every line of a plain-text to-do list as a
low-severity question.

`~/.agent-brief/config.json`:

```json
{"projects": [], "collector": ["python3", "/path/to/todo-collector.py"], "notify": null}
```

`/path/to/todo-collector.py` (trimmed; reuse `atomic_append_line` from
`docs/schema.md` verbatim -- it is not repeated here):

```python
import os
import uuid
from datetime import datetime, timezone

# atomic_append_line(path, record) goes here -- see docs/schema.md

brief_home = os.environ.get("BRIEF_HOME", os.path.expanduser("~/.agent-brief"))
with open(os.path.expanduser("~/todo.txt")) as f:
    for line in f:
        text = line.strip()
        if not text:
            continue
        atomic_append_line(os.path.join(brief_home, "inbox.jsonl"), {
            "type": "question",
            "id": str(uuid.uuid4()),
            "project": "todo-import",
            "title": text,
            "body": "",
            "severity": "low",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
```

Any structured source works the same way -- an issue tracker webhook, a
calendar export, a cron job scraping logs -- as long as it ends up appending
valid `question`/`report` records through the same atomic-append rule.

## Notify hook

`config.json`'s `notify` field is also an argv array (or `null` for no
notifications). When set, submitting a **high**-severity question executes it
exactly once, with the question's `project` and `title` appended as two
trailing arguments; `low` and `normal` severities never trigger it. Point it
at whatever reaches you -- a Telegram bot script, a desktop notifier, a
webhook curl:

```json
{"projects": [], "collector": null, "notify": ["python3", "/path/to/send-telegram.py"]}
```

Your script then receives e.g. `send-telegram.py my-project "prod deploy blocked: pick a rollback strategy"`.

## Teaching your agents to use the inbox

Dispatched sessions only use the inbox if their instructions say so.
`brief-init` prints a one-paragraph protocol sentence (use `brief-submit`
when blocked on a user decision, and once more before finishing to report
what was done) and offers to append it to a rules file you name -- your
global instructions file is the usual target. Sessions spawned by
`scripts/dispatch.py` also get this instruction embedded in their prompt, so
the rules-file step mainly covers sessions you start through other means
(cron jobs, your own scripts).

## Further reading

- `docs/schema.md` -- authoritative inbox schema and protocol.
- `docs/headless-delegation-evidence.md` -- evidence that dispatched
  headless sessions can themselves delegate to subagents.
- `AGENTS.md` -- development conventions for this repo.
