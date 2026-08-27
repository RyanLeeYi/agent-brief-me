---
name: brief-submit
description: Fire-and-forget write to the agent-brief inbox. Use from a dispatched/headless working session in exactly two situations - once when blocked on a decision only the user can make (submit a question), and once more right before finishing, to report what was done (submit a report). Never waits for an answer, never reads answers.jsonl.
---

# brief-submit

Non-blocking delivery into `~/.agent-brief/inbox.jsonl`. This skill has one
contract: validate, write one line, return. It never waits for a reply and
never reads `~/.agent-brief/answers.jsonl` - checking for an answer is a
different skill's job, not this one's. Every call below returns immediately
after the append (and after running `notify`, if configured).

## When to use which entry point

- Blocked on a decision only the user can make -> `submit_question`.
- Finishing work, to report what was done -> `submit_report`.

This mirrors the sentence `brief-init` prints in its Step 3.

## Question payload

Caller supplies a dict with these keys. Do not include `type` or `id` - this
skill generates both.

| key | required | notes |
|---|---|---|
| `project` | yes | non-empty string |
| `title` | yes | non-empty string |
| `body` | no | string; defaults to `""` |
| `choices` | no | array of strings |
| `recommendation` | no | string |
| `multi` | no | `true` when several `choices` may be picked together; the answer's `chosen` then comes back as an array |
| `severity` | no | one of `low`, `normal`, `high`; defaults to `normal` |

### Body template (self-contained rule)

The reader reviews on a phone, with no repo access and none of your
conversation context. A question body must be decidable from the card
alone: write out the information that exists only in your context, and
never reference file paths, code, or discussion the reader cannot see.

Use this template for `body` (drop a line only when it truly does not
apply):

```
Context: <why this decision is needed now - one sentence>
Options:
- <choice 1>: <its consequence - one sentence>
- <choice 2>: <its consequence - one sentence>
Recommendation: <choice> - <the reason - one sentence>
```

The `recommendation` key still carries the bare choice (the UI highlights
it); the reason lives in the body, because a recommendation without its
reason is exactly the context gap this template exists to close.

Rejected (nothing is written, error explains why) when: `project` is
missing/empty, `title` is missing/empty, `severity` is present but not one
of the three allowed values, the payload already contains an `id` key, or
the fully-built record serializes to more than 8192 bytes (8 KiB) including
its trailing newline (the same cap `docs/schema.md` sets for every
`inbox.jsonl` line).

## Report payload

| key | required | notes |
|---|---|---|
| `project` | yes | non-empty string |
| `summary` | yes | non-empty string |
| `severity` | no | one of `low`, `normal`, `high`; omitted from the record if not given |

Same rejection rules as above, with `summary` in place of `title` (`report`
has no `title`/`body`/`choices`/`recommendation` fields per `docs/schema.md`).

## Notify on every submission

`~/.agent-brief/config.json`'s `notify` field is either `null` or an argv
array (e.g. `["python3", "send-telegram.py"]`). After **any** record
(question or report, any severity) is appended, this skill runs that array
exactly once with four trailing arguments, in this order:

| arg | value |
|---|---|
| `project` | the record's `project` |
| `type` | `question` or `report` |
| `severity` | the record's severity; `normal` for a question without one, `none` for a report without one |
| `text` | the question's `title`, or the first line of the report's `summary` |

The notifier decides what to do (e.g. only push `high` questions and
reports). If `notify` is `null`, nothing is run. A failure while running
`notify` does not undo the append and does not turn success into a rejection
(the record is already durably written by that point); it is reported back
as a warning alongside the `ok: true` result.

## Reference implementation

Reuses the atomic append helper from `docs/schema.md` /
`skills/brief-init/SKILL.md` verbatim. Run this as a throwaway script (or
`python3 -c "..."`), calling `submit_question(payload)` or
`submit_report(payload)` with the payload dict built from the caller's
situation; print the JSON result so the calling agent can read it back.

```python
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone

MAX_LINE_BYTES = 8 * 1024
VALID_SEVERITIES = {"low", "normal", "high"}


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


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paths(base):
    base = base or os.path.expanduser("~/.agent-brief")
    return os.path.join(base, "inbox.jsonl"), os.path.join(base, "config.json")


def _reject_common(payload):
    if "id" in payload:
        return "payload must not include id; this skill generates it"
    project = payload.get("project")
    if not isinstance(project, str) or not project:
        return "project is required and must be a non-empty string"
    severity = payload.get("severity")
    if severity is not None and severity not in VALID_SEVERITIES:
        return f"severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}"
    return None


def submit_question(payload, base=None):
    err = _reject_common(payload)
    if err:
        return {"ok": False, "error": err}
    title = payload.get("title")
    if not isinstance(title, str) or not title:
        return {"ok": False, "error": "title is required and must be a non-empty string"}
    record = {
        "type": "question",
        "id": str(uuid.uuid4()),
        "project": payload["project"],
        "title": title,
        "body": payload.get("body", ""),
        "severity": payload.get("severity", "normal"),
        "created_at": _now(),
    }
    _stamp_session(record)
    if "choices" in payload:
        record["choices"] = payload["choices"]
    if payload.get("multi") is True:
        record["multi"] = True
    if "recommendation" in payload:
        record["recommendation"] = payload["recommendation"]
    return _finish(record, base)


def _stamp_session(record):
    """Record the submitting Claude Code session so the user can reopen it
    with `claude --resume <id>` and ask follow-ups. Absent outside Claude."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        record["session_id"] = sid


def submit_report(payload, base=None):
    err = _reject_common(payload)
    if err:
        return {"ok": False, "error": err}
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        return {"ok": False, "error": "summary is required and must be a non-empty string"}
    record = {
        "type": "report",
        "id": str(uuid.uuid4()),
        "project": payload["project"],
        "summary": summary,
        "created_at": _now(),
    }
    _stamp_session(record)
    if payload.get("severity") is not None:
        record["severity"] = payload["severity"]
    return _finish(record, base)


def _finish(record, base):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        return {"ok": False, "error": "record exceeds 8 KB limit"}
    inbox_path, config_path = _paths(base)
    atomic_append_line(inbox_path, record)
    result = {"ok": True, "id": record["id"]}
    warning = _run_notify(config_path, record)
    if warning:
        result["warning"] = warning
    return result


def _run_notify(config_path, record):
    if not os.path.exists(config_path):
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    notify = config.get("notify")
    if notify is None:
        return None
    is_q = record["type"] == "question"
    severity = record.get("severity") or ("normal" if is_q else "none")
    text = record["title"] if is_q else record["summary"].splitlines()[0]
    try:
        subprocess.run(list(notify) + [record["project"], record["type"], severity, text], check=False)
    except OSError as exc:
        return f"notify failed to run: {exc}"
    return None
```

## Usage

1. Build the payload dict for your situation (question or report).
2. Run the reference implementation above with `submit_question(payload)` or
   `submit_report(payload)` appended, e.g. via
   `python3 -c "<script>; print(json.dumps(submit_question({...})))"` or a
   short throwaway `.py` file, and read the printed JSON result.
3. If `result["ok"]` is `false`, fix the payload per `result["error"]` and
   retry; nothing was written by the rejected call.
4. If `result["ok"]` is `true`, note `result["id"]` if you need to refer to
   this question or report later. Do not poll `inbox.jsonl` or
   `answers.jsonl` for a reply, and do not wait: this skill's job ends the
   moment it returns.
