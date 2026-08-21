---
name: brief-me
description: Batch-review the agent-brief inbox - show unread reports, walk pending questions grouped by project and severity via AskUserQuestion, then offer to dispatch headless sessions for projects with unconsumed answers. Use when the user runs /brief-me or asks to review, check, or triage the agent-brief inbox.
---

# brief-me

Runs one review pass over `~/.agent-brief/inbox.jsonl` and
`~/.agent-brief/answers.jsonl`. Fixed order, run every step in sequence
(except where a step says to stop early):

0. Run the collector, if one is configured.
1. Fold the inbox. If there is nothing to show, say so and stop.
2. Show unread reports, marking each read as it is shown.
3. Walk pending questions, grouped by project, high severity first.
4. For projects left with an unconsumed answer, offer to dispatch.

Every write below follows the atomic append rule in `docs/schema.md`:
append-mode open, hold an OS-level exclusive lock, write the whole line in a
single `write()` call, release the lock. Nothing in this skill ever rewrites
or truncates an existing line - state is always derived by folding the file,
never stored separately, which is also why leaving `/brief-me` early is safe
(see "Mid-session exit" at the end).

## Reference implementation

Run this as a throwaway script (or `python3 -c "..."`), calling the pieces
described in each step below. `atomic_append_line` mirrors
`tests/test_concurrent_append.py`'s helper and the copy in
`skills/brief-init/SKILL.md` / `skills/brief-submit/SKILL.md` verbatim.

```python
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# The inbox is UTF-8; on Windows, Python < 3.15 defaults stdout to the
# locale code page (e.g. cp950), which mangles non-ASCII record content
# the moment you print it. Force UTF-8 so what you show the user is what
# is actually in the file.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_LINE_BYTES = 8 * 1024
SEVERITY_RANK = {"high": 0, "normal": 1, "low": 2}


def atomic_append_line(path, record):
    """Append one JSON record as one line, per the atomic append rule in
    docs/schema.md: open in append mode, hold an exclusive lock, write the
    whole line in a single write() call, release the lock.
    """
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


def get_brief_home():
    """~/.agent-brief, overridable by BRIEF_HOME - same rule dispatch.py
    uses, so both tools agree on where the inbox lives."""
    return os.environ.get("BRIEF_HOME", os.path.expanduser("~/.agent-brief"))


def iter_lines(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line:
                yield line


def fold_inbox(inbox_path):
    """Fold inbox.jsonl into current state, per docs/schema.md's folding
    rules. Returns (questions, reports, pending_questions, unread_reports):
    - questions: dict id -> question record (every question ever seen)
    - reports: dict id -> report record (every report ever seen)
    - pending_questions: question records with no answered/cancelled status
    - unread_reports: report records with no "read" status yet
    """
    questions = {}
    reports = {}
    q_last_status = {}
    r_read = set()
    for line in iter_lines(inbox_path):
        record = json.loads(line)
        rtype = record["type"]
        if rtype == "question":
            questions[record["id"]] = record
        elif rtype == "report":
            reports[record["id"]] = record
        elif rtype == "status":
            if record["status"] == "read":
                r_read.add(record["ref"])
            else:  # "answered" or "cancelled"; last line for a ref wins
                q_last_status[record["ref"]] = record["status"]
    pending_questions = [q for qid, q in questions.items() if qid not in q_last_status]
    unread_reports = [r for rid, r in reports.items() if rid not in r_read]
    return questions, reports, pending_questions, unread_reports


def fold_answers(answers_path):
    """Map question_id -> last answer record in answers.jsonl (the current
    value per docs/schema.md, including the current `consumed` flag)."""
    result = {}
    for line in iter_lines(answers_path):
        record = json.loads(line)
        result[record["question_id"]] = record
    return result


def group_by_project(records):
    """Group records by their `project` field; sorted by project name so
    display order is deterministic across runs."""
    groups = defaultdict(list)
    for r in records:
        groups[r["project"]].append(r)
    return dict(sorted(groups.items()))


def sort_questions_by_severity(questions):
    """High severity first; ties broken by created_at, oldest first."""
    return sorted(questions, key=lambda q: (SEVERITY_RANK[q["severity"]], q["created_at"]))


def sort_reports_by_age(reports):
    """Oldest first, for deterministic display order within a project."""
    return sorted(reports, key=lambda r: r["created_at"])


def time_ago(created_at):
    """Render a created_at timestamp ("%Y-%m-%dT%H:%M:%SZ") as e.g. "3h ago"."""
    created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    seconds = max(int((datetime.now(timezone.utc) - created).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_read_status(report_id):
    return {"type": "status", "ref": report_id, "status": "read", "at": _now()}


def build_option_list(question):
    """Option order for AskUserQuestion: recommendation first (if any),
    then the remaining choices in array order, always ending in "Skip".
    A choice equal to the recommendation is not repeated."""
    options = []
    seen = set()
    recommendation = question.get("recommendation")
    if recommendation:
        options.append(recommendation)
        seen.add(recommendation)
    for choice in question.get("choices", []):
        if choice not in seen:
            options.append(choice)
            seen.add(choice)
    options.append("Skip")
    return options


def build_answer_and_status(question_id, chosen=None, free_text=None):
    """Build the two records to append after a non-Skip answer. Append the
    answer first, then the status line: an answer line without its status
    line yet is harmless (the question is still correctly pending), the
    reverse would incorrectly mark it answered with nothing recorded."""
    answer = {"question_id": question_id, "answered_at": _now(), "consumed": False}
    if chosen is not None:
        answer["chosen"] = chosen
    if free_text is not None:
        answer["free_text"] = free_text
    status = {"type": "status", "ref": question_id, "status": "answered", "at": _now()}
    return answer, status


def run_collector(brief_home):
    """Run config.json's collector (argv array), cwd=brief_home, before the
    inbox is read. Returns an error string to report on non-zero exit (the
    caller keeps going regardless), or None on success / nothing configured."""
    config_path = os.path.join(brief_home, "config.json")
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    collector = config.get("collector")
    if collector is None:
        return None
    result = subprocess.run(list(collector), cwd=brief_home)
    if result.returncode != 0:
        return f"collector exited with status {result.returncode}"
    return None


def unconsumed_projects(questions, answers_by_question):
    """Sorted, de-duplicated project names with at least one unconsumed
    answer, per docs/schema.md's `consumed` folding rule."""
    projects = set()
    for qid, answer in answers_by_question.items():
        if not answer.get("consumed", False) and qid in questions:
            projects.add(questions[qid]["project"])
    return sorted(projects)
```

## Step 0: Run the collector

Compute `brief_home = get_brief_home()`. Call `run_collector(brief_home)`
*before* touching the inbox at all. If it returns a non-`None` string,
report that failure to the user as a warning; either way, proceed to Step 1
regardless of the result - a failing collector never stops `/brief-me`.

## Step 1: Fold the inbox, check for empty

Call `fold_inbox(os.path.join(brief_home, "inbox.jsonl"))` to get
`questions, reports, pending_questions, unread_reports`.

If `pending_questions` and `unread_reports` are both empty, say plainly that
the inbox is empty and stop here - do not run Step 2, 3, or 4, and do not
invoke `scripts/dispatch.py`.

Otherwise, continue to Step 2.

## Step 2: Show unread reports

Group `unread_reports` with `group_by_project`. Walk the groups in the
returned (alphabetical) project order; within each project's list, walk in
the order `sort_reports_by_age` returns (oldest first).

For each report, in order:

1. Display it under its project's heading, showing `summary` and
   `time_ago(report["created_at"])`.
2. Immediately append its read marker:
   `atomic_append_line(inbox_path, build_read_status(report["id"]))`.

Do this one report at a time (display, then append its read line) rather
than displaying the whole batch and appending afterward, so a report that
was actually shown to the user is never left unmarked if something
interrupts the run partway through.

## Step 3: Walk pending questions

Group `pending_questions` with `group_by_project`. Walk the groups in the
returned (alphabetical) project order; within each project's list, walk in
the order `sort_questions_by_severity` returns (high severity first, ties
broken oldest-first).

For each question, in order:

1. Build its option list: `options = build_option_list(question)`.
2. Present it with the AskUserQuestion tool: use the question's `title` as
   the question header and `body` as the supporting text, and pass
   `options` as the choices (in that order, so a recommendation - when
   present - is the first option and "Skip" is always the last). Mention
   the project and severity when presenting it, so the user can follow the
   project/severity grouping as they go. Do not add a separate "type your
   own answer" option - AskUserQuestion always accepts free text typed
   instead of a listed choice; that is the tool's built-in behavior, not
   something this skill adds. When `choices` and `recommendation` are both
   absent, `options` is just `["Skip"]`, and free text is still available
   through the tool itself.
3. Read what the user did:
   - **They picked "Skip"** - append nothing to either file. The question
     stays pending and will be presented again on the next `/brief-me` run.
     Move on to the next question.
   - **They picked one of the other listed options** - that option's exact
     text is the answer's `chosen` value.
   - **They typed free text instead of picking an option** - that text is
     the answer's `free_text` value. (If the tool reports both a picked
     option and typed text for the same turn, pass both.)
4. For any outcome other than Skip:
   `answer, status = build_answer_and_status(question["id"], chosen=..., free_text=...)`,
   then append `answer` to `answers.jsonl` and `status` to `inbox.jsonl`, in
   that order, both via `atomic_append_line`.

## Step 4: Offer to dispatch

Fold the current answers: `answers_by_question = fold_answers(answers_path)`.
Compute `projects = unconsumed_projects(questions, answers_by_question)`
(`questions` is the dict `fold_inbox` returned in Step 1 - it already has
every question's `project`, no need to re-read the file).

If `projects` is empty, there is nothing to dispatch; finish here.

Otherwise, for each project in `projects` (alphabetical order), ask via
AskUserQuestion whether to dispatch it now (e.g. "Yes" / "No, not now").
Collect the projects the user confirmed.

Once every project has been asked about, if the confirmed list is
non-empty, invoke it in one call from the plugin/repo root (where
`scripts/dispatch.py` lives):

```
python scripts/dispatch.py [--watch] <confirmed-project-1> <confirmed-project-2> ...
```

using the confirmed project names, in the order they were confirmed, as
positional arguments. Pass `--watch` if and only if the skill was invoked
with `--watch` in its arguments (`$ARGUMENTS`): it opens each project as an
interactive `claude` window the user can watch, instead of a headless
`claude -p` session. If no project was confirmed, do not invoke
`scripts/dispatch.py` at all.

`scripts/dispatch.py`'s own contract (looking up each project's path,
marking the answers it used as consumed, logging, skipping unknown project
names) is out of scope here - this skill's job ends at calling it with the
right argv. During F4's own acceptance testing, a stub script recording its
received argv stands in for the real `scripts/dispatch.py` at that same
path to verify this call, without needing a real `claude` binary or real
project checkouts.

## Mid-session exit

Nothing extra to implement for this: every write in this skill is a single
atomic append, and current state is always re-derived by folding the files
(see `docs/schema.md`, "Deriving current state"), never stored elsewhere. If
`/brief-me` is interrupted (or the user closes the session) between two
questions, or before Step 4 finishes asking about every project, no
in-progress state is lost:

- Any question not yet reached, or reached and left without an answer
  because Skip was picked, has no `status` line for it and is still
  `pending` - it appears again, unchanged, the next time `/brief-me` runs.
- Any report already shown got its `read` status line appended right after
  it was displayed (Step 2), so it will not reappear even if the run is cut
  short afterward.
- Any answer already appended is already durably on disk with
  `consumed: false`; a later `/brief-me` run's Step 4 will find it via
  `unconsumed_projects` and offer to dispatch it, even if this run never
  reached Step 4.
