# agent-brief-me

agent-brief-me is an async decision inbox for Claude Code agents. A working
session that hits a decision only you can make, or that finishes a piece of
work, appends a record to a shared JSONL inbox instead of blocking on you
mid-run. You review the inbox in batches instead of babysitting every
session live.

The daily loop:

1. **Before bed**, run `/brief`. It shows unread reports, walks you through
   pending questions grouped by project (high severity first), then offers
   to dispatch a headless follow-up session for any project that has a
   fresh, unconsumed answer.
2. **Dispatch**: each project you confirm gets a background `claude -p`
   session, carrying its unconsumed answers, spawned by
   `scripts/dispatch.py`.
3. **Sleep**, while the dispatched sessions keep working unattended.
4. **In the morning**, run `/brief` again to review what came in overnight
   and answer whatever is still pending.

The only command you run day to day is `/brief`.

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
git clone <this-repo-url> ~/.claude/skills/agent-brief-me
```

Start (or restart) a Claude Code session anywhere, then run:

```
/agent-brief-me:brief-init
```

to create `~/.agent-brief/` and register the projects you want tracked.
From then on, day to day, run `/agent-brief-me:brief` (called `/brief`
throughout the rest of this README). To confirm the install itself, run
`claude plugin list`; a working install lists `agent-brief-me@skills-dir`
under "Skills-directory plugins".

## Skills

- `brief-init` -- one-time, idempotent setup: creates `~/.agent-brief/`,
  registers projects, and runs a smoke test.
- `brief` -- the daily review pass: unread reports, pending questions,
  optional dispatch.
- `brief-submit` -- used by dispatched sessions themselves (not by you
  directly) to file a question or a report into the inbox without blocking.

## Collector hook

`~/.agent-brief/config.json`'s `collector` field is an argv array. When set,
every `/brief` run executes it (cwd `~/.agent-brief`) before reading the
inbox, so it can pull structured to-dos from any source and file them as
`question` or `report` records per `docs/schema.md`'s atomic append rule
(append-mode open, hold an exclusive lock, write the whole line in a single
`write()` call, release the lock -- see the `atomic_append_line` helper in
`docs/schema.md`). A non-zero exit is reported as a warning; `/brief`
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

## Further reading

- `docs/schema.md` -- authoritative inbox schema and protocol.
- `docs/headless-delegation-evidence.md` -- evidence that dispatched
  headless sessions can themselves delegate to subagents.
- `AGENTS.md` -- development conventions for this repo.
