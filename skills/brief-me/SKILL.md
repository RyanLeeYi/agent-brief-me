---
name: brief-me
description: Batch-review the agent-brief inbox - show unread reports, walk pending questions grouped by project and severity via AskUserQuestion, then offer to dispatch headless sessions for projects with unconsumed answers. Use when the user runs /brief-me or asks to review, check, or triage the agent-brief inbox.
---

# brief-me

One review pass over `~/.agent-brief/`. All file access goes through
`scripts/brief_me.py` (repo/plugin root, next to `scripts/dispatch.py`); do
not read or write `inbox.jsonl` / `answers.jsonl` yourself. Every command
prints one JSON object. Run the steps in order.

## Step 1: Load

```
python scripts/brief_me.py load
```

Runs the configured collector first (a failing collector is reported in
`collector_warning` and never stops the pass), then returns:

- `unread_reports`: `{project: [ {id, project, summary, severity?, session_id?, age} ]}`
- `pending_questions`: `{project: [ {id, project, title, body, severity, session_id?, options, age} ]}`
  (projects alphabetical; questions high severity first, then oldest first)
- `unconsumed_projects`: projects with an answer not yet used by dispatch

If `collector_warning` is set, show it. If both `unread_reports` and
`pending_questions` are empty, say the inbox is empty and stop.

## Step 2: Show unread reports

For each project, for each report in the given order: display `summary` and
`age`; if `session_id` is present add `resume: claude --resume <session_id>`
(run inside the project directory). Immediately after displaying each one:

```
python scripts/brief_me.py read <report_id>
```

One report at a time - display, then mark - so an interrupted run never
leaves a shown report unmarked.

## Step 3: Walk pending questions

For each project, for each question in the given order, call
AskUserQuestion with `title` as the header, `body` (plus project, severity,
and the `resume: claude --resume <session_id>` hint when present) as the
supporting text, and `options` as the choices in the given order
(recommendation first when present, "Dismiss" then "Skip" always last). When
the question has `multi: true`, pass `multiSelect: true` so several choices
can be picked at once (Dismiss/Skip still stand alone). Do not add a "type
your own" option - the tool already accepts free text.

- **Skip** picked: write nothing; the question stays pending for next time.
- **Dismiss** picked: drop it for good, no answer written:
  `python scripts/brief_me.py dismiss <question_id>`
- **A listed option** picked: `--chosen "<exact option text>"`; for a
  multi-select answer repeat `--chosen` once per picked option.
- **Free text** typed: `--free-text "<text>"` (pass both if the tool reports both).

```
python scripts/brief_me.py answer <question_id> [--chosen TEXT] [--free-text TEXT]
```

## Step 4: Offer to dispatch

```
python scripts/brief_me.py unconsumed
```

If `projects` is empty, finish. Otherwise ask, per project (alphabetical),
whether to dispatch it now. If any were confirmed, invoke once from the
repo/plugin root:

```
python scripts/dispatch.py [--watch] <confirmed-project-1> <confirmed-project-2> ...
```

Pass `--watch` if and only if `$ARGUMENTS` contains `--watch` (opens an
interactive `claude` window per project instead of a headless `claude -p`).
Marking answers consumed, logging and unknown-project handling belong to
`dispatch.py`, not here.

## Mid-session exit

Every write is a single atomic append and state is re-derived by folding the
files (`docs/schema.md`, "Deriving current state"), so stopping early loses
nothing: unanswered questions stay pending, shown reports are already marked
read, answers already written will be offered for dispatch next run.
