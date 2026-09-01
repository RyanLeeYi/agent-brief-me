---
name: brief-init
description: First-time setup for agent-brief-me. Creates ~/.agent-brief/, registers projects to track, offers to document the agent-side protocol, and runs a smoke test. Use when the user runs /agent-brief-me:brief-init or asks to set up, initialize, or configure the agent-brief inbox.
---

# brief-init

One-time, idempotent setup for the agent-brief-me inbox. Run the steps below
in order. Report progress after each step; never batch the report to the end.

## Atomic append helper

Step 4 appends lines to `inbox.jsonl`. Every write to `inbox.jsonl` or
`answers.jsonl` must follow the atomic append rule in `docs/schema.md`:
append-mode open, hold an OS-level exclusive lock, write the whole line in a
single `write()` call, release the lock. Reuse this snippet (it mirrors
`tests/test_concurrent_append.py`'s `atomic_append_line()` helper) via
`python3 -c "..."` or a short throwaway script:

```python
import json, os

MAX_LINE_BYTES = 8 * 1024

def atomic_append_line(path, record):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("record exceeds 8 KB limit")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                written = os.write(fd, data)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                written = os.write(fd, data)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if written != len(data):
        raise IOError(f"short write: {written}/{len(data)} bytes")
```

## Step 1: Create ~/.agent-brief/

Compute `base = os.path.expanduser("~/.agent-brief")`.

1. Create `base` if it does not exist (`os.makedirs(base, exist_ok=True)`).
2. For each of the three files below, check whether it already exists
   *first*. An existing file is never truncated or overwritten; report
   "already exists, skipping" for it and move on. Only a missing file is
   created:
   - `inbox.jsonl`: create empty (0 bytes).
   - `answers.jsonl`: create empty (0 bytes).
   - `config.json`: create with exactly this content:
     ```json
     {"projects": [], "collector": null, "notify": null, "dispatch": {}}
     ```
3. If any of the three files already existed, state explicitly that this is
   a rerun on an existing installation and name which files were skipped. If
   all three were freshly created, state that this is a first-time setup.

`collector` and `notify`, once configured (by something other than
brief-init), are argv arrays, e.g. `["python3", "collector.py"]`. When
`notify` runs, `project` and `title` are appended, in that order, to the end
of the array before it is executed. brief-init only guarantees both fields
start as `null`; it never sets them itself.

## Step 2: Register projects to track

Repeat until the user is done:

1. Ask (AskUserQuestion) whether the user wants to add a project to track
   (e.g. options "Yes" / "No, done").
2. If yes, collect a project name and a filesystem path from the user.
3. Validate the path exists (`os.path.exists(path)`). If it does not, report
   the failure and let the user retry with a corrected path or drop this
   project; never store an unvalidated path.
4. On success, read `config.json`, append `{"name": name, "path": path}` to
   its `projects` array, and write the whole file back.
5. Stop as soon as the user says they are done.

## Step 3: Dispatch settings

These control how `scripts/dispatch.py` launches worker sessions. Ask
question 0 (probing first, as described below), then the seven questions
below it, all with AskUserQuestion, then read `config.json`, set the answers
under its `dispatch` object (create it if missing, keep any other keys), and
write the whole file back. On a rerun, show the current value of each
(including `window`) as the default option.

0. **Open dispatch windows inside Orca?** -> `window`: first probe by
   running `orca status --json` (Bash). If the command is not found on
   `PATH`, or it exits non-zero, do not ask this question at all: set
   `window` to `"console"` and print exactly this line: "Orca CLI not
   found, dispatch windows will use the system console". If the probe runs
   successfully, ask (AskUserQuestion) "Open dispatch windows inside
   Orca?" -> `"orca"` (yes) or `"console"` (no, default).
1. **Show a terminal window?** -> `watch`: `false` (headless `claude -p`,
   output goes to `~/.agent-brief/logs/`; default) or `true` (opens an
   interactive `claude` window per project so the user can watch).
2. **Permission level?** -> `permission_mode`:
   - `auto` (default): Claude Code auto mode plus the tool allowlist in
     `allowed_tools` (default `Bash,Read,Edit,Write,Glob,Grep,Skill`).
     Actions the classifier blocks are silently denied in headless mode.
   - `bypassPermissions`: runs with `--dangerously-skip-permissions`. Before
     accepting this, print the warning: "Workers will run every command,
     including destructive ones, with no confirmation, unattended and
     possibly overnight. Only choose this if the repos' own hooks/guards are
     what you rely on." Require a second explicit confirmation.
3. **Model for the worker session?** -> `model`: `null` (Claude Code
   default) or a model name/alias such as `sonnet`, `opus`.
4. **May workers delegate to subagents?** -> `delegate`: `true` (default;
   the prompt authorizes subagents under the user's own delegation rules) or
   `false` (prompt says to work solo and `Agent` is added to
   `--disallowedTools`; pick this if the harness has no delegation rules yet).
5. **Model for context-refill sessions?** -> `context_model`: `"sonnet"`
   (default) or another model name/alias. Used only when the user dispatches
   a context-refill for one vague pending question (from the Web UI's "Add
   context" button, or brief-me's per-question review): a lightweight
   session that reads the project and refiles a self-contained replacement
   question. It never edits the repo, so a cheap model is usually enough.
6. **Reply language for context-refill replacement questions?** -> `context_language`
   (F29): `null` (default; no requirement, worker replies in whatever
   language it would otherwise use) or one of the presets below - each
   composes into a single free-form English description string written
   verbatim to `context_language`:
   - Not specified -> `null`
   - Traditional Chinese -> `"Traditional Chinese"`
   - Simplified Chinese -> `"Simplified Chinese"`
   - English -> `"English"`
   - Japanese -> `"Japanese"`

   For any preset except "Not specified" or "English", ask a follow-up:
   **keep technical terms in English?** If yes, append
   `" (keep technical terms in English)"` to the string above (e.g.
   `"Traditional Chinese (keep technical terms in English)"`). This is the
   same preset list and composition rule the Web UI's Settings view uses, so
   a value set by either surface parses back into the other's dropdown.
7. **Plain-language requirement?** -> `plain_language` (F29): `"off"`
   (default; no change to worker prompts), `"refill"` (context-refill
   replacement questions must be written in plain, non-technical language),
   or `"all"` (that requirement also applies to every question/report a
   regular dispatched worker files via `brief-submit`).

### Per-project Orca binding (only when `window` is `"orca"`)

Skip this whole sub-step - no questions, no `config.json` change to any
project's `orca` field - when `window` (from question 0) is `"console"`.
Never delete a project's existing `orca` object in that case either.

When `window` is `"orca"`, after writing `config.json` above, run `orca
repo list --json` once. Then, for each entry in `config.json`'s `projects`
array (one at a time, in order):

1. Compute its ancestor Orca repos: the `orca repo list` entries whose
   `path` is a strict path-component ancestor of this project's `path` -
   use `is_ancestor_workspace(repo_path, project_path)` from
   `scripts/dispatch.py` (run from the repo/plugin root, e.g.
   `python3 -c "import sys; sys.path.insert(0, 'scripts'); from dispatch
   import is_ancestor_workspace; print(is_ancestor_workspace(sys.argv[1],
   sys.argv[2]))" <repo_path> <project_path>`); an equal path is never an
   ancestor.
2. Build the option list: `"Own repo"`, plus one `"Under <displayName>"`
   per ancestor found (in the order `orca repo list` returned them).
3. If there are no ancestors, only `"Own repo"` is possible: skip asking
   and go straight to the "Own repo" action below.
4. Otherwise ask (AskUserQuestion) "How should <name> open in Orca?" with
   those options. On a rerun, default to the option matching the project's
   current `orca` object if it has one and it still resolves (its
   `repo_id` is still in this run's `orca repo list`), else `"Own repo"`.
5. **"Own repo"** chosen (or auto-selected in step 3): set this project's
   `orca` to `{"mode": "repo"}`, then immediately run `orca repo add
   --path <path> --json`; ignore any failure other than printing one
   warning line.
   **"Under <displayName>"** chosen: set this project's `orca` to
   `{"mode": "bind", "repo_id": "<that repo's id>"}`.
6. Read `config.json`, set this one project's `orca` field, and write the
   whole file back - one project at a time, so an interrupted run never
   leaves a decided project unrecorded.

## Step 4: Agent-side protocol sentence

Print this sentence to the user:

> Dispatched sessions use brief-submit in two situations: whenever they are
> blocked on a decision only the user can make, and once more, right before
> finishing, to report what was done.

Then ask (AskUserQuestion) whether to append this sentence to a rules file.
If the user declines, do not modify any file. If the user accepts, ask for
the target file path and append the sentence to the end of that file's
existing content; never truncate or overwrite what is already there.

## Step 5: Smoke test

Run each check in order and report pass/fail as you go, one check at a time:

1. Confirm the `claude` CLI is on `PATH` (`command -v claude` on POSIX,
   `where claude` on Windows, via Bash). Report found/not found.
2. Append one test `question` record to `inbox.jsonl` using the atomic
   append helper above:
   ```python
   {
     "type": "question",
     "id": str(uuid.uuid4()),
     "project": "agent-brief-me",
     "title": "brief-init smoke test",
     "body": "Automated smoke-test question created by /agent-brief-me:brief-init; safe to ignore.",
     "severity": "low",
     "created_at": "<current UTC time, e.g. datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')>",
   }
   ```
   Report success.
3. Read `inbox.jsonl` back and confirm the last line parses as JSON and its
   `id` matches the one just written. Report success/fail.
4. Append one `status` record per `docs/schema.md`, with `ref` equal to that
   question's `id` and `status: "cancelled"`, using the same atomic append
   helper. Report success.
5. If `inbox.jsonl` was just created empty in Step 1 (first-time setup),
   confirm it now contains exactly these two lines. On a rerun of an
   existing installation, skip this exact-count check (other lines may
   already be present) and just confirm the two smoke-test lines were
   appended.

Setup is complete once all four smoke checks have reported a result.
