"""Example collector: file review items from a per-repo `feature_list.json`.

This is the author's own collector, shipped as a worked example. It assumes a
specific to-do format; adapt the `scan_project` function (or write your own
collector from scratch - see README "Extending") if yours differs.

Assumed format - `<project>/feature_list.json` at the repo root:

    {
      "features": [
        {"id": "F12", "title": "...", "status": "failing", "signed_off": true},
        {"id": "F13", "title": "...", "status": "passing"},
        ...
      ]
    }

  - `status`      "failing" (open) or "passing" (done). Only failing entries count.
  - `signed_off`  true  -> the spec is approved; the entry is ready for a worker.
                  false -> filed as a "[sign-off]" question so the user approves it.
                  absent -> legacy entry, ignored (counted in a stderr notice).
  - `id`, `title` used in question titles; `name` is accepted as a fallback for title.

What it files (questions, severity "normal"):
  - one "[sign-off] <project> <id>: <title>" per failing entry with signed_off == false
  - one "[dispatch] <project>: run tonight? (...)" per project that has any
    signed-off failing entries, with "Run, start with <first id>" recommended
    and deliberately no "skip" choice (any answer triggers a dispatch; to
    skip a night, leave it pending; to drop it for good, dismiss it in
    brief-me). A proposal stays pending until answered or dismissed, even if
    the id set changes and a newer proposal is filed alongside it.

Idempotent: a question is skipped while one with the identical title is still
pending. `--dry-run` prints what would be filed without writing.

Install: copy anywhere and point config.json at it, e.g.
    "collector": ["python", "/path/to/feature-list-collector.py"]
"""
import json
import os
import sys
import uuid

# Windows Python < 3.15 defaults stdout/stderr to the locale code page
# (cp950), which mangles Chinese feature titles in "filed:" lines.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timezone

MAX_LINE_BYTES = 8 * 1024
BODY_BUDGET = 6 * 1024  # leave headroom for the JSON envelope


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


def brief_home():
    return os.environ.get("BRIEF_HOME", os.path.expanduser("~/.agent-brief"))


def pending_questions(inbox_path):
    """Pending questions (no status line referencing them): {title: (id, project)}."""
    questions, closed = {}, set()
    try:
        with open(inbox_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "question":
                    questions[rec["id"]] = (rec.get("title", ""), rec.get("project", ""))
                elif rec.get("type") == "status":
                    closed.add(rec.get("ref"))
    except OSError:
        return {}
    return {t: (qid, proj) for qid, (t, proj) in questions.items() if qid not in closed}


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip(text, budget=BODY_BUDGET):
    data = text.encode("utf-8")
    if len(data) <= budget:
        return text
    return data[:budget].decode("utf-8", errors="ignore") + "\n[truncated]"


def scan_repo(name, path):
    """Return (signoff_questions, dispatch_candidates) for one repo."""
    fl_path = os.path.join(path, "feature_list.json")
    if not os.path.isfile(fl_path):
        return [], []
    try:
        with open(fl_path, encoding="utf-8") as f:
            features = json.load(f).get("features", [])
    except (OSError, json.JSONDecodeError) as e:
        print(f"{name}: cannot read feature_list.json ({e}), skipping", file=sys.stderr)
        return [], []
    # Entries predating the signed_off field (2026-08-19) are legacy: neither
    # awaiting sign-off nor dispatchable (the delegation hook would deny them).
    unsigned, signed_failing, legacy = [], [], 0
    for feat in features:
        if feat.get("status") == "passing":
            continue
        flag = feat.get("signed_off")
        if flag is True:
            signed_failing.append(feat)
        elif flag is False:
            unsigned.append(feat)
        else:
            legacy += 1
    if legacy:
        print(f"{name}: {legacy} legacy failing entries without signed_off field (ignored)",
              file=sys.stderr)
    questions = []
    for feat in unsigned:
        acc = feat.get("acceptance", [])
        acc_text = "\n".join(f"- {a}" for a in acc) if isinstance(acc, list) else str(acc)
        title = feat.get("title") or feat.get("name") or ""
        questions.append({
            "title": f"[sign-off] {name} {feat.get('id')}: {title}",
            "body": clip(acc_text),
            "choices": ["Needs revision"],
            "recommendation": "Sign off as-is",
        })
    return questions, signed_failing


def main():
    dry = "--dry-run" in sys.argv
    home = brief_home()
    cfg_path = os.path.join(home, "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            projects = json.load(f).get("projects", [])
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"cannot read {cfg_path}: {e}")
    inbox = os.path.join(home, "inbox.jsonl")
    pending = pending_questions(inbox)
    already = set(pending)
    filed = skipped = 0
    for proj in projects:
        name, path = proj.get("name"), proj.get("path")
        if not name or not path:
            continue
        questions, signed_failing = scan_repo(name, path)
        if signed_failing:
            ids = ", ".join(f.get("id", "?") for f in signed_failing)
            first = signed_failing[0]
            questions.append({
                "title": f"[dispatch] {name}: run tonight? ({len(signed_failing)} signed failing: {ids})",
                "body": clip("\n".join(
                    f"- {f.get('id')}: {f.get('title') or f.get('name') or ''}"
                    for f in signed_failing)),
                # No "skip" choice on purpose: any answer makes dispatch.py spawn a
                # worker, so "not tonight" must be the UI's Skip (writes nothing).
                "choices": [],
                "recommendation": f"Run, start with {first.get('id')}",
            })
        for q in questions:
            if q["title"] in already:
                skipped += 1
                continue
            record = {
                "type": "question",
                "id": str(uuid.uuid4()),
                "project": name,
                "title": q["title"],
                "body": q["body"],
                "choices": q["choices"],
                "recommendation": q["recommendation"],
                "severity": "normal",
                "created_at": now_utc(),
            }
            if dry:
                print(f"would file: {q['title']}")
            else:
                atomic_append_line(inbox, record)
                print(f"filed: {q['title']}")
            filed += 1
    print(f"brief-scan done: {filed} {'would be ' if dry else ''}filed, {skipped} already pending")


if __name__ == "__main__":
    main()
