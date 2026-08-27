"""brief-me reference implementation, extracted from skills/brief-me/SKILL.md
so the skill prompt stays short. See docs/schema.md for the file formats.

Also implements `serve`: a stdlib http.server backend for scripts/brief_me.html
(F9), 127.0.0.1-only. See docs/schema.md for the `reopened` status this adds.
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The inbox is UTF-8; on Windows, Python < 3.15 defaults stdout to the
# locale code page (e.g. cp950), which mangles non-ASCII record content
# the moment you print it. Force UTF-8 so what you show the user is what
# is actually in the file.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_LINE_BYTES = 8 * 1024
SEVERITY_RANK = {"high": 0, "normal": 1, "low": 2}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8765


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


def load_config(brief_home):
    """~/.agent-brief/config.json, or {} if it doesn't exist yet."""
    path = os.path.join(brief_home, "config.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fold_inbox(inbox_path):
    """Fold inbox.jsonl into current state, per docs/schema.md's folding
    rules. Returns (questions, reports, pending_questions, unread_reports,
    q_last_status):
    - questions: dict id -> question record (every question ever seen)
    - reports: dict id -> report record (every report ever seen)
    - pending_questions: question records whose last status line is none,
      or "reopened" (a "reopened" question folds back to pending)
    - unread_reports: report records with no "read" status yet
    - q_last_status: dict ref -> last answered/cancelled/reopened status
      record for that ref (the raw {"type": "status", ...} line), so
      callers needing more than pending/answered (e.g. the API's
      "dismissed" list, which needs the cancelled line's `at`) don't have
      to re-fold the file.
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
            else:  # "answered", "cancelled", or "reopened"; last line for a ref wins
                q_last_status[record["ref"]] = record
    pending_questions = [
        q for qid, q in questions.items()
        if qid not in q_last_status or q_last_status[qid]["status"] == "reopened"
    ]
    unread_reports = [r for rid, r in reports.items() if rid not in r_read]
    return questions, reports, pending_questions, unread_reports, q_last_status


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


def _parse_utc(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _report_times(inbox_path):
    """{project: [created_at datetime, ...]} for every report in the inbox (F15).
    A session counts as ended once a report for its project is filed at or
    after its started_at - a watch-mode claude window stays alive at the
    prompt after the work is done, so pid liveness alone never ends it."""
    out = {}
    for line in iter_lines(inbox_path):
        try:
            rec = json.loads(line)
            if rec.get("type") != "report":
                continue
            out.setdefault(rec.get("project"), []).append(_parse_utc(rec["created_at"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _report_ended_at(reports, project, started_at):
    """Earliest report for `project` at/after `started_at`, or None."""
    later = [t for t in reports.get(project, []) if t >= started_at]
    return min(later) if later else None


def build_status(ref, status):
    """Build a {"type": "status", ...} line for any of the four status
    values (answered/cancelled/read/reopened); `at` is now()."""
    return {"type": "status", "ref": ref, "status": status, "at": _now()}


def build_option_list(question):
    """Option order for AskUserQuestion: recommendation first (if any),
    then the remaining choices in array order, always ending in "Dismiss"
    (drop for good) and "Skip" (ask again next time).
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
    options.append("Dismiss")
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
    status = build_status(question_id, "answered")
    return answer, status


def run_collector(brief_home):
    """Run config.json's collector (argv array), cwd=brief_home, before the
    inbox is read. Returns an error string to report on non-zero exit (the
    caller keeps going regardless), or None on success / nothing configured."""
    collector = load_config(brief_home).get("collector")
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


def question_view(q):
    v = {k: q.get(k) for k in ("id", "project", "title", "body", "severity", "session_id", "multi") if q.get(k) is not None}
    # API/JSON consumers render Dismiss/Skip themselves; only real choices here.
    v["options"] = [o for o in build_option_list(q) if o not in ("Dismiss", "Skip")]
    v["age"] = time_ago(q["created_at"])
    return v


_REPORT_PART_SPLIT_RE = re.compile(r"[；;]")  # full-width or half-width semicolon
_REPORT_FEATURE_ID_RE = re.compile(r"F(\d+)")
_REPORT_STATUS_RE = re.compile(r"\b(passing|failing|blocked)\b", re.IGNORECASE)


def _split_report_parts(summary):
    """Split `summary` on up to two semicolons (full-width or half-width)
    into (done, blocked, handoff); a missing trailing segment is None. No
    semicolon at all means the report predates the three-part convention,
    so all three come back None and callers keep showing raw `summary`."""
    segments = [s.strip() for s in _REPORT_PART_SPLIT_RE.split(summary, maxsplit=2)]
    if len(segments) == 1:
        return None, None, None
    segments += [None] * (3 - len(segments))
    return tuple(segments[:3])


def _compress_feature_ids(ids):
    """[11, 12, 13, 15] -> "F11-F13 F15": consecutive runs collapse to a range."""
    ranges = []
    start = prev = ids[0]
    for n in ids[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((start, prev))
        start = prev = n
    ranges.append((start, prev))
    return " ".join(f"F{a}" if a == b else f"F{a}-F{b}" for a, b in ranges)


def _feature_chip(done, summary):
    """`F11-F15 passing`-style chip built from the done segment (or the
    whole summary when there was no three-part split); None when either an
    F-id or a terminal status keyword is missing."""
    source = done or summary
    ids = sorted(set(int(n) for n in _REPORT_FEATURE_ID_RE.findall(source)))
    status = _REPORT_STATUS_RE.search(source)
    if not ids or not status:
        return None
    return _compress_feature_ids(ids) + " " + status.group(1).lower()


def report_view(r):
    v = {k: r.get(k) for k in ("id", "project", "summary", "severity", "session_id") if r.get(k) is not None}
    v["age"] = time_ago(r["created_at"])
    done, blocked, handoff = _split_report_parts(r["summary"])
    v["parts"] = {"done": done, "blocked": blocked, "handoff": handoff}
    v["feature_chip"] = _feature_chip(done, r["summary"])
    return v


def _state(brief_home, collector_warning):
    """Shared question/report state used by both CLI `load` and GET
    /api/state. Returns (payload, questions, q_last_status,
    answers_by_question) so callers needing more than `payload` (the API's
    dismissed list / unconsumed_counts) don't have to re-fold the files."""
    inbox_path = os.path.join(brief_home, "inbox.jsonl")
    questions, reports, pending, unread, q_last_status = fold_inbox(inbox_path)
    answers_by_question = fold_answers(os.path.join(brief_home, "answers.jsonl"))
    payload = {
        "collector_warning": collector_warning,
        "unread_reports": {p: [report_view(r) for r in sort_reports_by_age(rs)]
                           for p, rs in group_by_project(unread).items()},
        "pending_questions": {p: [question_view(q) for q in sort_questions_by_severity(qs)]
                              for p, qs in group_by_project(pending).items()},
        "unconsumed_projects": unconsumed_projects(questions, answers_by_question),
    }
    return payload, questions, q_last_status, answers_by_question


# ---------------------------------------------------------------- CLI ----
# Called by skills/brief-me/SKILL.md so the reference implementation above
# does not have to sit in the skill prompt. Every command prints one JSON
# object to stdout.

def _cmd_load(args):
    brief_home = get_brief_home()
    warning = run_collector(brief_home)
    requeued = _requeue_stale_batches(brief_home)  # F24, before _state() so
    # unconsumed_projects/unconsumed_counts reflect what was just requeued.
    payload, _, _, _ = _state(brief_home, warning)
    payload["requeued"] = requeued
    return payload


def _cmd_read(args):
    brief_home = get_brief_home()
    for rid in args:
        atomic_append_line(os.path.join(brief_home, "inbox.jsonl"), build_status(rid, "read"))
    return {"ok": True, "read": args}


def _cmd_answer(args):
    # answer <question_id> [--chosen TEXT]... [--free-text TEXT]
    # --chosen may repeat for multi: true questions; one value stays a string.
    qid, chosen_list, free_text = args[0], [], None
    rest = args[1:]
    while rest:
        flag, val, rest = rest[0], rest[1] if len(rest) > 1 else None, rest[2:]
        if flag == "--chosen":
            chosen_list.append(val)
        elif flag == "--free-text":
            free_text = val
        else:
            return {"ok": False, "error": f"unknown flag {flag}"}
    chosen = None if not chosen_list else (chosen_list[0] if len(chosen_list) == 1 else chosen_list)
    if chosen is None and free_text is None:
        return {"ok": False, "error": "need --chosen and/or --free-text"}
    brief_home = get_brief_home()
    answer, status = build_answer_and_status(qid, chosen=chosen, free_text=free_text)
    atomic_append_line(os.path.join(brief_home, "answers.jsonl"), answer)
    atomic_append_line(os.path.join(brief_home, "inbox.jsonl"), status)
    return {"ok": True, "question_id": qid}


def _cmd_dismiss(args):
    """Drop questions for good: append a cancelled status, write no answer."""
    brief_home = get_brief_home()
    for qid in args:
        atomic_append_line(os.path.join(brief_home, "inbox.jsonl"), build_status(qid, "cancelled"))
    return {"ok": True, "dismissed": args}


def _cmd_unconsumed(args):
    brief_home = get_brief_home()
    questions, _, _, _, _ = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
    answers_by_question = fold_answers(os.path.join(brief_home, "answers.jsonl"))
    return {"projects": unconsumed_projects(questions, answers_by_question)}


COMMANDS = {"load": _cmd_load, "read": _cmd_read, "answer": _cmd_answer, "dismiss": _cmd_dismiss, "unconsumed": _cmd_unconsumed}


# ------------------------------------------------------------- Web UI ----
# GET / and the /api/* JSON endpoints backing scripts/brief_me.html (F9).
# This is the F8/F9 API contract: F9 (and tests/_stub_api.py) must not
# extend it independently.

_DISPATCHED_WITH_LOG_RE = re.compile(r"^(.+): dispatched, log=(.+)$")
_DISPATCHED_RE = re.compile(r"^(.+): dispatched$")
_OPENED_WINDOW_RE = re.compile(r"^(.+): opened in new window$")


def _api_state(brief_home):
    """GET /api/state: like `load` but never runs the collector
    (collector_warning is always null), plus `projects`,
    `unconsumed_counts`, and `dismissed` for the Web UI."""
    payload, questions, q_last_status, answers_by_question = _state(brief_home, None)

    dismissed_by_project = defaultdict(list)
    for qid, q in questions.items():
        last = q_last_status.get(qid)
        if last and last["status"] == "cancelled":
            v = question_view(q)
            # Early status lines (brief-init smoke test, 2026-08-20) carry
            # created_at instead of at; tolerate both, never 500 on old data.
            v["dismissed_at"] = last.get("at") or last.get("created_at")
            dismissed_by_project[q["project"]].append((q, v))
    payload["dismissed"] = {
        p: [v for _, v in sorted(items, key=lambda t: (SEVERITY_RANK[t[0]["severity"]], t[0]["created_at"]))]
        for p, items in sorted(dismissed_by_project.items())
    }

    config = load_config(brief_home)
    payload["projects"] = [p["name"] for p in config.get("projects", [])]

    unconsumed_counts = {}
    for qid, answer in answers_by_question.items():
        if not answer.get("consumed", False) and qid in questions:
            proj = questions[qid]["project"]
            unconsumed_counts[proj] = unconsumed_counts.get(proj, 0) + 1
    payload["unconsumed_counts"] = unconsumed_counts
    dispatches_path = os.path.join(brief_home, "dispatches.jsonl")
    reports = _report_times(os.path.join(brief_home, "inbox.jsonl"))
    payload["running"] = running_sessions(dispatches_path, reports)
    projects_by_path = {p["name"]: p["path"] for p in config.get("projects", [])}
    payload["sessions"] = sessions_view(dispatches_path, projects_by_path, reports)

    return payload


def _pid_alive(pid):
    """False only when we can prove the pid is gone; unknown counts as alive."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return (not ok) or code.value == 259
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def running_sessions(path, reports=None):
    """{project: {started_at, elapsed_seconds}} for started sessions whose
    batch has no finished line, whose pid is still alive, and (F15) for
    which no report has been filed since started_at."""
    reports = reports or {}
    if not os.path.exists(path):
        return {}
    started, finished = [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "started":
                started.append(rec)
            elif rec.get("type") == "finished":
                finished.add(rec.get("batch_id"))
    now = datetime.now(timezone.utc)
    out = {}
    for rec in started:
        if rec.get("batch_id") in finished or not _pid_alive(int(rec.get("pid", 0))):
            continue
        try:
            t0 = datetime.strptime(rec["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if _report_ended_at(reports, rec.get("project"), t0) is not None:
            continue
        out[rec["project"]] = {"started_at": rec["started_at"],
                               "elapsed_seconds": int((now - t0).total_seconds())}
    return out


SESSION_WINDOW_SECONDS = 7 * 24 * 3600  # F14: hide `finished` sessions older than this


def _project_current(path):
    """`current` for a running session (F14): {"feature": <content>, "mtime":
    <ISO UTC>} from <path>/.harness/current_feature, or None if the project
    path is unknown or the file doesn't exist.

    F20 adds two more best-effort fields, each omitted (not None/0) when its
    source is unavailable so the caller can tell "no signal" apart from
    "zero": `passing`/`total` (feature counts from <path>/feature_list.json,
    for the sessions progress bar) and `last_tool` (the last tool name in
    <path>/.harness/trace.jsonl, for the sessions "last tool" line)."""
    if not path:
        return None
    cf_path = os.path.join(path, ".harness", "current_feature")
    try:
        with open(cf_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        mtime = datetime.fromtimestamp(os.path.getmtime(cf_path), tz=timezone.utc)
    except OSError:
        return None
    result = {"feature": content, "mtime": mtime.strftime("%Y-%m-%dT%H:%M:%SZ")}

    try:
        with open(os.path.join(path, "feature_list.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        features = data.get("features", []) if isinstance(data, dict) else []
        result["total"] = len(features)
        result["passing"] = sum(1 for feat in features if isinstance(feat, dict) and feat.get("status") == "passing")
    except (OSError, ValueError):
        pass

    last_tool = None
    for line in iter_lines(os.path.join(path, ".harness", "trace.jsonl")):
        try:
            last_tool = json.loads(line).get("tool") or last_tool
        except ValueError:
            continue
    if last_tool:
        result["last_tool"] = last_tool

    return result


def _load_batches(path):
    """Parse dispatches.jsonl (F14) into batch_id -> {"started": [rec, ...],
    "finished": rec or None}, in file order. Malformed lines are skipped -
    never 500 on a broken dispatches.jsonl."""
    batches = {}
    for line in iter_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        batch_id = rec.get("batch_id")
        if batch_id is None:
            continue
        entry = batches.setdefault(batch_id, {"started": [], "finished": None})
        if rec.get("type") == "started":
            entry["started"].append(rec)
        elif rec.get("type") == "finished":
            entry["finished"] = rec
    return batches


def _requeued_batch_ids(path):
    """Set of batch_ids that already have a "requeued" line in
    dispatches.jsonl (F24's dedup condition). Malformed lines are skipped,
    same as _load_batches."""
    out = set()
    for line in iter_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "requeued" and rec.get("batch_id") is not None:
            out.add(rec["batch_id"])
    return out


def _task_title(task):
    """A tasks[] element's title: the "title" field of the F23 object
    shape, or the element itself for a pre-F23 plain string."""
    return task.get("title") if isinstance(task, dict) else task


def _requeue_candidates(brief_home):
    """F24: which started dispatch batches should requeue their consumed
    answers, per docs/schema.md's "Requeue" rule. A batch qualifies once
    all four hold: (a) at least one of its started records has a non-empty
    `tasks` list (a missing `tasks` key folds to [], so a legacy batch
    never qualifies); (b) the session has ended - the batch has a
    `finished` line, or every started record's pid is dead; (c) no report
    has been filed for any of the batch's projects at or after that
    record's `started_at`; (d) no `requeued` line for this batch_id yet.

    Returns [(batch_id, [(question_id, project, title), ...]), ...] for
    qualifying batches only. question_id is resolved by matching a task's
    (project, title) against the last answered question whose folded
    answer is still `consumed: true` - dispatch.py's own definition of
    "this title was consumed" (docs/schema.md's "Collector idempotency").
    A task that cannot be resolved this way is dropped; the batch still
    gets its `requeued` line so it is not retried forever.
    """
    dispatches_path = os.path.join(brief_home, "dispatches.jsonl")
    batches = _load_batches(dispatches_path)
    if not batches:
        return []
    already_requeued = _requeued_batch_ids(dispatches_path)
    inbox_path = os.path.join(brief_home, "inbox.jsonl")
    reports = _report_times(inbox_path)
    questions, _, _, _, q_last_status = fold_inbox(inbox_path)
    answers_by_question = fold_answers(os.path.join(brief_home, "answers.jsonl"))

    resolved = {}
    for qid, q in questions.items():
        status = q_last_status.get(qid)
        if not status or status["status"] != "answered":
            continue
        answer = answers_by_question.get(qid)
        if not answer or not answer.get("consumed", False):
            continue
        resolved[(q["project"], q["title"])] = qid

    out = []
    for batch_id, batch in batches.items():
        if batch_id in already_requeued:
            continue
        started = batch["started"]
        if not started or not any(rec.get("tasks", []) for rec in started):
            continue

        if batch["finished"] is None:
            try:
                ended = all(not _pid_alive(int(rec.get("pid", 0))) for rec in started)
            except (TypeError, ValueError):
                ended = False
            if not ended:
                continue

        no_report = True
        for rec in started:
            try:
                t0 = _parse_utc(rec["started_at"])
            except (KeyError, ValueError):
                no_report = False
                break
            if _report_ended_at(reports, rec.get("project"), t0) is not None:
                no_report = False
                break
        if not no_report:
            continue

        matched, seen_qids = [], set()
        for rec in started:
            project = rec.get("project")
            for task in rec.get("tasks", []):
                qid = resolved.get((project, _task_title(task)))
                if qid is None or qid in seen_qids:
                    continue
                seen_qids.add(qid)
                matched.append((qid, project, _task_title(task)))
        out.append((batch_id, matched))
    return out


def _requeue_stale_batches(brief_home):
    """Perform F24's requeue for every qualifying batch (_requeue_candidates):
    for each resolved (question_id, project, title), append a copy of that
    question's last answer with `consumed` flipped back to false and
    `answered_at` reset to now, then append one `requeued` line per batch.
    Returns {project: [title, ...]} for what was just requeued (a task that
    could not be resolved to a question is omitted, not just its title)."""
    dispatches_path = os.path.join(brief_home, "dispatches.jsonl")
    answers_path = os.path.join(brief_home, "answers.jsonl")
    answers_by_question = fold_answers(answers_path)

    requeued = defaultdict(list)
    for batch_id, matched in _requeue_candidates(brief_home):
        for qid, project, title in matched:
            last_answer = answers_by_question.get(qid)
            if last_answer is None:
                continue
            new_answer = {**last_answer, "consumed": False, "answered_at": _now()}
            atomic_append_line(answers_path, new_answer)
            requeued[project].append(title)
        atomic_append_line(dispatches_path, {"type": "requeued", "batch_id": batch_id, "at": _now()})
    return dict(requeued)


_BLOCKED_SPLIT_RE = re.compile(r"[;；]")  # ";" or full-width "；"


def _report_note(summary):
    """F20's Tasks-column subtext for a finished row with a linked report:
    the "blocked" middle segment of a three-part report summary (id/state;
    blocked reason; handoff path - see agents.md's report convention), or
    the summary's first line when it isn't three parts.

    Deliberately re-splits the raw summary here rather than depending on
    report_view()'s (still in-flight, different-owner) `parts` field."""
    parts = _BLOCKED_SPLIT_RE.split(summary or "", maxsplit=2)
    if len(parts) >= 2 and parts[1].strip():
        return parts[1].strip()
    return (summary or "").split("\n", 1)[0]


def _reports_with_ids(inbox_path):
    """{project: [(created_at, id, summary), ...]} for every report in the
    inbox (F20) - like _report_times(), but also keeping id/summary so a
    finished session row can link to (and preview) the report that likely
    explains it. Kept separate from _report_times() since that function is
    also used by running_sessions(), outside this feature's ownership."""
    out = {}
    for line in iter_lines(inbox_path):
        try:
            rec = json.loads(line)
            if rec.get("type") != "report":
                continue
            out.setdefault(rec.get("project"), []).append(
                (_parse_utc(rec["created_at"]), rec.get("id"), rec.get("summary")))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _nearest_report(reports_with_id, project, anchor):
    """(id, summary) of the earliest report for `project` at/after `anchor`,
    or (None, None). Mirrors _report_ended_at()'s earliest-at-or-after
    selection, so a session ended_by="report" always links to the exact
    report that ended it (same anchor, same tie-break)."""
    later = sorted(t for t in reports_with_id.get(project, []) if t[0] >= anchor)
    return (later[0][1], later[0][2]) if later else (None, None)


def _finished_outcome(ended_by, exit_code):
    """F20 outcome classification for a finished session row: legible labels
    instead of a raw platform exit code (4294967295/-1 for a killed
    process, 3221225786 for Ctrl-C - both meaningless at a glance)."""
    if ended_by == "report":
        return "report"
    if exit_code is None:
        return "unknown"
    if exit_code in (4294967295, -1):
        return "killed"
    if exit_code == 3221225786:
        return "ctrl_c"
    return "ok" if exit_code == 0 else "exit"


def sessions_view(path, projects_by_path, reports=None):
    """GET /api/state's `sessions` field (F14): {running: [...], finished:
    [...], finished_hidden: N, finished_counts: {report, killed}} (N =
    batches whose finished_at is older than SESSION_WINDOW_SECONDS;
    finished_counts is a tally of `finished`'s outcome field, F20, with
    "killed" including "ctrl_c"). `running` uses the same alive-batch
    condition as running_sessions(); `finished` is sorted newest-first. A
    `started` record with no `tasks` key (written before this field existed)
    folds to []. Each `finished` row also carries F20's `outcome` and
    `report_id` (the report, if any, that most likely explains it - see
    _nearest_report())."""
    now = datetime.now(timezone.utc)
    reports = reports or {}
    reports_with_id = _reports_with_ids(os.path.join(os.path.dirname(path), "inbox.jsonl"))
    running, finished, hidden = [], [], 0
    for batch_id, batch in _load_batches(path).items():
        fin = batch["finished"]
        if fin is None:
            for rec in batch["started"]:
                try:
                    pid = int(rec.get("pid", 0))
                    t0 = _parse_utc(rec["started_at"])
                except (KeyError, ValueError):
                    continue
                if not _pid_alive(pid):
                    continue
                ended = _report_ended_at(reports, rec.get("project"), t0)
                if ended is not None:
                    if (now - ended).total_seconds() > SESSION_WINDOW_SECONDS:
                        hidden += 1
                        continue
                    report_id, report_summary = _nearest_report(reports_with_id, rec.get("project"), t0)
                    finished.append({
                        "batch_id": batch_id, "project": rec.get("project"), "pid": pid,
                        "started_at": rec["started_at"],
                        "finished_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "duration_seconds": int((ended - t0).total_seconds()),
                        "exit_code": None, "ended_by": "report",
                        "tasks": rec.get("tasks", []), "log": rec.get("log"),
                        "outcome": "report", "report_id": report_id,
                        "report_note": _report_note(report_summary) if report_id else None,
                    })
                    continue
                running.append({
                    "batch_id": batch_id, "project": rec.get("project"), "pid": pid,
                    "started_at": rec["started_at"],
                    "elapsed_seconds": int((now - t0).total_seconds()),
                    "tasks": rec.get("tasks", []), "log": rec.get("log"),
                    "current": _project_current(projects_by_path.get(rec.get("project"))),
                })
            continue

        try:
            finished_at = _parse_utc(fin["finished_at"])
        except (KeyError, ValueError):
            continue
        if (now - finished_at).total_seconds() > SESSION_WINDOW_SECONDS:
            hidden += 1
            continue
        exit_codes = fin.get("exit_codes", [])
        for i, rec in enumerate(batch["started"]):
            try:
                t0 = _parse_utc(rec["started_at"])
                duration = int((finished_at - t0).total_seconds())
            except (KeyError, ValueError):
                duration = None
            exit_code = exit_codes[i] if i < len(exit_codes) else None
            report_id, report_summary = _nearest_report(reports_with_id, rec.get("project"), finished_at)
            finished.append({
                "batch_id": batch_id, "project": rec.get("project"), "pid": rec.get("pid"),
                "started_at": rec.get("started_at"), "finished_at": fin["finished_at"],
                "duration_seconds": duration,
                "exit_code": exit_code,
                "ended_by": "exit",
                "tasks": rec.get("tasks", []), "log": rec.get("log"),
                "outcome": _finished_outcome("exit", exit_code), "report_id": report_id,
                "report_note": _report_note(report_summary) if report_id else None,
            })
    finished.sort(key=lambda s: s["finished_at"], reverse=True)
    finished_counts = {"report": 0, "killed": 0}
    for row in finished:
        if row["outcome"] == "report":
            finished_counts["report"] += 1
        elif row["outcome"] in ("killed", "ctrl_c"):
            finished_counts["killed"] += 1
    return {"running": running, "finished": finished, "finished_hidden": hidden,
            "finished_counts": finished_counts}


def _run_dispatch(brief_home, projects, watch):
    """Invoke scripts/dispatch.py per the F8 API contract. Never raises on
    a non-zero dispatch.py exit - that becomes dispatch_error, not an
    exception, per the contract."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "dispatch.py")]
    cmd.append("--watch" if watch else "--no-watch")  # explicit: UI value overrides config default
    cmd.extend(projects)
    result = subprocess.run(cmd, capture_output=True, text=True)

    dispatched, log_paths = [], []
    for line in result.stdout.splitlines():
        m = _DISPATCHED_WITH_LOG_RE.match(line)
        if m:
            dispatched.append(m.group(1))
            log_paths.append(m.group(2))
            continue
        m = _DISPATCHED_RE.match(line) or _OPENED_WINDOW_RE.match(line)
        if m:
            dispatched.append(m.group(1))

    out = {"dispatched": dispatched, "log_paths": log_paths}
    if result.returncode != 0:
        out["dispatch_error"] = (result.stdout + result.stderr)[-2000:]
    return out


def _run_refill(brief_home, question_id):
    """Invoke `scripts/dispatch.py --refill <question_id>` (F28) and pass its
    JSON result straight through. Never raises on a non-zero dispatch.py
    exit or malformed stdout - both become an {"ok": False, "error": ...}
    result instead."""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "dispatch.py"), "--refill", question_id]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (result.stdout + result.stderr)[-2000:]}


def _validate_answer_body(body, questions):
    """Returns an error string, or None if `body` is a valid /api/answer
    request per the F8 acceptance's 400 rules (checked in full before any
    write happens)."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    answers = body.get("answers", [])
    dismiss = body.get("dismiss", [])
    if not isinstance(answers, list) or not isinstance(dismiss, list):
        return "answers and dismiss must be arrays"
    for entry in answers:
        if not isinstance(entry, dict) or not entry.get("id"):
            return "each answer needs an id"
        has_chosen = entry.get("chosen") is not None
        free_text = entry.get("free_text")
        has_free_text = free_text is not None and str(free_text).strip() != ""
        if not has_chosen and not has_free_text:
            return f"answer {entry['id']} needs chosen and/or free_text"
    answer_ids = [e["id"] for e in answers]
    if set(answer_ids) & set(dismiss):
        return f"id in both answers and dismiss: {sorted(set(answer_ids) & set(dismiss))[0]}"
    unknown = [i for i in answer_ids + list(dismiss) if i not in questions]
    if unknown:
        return f"unknown question id: {unknown[0]}"
    return None


class BriefHTTPHandler(BaseHTTPRequestHandler):
    """127.0.0.1-only backend for scripts/brief_me.html (F9). GET / serves
    the html file; GET/POST /api/* implement the F8 API contract."""

    def log_message(self, format, *args):
        pass  # keep test/CLI output quiet; errors still surface as JSON

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        """Returns (data, error): data is a dict on success, error is a
        user-facing string ("body must be a JSON object") otherwise. A
        missing/empty body is treated as {}."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}, None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, "body must be a JSON object"
        if not isinstance(data, dict):
            return None, "body must be a JSON object"
        return data, None

    def _serve_index(self):
        html_path = os.path.join(SCRIPT_DIR, "brief_me.html")
        try:
            with open(html_path, "rb") as f:
                body = f.read()
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._serve_index()
            elif path == "/api/state":
                self._json(200, _api_state(get_brief_home()))
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            handler = {
                "/api/collect": self._post_collect,
                "/api/read": self._post_read,
                "/api/answer": self._post_answer,
                "/api/reopen": self._post_reopen,
                "/api/refill": self._post_refill,
            }.get(path)
            if handler is None:
                self._json(404, {"ok": False, "error": "not found"})
                return
            handler()
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})

    def _post_collect(self):
        body, err = self._read_json_body()
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        brief_home = get_brief_home()
        warning = run_collector(brief_home)
        self._json(200, {"ok": True, "collector_warning": warning})

    def _post_read(self):
        body, err = self._read_json_body()
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            self._json(400, {"ok": False, "error": "ids must be an array"})
            return
        brief_home = get_brief_home()
        _, reports, _, _, _ = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
        unknown = [i for i in ids if i not in reports]
        if unknown:
            self._json(400, {"ok": False, "error": f"unknown report id: {unknown[0]}"})
            return
        inbox_path = os.path.join(brief_home, "inbox.jsonl")
        for rid in ids:
            atomic_append_line(inbox_path, build_status(rid, "read"))
        self._json(200, {"ok": True, "read": ids})

    def _post_reopen(self):
        body, err = self._read_json_body()
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            self._json(400, {"ok": False, "error": "ids must be an array"})
            return
        brief_home = get_brief_home()
        questions, _, _, _, _ = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
        unknown = [i for i in ids if i not in questions]
        if unknown:
            self._json(400, {"ok": False, "error": f"unknown question id: {unknown[0]}"})
            return
        inbox_path = os.path.join(brief_home, "inbox.jsonl")
        for qid in ids:
            atomic_append_line(inbox_path, build_status(qid, "reopened"))
        self._json(200, {"ok": True, "reopened": ids})

    def _post_refill(self):
        """POST /api/refill {"id": <question_id>} (F28): dispatch a
        context-refill session for one pending question. `id` unknown to
        dispatch.py's --refill is the only 400; an already-running refill
        for this question is a 200 with `already_running: true` (dedup,
        not an error)."""
        body, err = self._read_json_body()
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        qid = body.get("id")
        if not isinstance(qid, str) or not qid:
            self._json(400, {"ok": False, "error": "id is required"})
            return
        result = _run_refill(get_brief_home(), qid)
        self._json(200 if result.get("ok") else 400, result)

    def _post_answer(self):
        body, err = self._read_json_body()
        if err:
            self._json(400, {"ok": False, "error": err})
            return
        brief_home = get_brief_home()
        questions, _, _, _, _ = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
        err = _validate_answer_body(body, questions)
        if err:
            self._json(400, {"ok": False, "error": err})
            return

        answers = body.get("answers", [])
        dismiss = body.get("dismiss", [])
        inbox_path = os.path.join(brief_home, "inbox.jsonl")
        answers_path = os.path.join(brief_home, "answers.jsonl")
        for entry in answers:
            answer, status = build_answer_and_status(
                entry["id"], chosen=entry.get("chosen"), free_text=entry.get("free_text"))
            atomic_append_line(answers_path, answer)
            atomic_append_line(inbox_path, status)
        for qid in dismiss:
            atomic_append_line(inbox_path, build_status(qid, "cancelled"))

        result = {"ok": True, "saved": len(answers), "dismissed": len(dismiss),
                  "dispatched": [], "log_paths": []}
        dispatch = body.get("dispatch")
        if dispatch:
            projects = dispatch.get("projects") or []
            watch = bool(dispatch.get("watch"))
            result.update(_run_dispatch(brief_home, projects, watch))
        self._json(200, result)


def make_server(host="127.0.0.1", port=DEFAULT_PORT):
    """Bind (not yet serving) the Web UI HTTP server. port=0 picks a free
    ephemeral port - tests read it back from server.server_address[1]."""
    return ThreadingHTTPServer((host, port), BriefHTTPHandler)


def _run_serve(args):
    port, host = DEFAULT_PORT, "127.0.0.1"
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--host" and i + 1 < len(args):
            # Opt-in only. The API has no authentication: bind to a LAN/VPN
            # address (or 0.0.0.0) only on a network you trust end to end.
            host = args[i + 1]
            i += 2
        else:
            i += 1
    server = make_server(host=host, port=port)
    print(f"agent-brief-me serving on http://{host}:{server.server_address[1]}/ "
          f"(BRIEF_HOME={get_brief_home()})")
    if host != "127.0.0.1":
        print("warning: --host exposes an unauthenticated API beyond localhost")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        _run_serve(sys.argv[2:])
    elif len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "usage: brief_me.py serve [--port N] [--host H] | load | read <report_id>... | answer <qid> [--chosen T]... [--free-text T] | dismiss <qid>... | unconsumed"}))
        sys.exit(1)
    else:
        print(json.dumps(COMMANDS[sys.argv[1]](sys.argv[2:]), ensure_ascii=False, indent=1))
