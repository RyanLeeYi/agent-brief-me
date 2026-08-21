"""Dispatch headless claude -p sessions to continue work on named projects.

Usage: python scripts/dispatch.py <project> [<project> ...]

For each named project, looks up its repo path in config.json's `projects`
array and background-spawns a `claude -p` session there (prompt delivered on
stdin), carrying that project's unconsumed answers plus a
delegation-authorization sentence and a brief-submit instruction. On
successful spawn, the answers used are marked consumed by appending updated
answer lines (see docs/schema.md). Unrecognized project names are reported
and skipped without aborting the rest of the batch. The script never waits
for a dispatched session to finish.

Stdlib only. Two knobs exist purely for testing without a real `claude`
binary or a real ~/.agent-brief:
- BRIEF_HOME: overrides the inbox home directory (default ~/.agent-brief).
- BRIEF_CLAUDE_CMD: overrides the claude executable name (default "claude").
"""

import json
import os
import subprocess
import sys
import uuid
from typing import Any

MAX_LINE_BYTES = 8 * 1024

DELEGATION_SENTENCE = (
    "You are authorized to dispatch subagents to help complete this work, "
    "following the user's existing subagent-delegation rules."
)

BRIEF_SUBMIT_SENTENCE = (
    "Use brief-submit in two situations: whenever you are blocked on a "
    "decision only the user can make, and once more, right before "
    "finishing, to report what was done."
)


def atomic_append_line(path: str, record: dict[str, Any]) -> None:
    """Append one JSON record as one line, per the atomic append rule in
    docs/schema.md: append-mode open, hold an exclusive lock, write the
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


def get_brief_home() -> str:
    return os.environ.get("BRIEF_HOME", os.path.expanduser("~/.agent-brief"))


def load_config(brief_home: str) -> dict[str, Any]:
    path = os.path.join(brief_home, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"projects": []}


def iter_lines(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line:
                yield line


def fold_question_projects(inbox_path: str) -> dict[str, str]:
    """Map question id -> project, folding inbox.jsonl's `question` lines."""
    result: dict[str, str] = {}
    for line in iter_lines(inbox_path):
        record = json.loads(line)
        if record.get("type") == "question":
            result[record["id"]] = record["project"]
    return result


def fold_answers(answers_path: str) -> dict[str, dict[str, Any]]:
    """Map question_id -> {"line": <raw text>, "record": <parsed dict>} for
    the *last* answer line per question_id, per docs/schema.md's folding
    rule.
    """
    result: dict[str, dict[str, Any]] = {}
    for line in iter_lines(answers_path):
        record = json.loads(line)
        result[record["question_id"]] = {"line": line, "record": record}
    return result


def unconsumed_answers_for(
    project: str,
    question_project: dict[str, str],
    answers_by_question: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        entry
        for qid, entry in answers_by_question.items()
        if not entry["record"].get("consumed", False)
        and question_project.get(qid) == project
    ]


def build_prompt(project: str, unconsumed: list[dict[str, Any]]) -> str:
    # ponytail: same prompt for headless and --watch; the BRIEF_SUBMIT
    # sentence is harmless in an interactive window (user just sees it).
    if unconsumed:
        answers_block = "\n".join(entry["line"] for entry in unconsumed)
    else:
        answers_block = "(no unconsumed answers for this project)"

    return (
        f'You are a headless Claude Code session dispatched by agent-brief-me '
        f'for project "{project}".\n\n'
        f"Unconsumed answers for this project (raw JSONL records):\n"
        f"{answers_block}\n\n"
        f"{DELEGATION_SENTENCE}\n\n"
        f"{BRIEF_SUBMIT_SENTENCE}\n"
    )


# Non-interactive `-p` auto-denies every permission prompt, so without an
# allowlist the worker is read-only (2026-08-20: two dispatched sessions spun
# for 0 changes). Edits + shell + the brief-submit skill are what a worker needs;
# everything else (MCP, web, Agent) stays denied.
# ponytail: flat allowlist, no per-project config; add a config.json key when
# one project needs a different set.
HEADLESS_ARGS = [
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep,Skill",
]


def make_log_path(brief_home: str, project: str) -> str:
    return os.path.join(brief_home, "logs", f"{project}-{uuid.uuid4().hex}.log")


def spawn(claude_cmd: str, cwd: str, prompt: str, log_path: str) -> subprocess.Popen:
    """Background-spawn `claude_cmd -p` in cwd, feeding prompt on stdin (this
    sidesteps argv-quoting/length limits for arbitrary JSONL content), with
    combined stdout/stderr redirected to log_path. Does not wait for it to
    finish. Raises OSError (e.g. FileNotFoundError) if the process cannot
    start.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            [claude_cmd, "-p", *HEADLESS_ARGS],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        log_fh.close()
    # ponytail: single blocking write, no chunking/background thread. Fine
    # while prompts stay small (each raw answer line is capped at 8 KiB by
    # the atomic append rule); upgrade to a writer thread if that stops
    # holding.
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()
    return proc


def spawn_watch(claude_cmd: str, cwd: str, prompt: str) -> subprocess.Popen:
    """Open an interactive `claude <prompt>` in a new console window so the
    user can watch it work. Same allowlist as headless so behaviour matches;
    anything outside it still prompts in the window. No log file - the window
    is the log.
    """
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # Windows only; 0 elsewhere
    return subprocess.Popen([claude_cmd, *HEADLESS_ARGS, prompt], cwd=cwd, creationflags=flags)


def main(argv: list[str]) -> int:
    watch = "--watch" in argv
    argv = [a for a in argv if a != "--watch"]
    if not argv:
        print("usage: dispatch.py [--watch] <project> [<project> ...]", file=sys.stderr)
        return 1

    brief_home = get_brief_home()
    claude_cmd = os.environ.get("BRIEF_CLAUDE_CMD", "claude")

    config = load_config(brief_home)
    projects_by_name = {p["name"]: p["path"] for p in config.get("projects", [])}

    question_project = fold_question_projects(os.path.join(brief_home, "inbox.jsonl"))
    answers_path = os.path.join(brief_home, "answers.jsonl")
    answers_by_question = fold_answers(answers_path)

    exit_code = 0
    for name in argv:
        path = projects_by_name.get(name)
        if path is None:
            print(f"{name}: unknown project, skipping")
            exit_code = 1
            continue

        unconsumed = unconsumed_answers_for(name, question_project, answers_by_question)
        prompt = build_prompt(name, unconsumed)
        log_path = make_log_path(brief_home, name)

        try:
            if watch:
                spawn_watch(claude_cmd, path, prompt)
            else:
                spawn(claude_cmd, path, prompt, log_path)
        except OSError as exc:
            print(f"{name}: spawn failed: {exc}")
            exit_code = 1
            continue

        print(f"{name}: opened in new window" if watch else f"{name}: dispatched, log={log_path}")

        for entry in unconsumed:
            updated = dict(entry["record"])
            updated["consumed"] = True
            atomic_append_line(answers_path, updated)

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
