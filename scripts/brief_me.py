"""brief-me reference implementation, extracted from skills/brief-me/SKILL.md
so the skill prompt stays short. See docs/schema.md for the file formats.
"""
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


# ---------------------------------------------------------------- CLI ----
# Called by skills/brief-me/SKILL.md so the reference implementation above
# does not have to sit in the skill prompt. Every command prints one JSON
# object to stdout.

def _cmd_load(args):
    brief_home = get_brief_home()
    warning = run_collector(brief_home)
    questions, reports, pending, unread = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
    answers_by_question = fold_answers(os.path.join(brief_home, "answers.jsonl"))
    def q_view(q):
        v = {k: q.get(k) for k in ("id", "project", "title", "body", "severity", "session_id") if q.get(k) is not None}
        v["options"] = build_option_list(q)
        v["age"] = time_ago(q["created_at"])
        return v
    def r_view(r):
        v = {k: r.get(k) for k in ("id", "project", "summary", "severity", "session_id") if r.get(k) is not None}
        v["age"] = time_ago(r["created_at"])
        return v
    return {
        "collector_warning": warning,
        "unread_reports": {p: [r_view(r) for r in sort_reports_by_age(rs)]
                           for p, rs in group_by_project(unread).items()},
        "pending_questions": {p: [q_view(q) for q in sort_questions_by_severity(qs)]
                              for p, qs in group_by_project(pending).items()},
        "unconsumed_projects": unconsumed_projects(questions, answers_by_question),
    }


def _cmd_read(args):
    brief_home = get_brief_home()
    for rid in args:
        atomic_append_line(os.path.join(brief_home, "inbox.jsonl"), build_read_status(rid))
    return {"ok": True, "read": args}


def _cmd_answer(args):
    # answer <question_id> [--chosen TEXT] [--free-text TEXT]
    qid, chosen, free_text = args[0], None, None
    rest = args[1:]
    while rest:
        flag, val, rest = rest[0], rest[1] if len(rest) > 1 else None, rest[2:]
        if flag == "--chosen":
            chosen = val
        elif flag == "--free-text":
            free_text = val
        else:
            return {"ok": False, "error": f"unknown flag {flag}"}
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
        atomic_append_line(os.path.join(brief_home, "inbox.jsonl"),
                           {"type": "status", "ref": qid, "status": "cancelled", "at": _now()})
    return {"ok": True, "dismissed": args}


def _cmd_unconsumed(args):
    brief_home = get_brief_home()
    questions, _, _, _ = fold_inbox(os.path.join(brief_home, "inbox.jsonl"))
    answers_by_question = fold_answers(os.path.join(brief_home, "answers.jsonl"))
    return {"projects": unconsumed_projects(questions, answers_by_question)}


COMMANDS = {"load": _cmd_load, "read": _cmd_read, "answer": _cmd_answer, "dismiss": _cmd_dismiss, "unconsumed": _cmd_unconsumed}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "usage: brief_me.py load | read <report_id>... | answer <qid> [--chosen T] [--free-text T] | dismiss <qid>... | unconsumed"}))
        sys.exit(1)
    print(json.dumps(COMMANDS[sys.argv[1]](sys.argv[2:]), ensure_ascii=False, indent=1))
