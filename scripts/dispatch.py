"""Dispatch headless claude -p sessions to continue work on named projects.

Usage: python scripts/dispatch.py <project> [<project> ...]

For each named project, looks up its repo path in config.json's `projects`
array and background-spawns a `claude -p` session there (prompt delivered on
stdin), carrying that project's unconsumed answers plus a
delegation-authorization sentence and a brief-submit instruction. On
successful spawn, the answers used are marked consumed by appending updated
answer lines (see docs/schema.md). Unrecognized project names are reported
and skipped without aborting the rest of the batch. The script never waits
for a dispatched session to finish: every spawn is recorded in
dispatches.jsonl and a detached waiter (`--wait`) records the batch's end
and runs config.json's `notify` once.

Stdlib only. Three knobs exist purely for testing without a real `claude`
binary, a real ~/.agent-brief, or a real ~/.claude.json:
- BRIEF_HOME: overrides the inbox home directory (default ~/.agent-brief).
- BRIEF_CLAUDE_CMD: overrides the claude executable name (default "claude").
- BRIEF_CLAUDE_JSON: overrides the claude.json path used to pre-accept the
  workspace trust dialog before a --watch window opens (default
  ~/.claude.json). Not read in headless (-p) mode.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any

MAX_LINE_BYTES = 8 * 1024

DELEGATION_SENTENCE = (
    "Before implementing, invoke Skill(skill=\"baton-dispatch\") and answer its five "
    "dispatch-brake questions (Outcome, Direct-work, Independence, Ownership, Closure) "
    "in your reply; group the features by shared files per its step 2 - one feature is "
    "not automatically one worker. Dispatch `executor` (isolation=\"worktree\") only "
    "for groups that pass all five; do the rest yourself. Record the per-feature "
    "verdict (dispatched / direct, and why) in your final brief-submit report."
)

NO_DELEGATION_SENTENCE = (
    "Do the work yourself in this session; do not dispatch subagents."
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


def fold_question_projects(inbox_path: str) -> dict[str, dict[str, str]]:
    """Map question id -> {"project": ..., "title": ...}, folding inbox.jsonl's
    `question` lines."""
    result: dict[str, dict[str, str]] = {}
    for line in iter_lines(inbox_path):
        record = json.loads(line)
        if record.get("type") == "question":
            result[record["id"]] = {"project": record["project"], "title": record["title"]}
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
    question_project: dict[str, dict[str, str]],
    answers_by_question: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        entry
        for qid, entry in answers_by_question.items()
        if not entry["record"].get("consumed", False)
        and question_project.get(qid, {}).get("project") == project
    ]


FEATURE_TOKEN_RE = re.compile(r"[A-Z]+[0-9]+")


def classify_task(title: str) -> dict[str, Any]:
    """Normalize one question title into a dispatches.jsonl tasks entry:
    {"feature": "F13"|None, "kind": "sign-off"|"dispatch"|"question", "title": title}.
    `kind` comes from the title's "[kind] " prefix (question if neither
    matches). `feature` is the first [A-Z]+[0-9]+ token right after the
    "[kind] <project>" lead-in - see examples/feature-list-collector.py for
    the two prefixed title formats ("[sign-off] <project> <id>: <title>",
    "[dispatch] <project>: ..."); a plain question title has no lead-in to
    strip, so its first token is checked as-is.
    ponytail: a dispatch title's lead-in is always followed by free text,
    never a feature id (any id appears later, inside "(... failing: F11,
    F13)"), so it short-circuits to feature=None instead of tokenizing.
    """
    if title.startswith("[sign-off] "):
        kind, body = "sign-off", title[len("[sign-off] "):]
    elif title.startswith("[dispatch] "):
        return {"feature": None, "kind": "dispatch", "title": title}
    else:
        kind, body = "question", title

    if kind == "sign-off":
        body = body.split(" ", 1)[1] if " " in body else ""  # drop "<project>"

    first_token = body.split(None, 1)[0] if body.strip() else ""
    match = FEATURE_TOKEN_RE.match(first_token)
    return {"feature": match.group(0) if match else None, "kind": kind, "title": title}


def tasks_for(
    unconsumed: list[dict[str, Any]],
    question_project: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Normalized task objects for the consumed answers, in prompt order (see
    build_prompt), for the started record's `tasks` field. Entries sharing
    the same (feature, kind) with a non-null feature are deduplicated,
    keeping only the first."""
    seen: set[tuple[str, str]] = set()
    tasks: list[dict[str, Any]] = []
    for entry in unconsumed:
        title = question_project.get(entry["record"]["question_id"], {}).get("title", "")
        task = classify_task(title)
        key = (task["feature"], task["kind"])
        if task["feature"] is not None:
            if key in seen:
                continue
            seen.add(key)
        tasks.append(task)
    return tasks


def build_prompt(project: str, unconsumed: list[dict[str, Any]], delegate: bool = True) -> str:
    # ponytail: same prompt for headless and --watch; the BRIEF_SUBMIT
    # sentence is harmless in an interactive window (user just sees it).
    delegation = DELEGATION_SENTENCE if delegate else NO_DELEGATION_SENTENCE
    if unconsumed:
        answers_block = "\n".join(entry["line"] for entry in unconsumed)
    else:
        answers_block = "(no unconsumed answers for this project)"

    return (
        f'You are a headless Claude Code session dispatched by agent-brief-me '
        f'for project "{project}".\n\n'
        f"Unconsumed answers for this project (raw JSONL records):\n"
        f"{answers_block}\n\n"
        f"{delegation}\n\n"
        f"{BRIEF_SUBMIT_SENTENCE}\n"
    )


# Non-interactive `-p` auto-denies every permission prompt, so without an
# allowlist the worker is read-only (2026-08-20: two dispatched sessions spun
# for 0 changes). Defaults below; overridable via config.json "dispatch".
DISPATCH_DEFAULTS = {
    "watch": False,
    "permission_mode": "auto",
    "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep,Skill",
    "model": None,
    "delegate": True,
}
PERMISSION_MODES = ("auto", "bypassPermissions")


def dispatch_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("dispatch") or {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DISPATCH_DEFAULTS, **{k: v for k, v in raw.items() if k in DISPATCH_DEFAULTS}}
    if merged["permission_mode"] not in PERMISSION_MODES:
        print(f'config dispatch.permission_mode {merged["permission_mode"]!r} unknown, using "auto"')
        merged["permission_mode"] = "auto"
    return merged


def claude_args(settings: dict[str, Any], brief_home: str) -> list[str]:
    """Flags shared by headless and --watch. Must come AFTER the positional
    prompt: `--allowedTools <tools...>` is variadic and swallows anything
    after it (verified 2026-08-21)."""
    if settings["permission_mode"] == "bypassPermissions":
        args = ["--dangerously-skip-permissions"]
    else:
        args = ["--permission-mode", settings["permission_mode"],
                "--allowedTools", settings["allowed_tools"]]
    # brief-submit writes outside the project dir; auto mode silently denies
    # that in -p unless the dir is granted.
    args += ["--add-dir", brief_home]
    if settings["model"]:
        args += ["--model", str(settings["model"])]
    if not settings["delegate"]:
        args += ["--disallowedTools", "Agent"]
    return args


def make_log_path(brief_home: str, project: str) -> str:
    return os.path.join(brief_home, "logs", f"{project}-{uuid.uuid4().hex}.log")


def spawn(claude_cmd: str, cwd: str, prompt: str, log_path: str, args: list[str]) -> subprocess.Popen:
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
            [claude_cmd, "-p", *args],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=clean_env(),
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


def clean_env() -> dict[str, str]:
    """Drop CLAUDE* vars inherited from the Claude session that runs this
    script; otherwise the child is treated as a nested session (transcript
    off, initial prompt ignored - observed 2026-08-21)."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE")}


def get_claude_json_path() -> str:
    return os.environ.get("BRIEF_CLAUDE_JSON", os.path.expanduser("~/.claude.json"))


def ensure_trusted(claude_json_path: str, project_path: str) -> bool:
    """Mark project_path trust-dialog-accepted in Claude Code's claude.json,
    so a --watch window never blocks on the one-time workspace trust prompt.

    Returns True if the file was rewritten (the project's
    `hasTrustDialogAccepted` was missing or not True), False if it was
    already True (file left untouched). The project key matches Claude
    Code's own claude.json format: absolute path, forward slashes, no
    trailing slash (e.g. "C:/Users/x/repo"). A project missing from
    `projects` gets a new entry.

    Raises FileNotFoundError if claude_json_path doesn't exist,
    json.JSONDecodeError if it isn't valid JSON, and OSError if the atomic
    write fails - callers wanting fail-open behaviour should catch
    (OSError, ValueError) around this call (see main()).
    """
    with open(claude_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    key = os.path.abspath(project_path).replace("\\", "/").rstrip("/")
    entry = data.setdefault("projects", {}).setdefault(key, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return False
    entry["hasTrustDialogAccepted"] = True

    directory = os.path.dirname(os.path.abspath(claude_json_path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".claude-json-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, claude_json_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return True


def spawn_watch(claude_cmd: str, cwd: str, prompt: str, args: list[str]) -> subprocess.Popen:
    """Open an interactive `claude <prompt>` in a new console window so the
    user can watch it work. Same allowlist as headless so behaviour matches;
    anything outside it still prompts in the window. No log file - the window
    is the log.
    """
    # The multi-line JSONL prompt is written to a file and referenced by a
    # one-line positional prompt: a long quoted argv with newlines/quotes was
    # silently dropped by the interactive launcher (2026-08-21).
    prompt_path = os.path.join(get_brief_home(), "prompts", f"{os.path.basename(cwd)}.md")
    os.makedirs(os.path.dirname(prompt_path), exist_ok=True)
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    opener = f"Read {prompt_path} and follow it as your task brief."
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)  # Windows only; 0 elsewhere
    return subprocess.Popen([claude_cmd, opener, *args], cwd=cwd,
                            creationflags=flags, env=clean_env())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dispatches_path(brief_home: str) -> str:
    return os.path.join(brief_home, "dispatches.jsonl")


def pid_exit_code(pid: int):
    """None while the process is alive; its exit code once gone. The sessions
    are not our children once the waiter is detached, so no waitpid.
    ponytail: POSIX branch reports 0 for any finished pid - os.kill can
    tell alive/dead but not the code; use Windows branch for real codes."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 0
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        if not ok or code.value == 259:  # STILL_ACTIVE
            return None
        return int(code.value)
    try:
        os.kill(pid, 0)
        return None
    except ProcessLookupError:
        return 0
    except PermissionError:
        return None


def spawn_waiter(brief_home: str, batch_id: str, pids: list[int]) -> None:
    """Detached `dispatch.py --wait` so the caller returns immediately."""
    cmd = [sys.executable, os.path.abspath(__file__), "--wait", batch_id, *map(str, pids)]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "cwd": brief_home}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def wait_batch(brief_home: str, batch_id: str, pids: list[int]) -> int:
    codes: dict[int, int] = {}
    while len(codes) < len(pids):
        for pid in pids:
            if pid not in codes:
                code = pid_exit_code(pid)
                if code is not None:
                    codes[pid] = code
        if len(codes) < len(pids):
            time.sleep(2)
    atomic_append_line(dispatches_path(brief_home), {
        "type": "finished", "batch_id": batch_id, "finished_at": _now(),
        "exit_codes": [codes[p] for p in pids],
    })
    projects = []
    for line in open(dispatches_path(brief_home), encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("type") == "started" and rec.get("batch_id") == batch_id:
            projects.append(rec["project"])
    notify = load_config(brief_home).get("notify")
    if notify:
        try:
            subprocess.run(list(notify) + [",".join(projects), "batch", "none",
                                           f"{len(pids)}/{len(pids)} sessions finished"], check=False)
        except OSError:
            pass
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--wait"]:
        return wait_batch(get_brief_home(), argv[1], [int(p) for p in argv[2:]])
    watch_flag, no_watch_flag = "--watch" in argv, "--no-watch" in argv
    argv = [a for a in argv if a not in ("--watch", "--no-watch")]
    if not argv or (watch_flag and no_watch_flag):
        print("usage: dispatch.py [--watch | --no-watch] <project> [<project> ...]", file=sys.stderr)
        return 2 if (watch_flag and no_watch_flag) else 1

    brief_home = get_brief_home()
    claude_cmd = os.environ.get("BRIEF_CLAUDE_CMD", "claude")
    claude_json_path = get_claude_json_path()

    config = load_config(brief_home)
    projects_by_name = {p["name"]: p["path"] for p in config.get("projects", [])}
    settings = dispatch_settings(config)
    # CLI flag is an explicit override; config dispatch.watch only applies when neither is given.
    watch = watch_flag if (watch_flag or no_watch_flag) else bool(settings["watch"])
    args = claude_args(settings, brief_home)

    question_project = fold_question_projects(os.path.join(brief_home, "inbox.jsonl"))
    answers_path = os.path.join(brief_home, "answers.jsonl")
    answers_by_question = fold_answers(answers_path)

    exit_code = 0
    batch_id = str(uuid.uuid4())
    pids: list[int] = []
    for name in argv:
        path = projects_by_name.get(name)
        if path is None:
            print(f"{name}: unknown project, skipping")
            exit_code = 1
            continue

        unconsumed = unconsumed_answers_for(name, question_project, answers_by_question)
        prompt = build_prompt(name, unconsumed, delegate=bool(settings["delegate"]))
        log_path = make_log_path(brief_home, name)

        if watch:
            try:
                if ensure_trusted(claude_json_path, path):
                    print(f"{name}: trusted {path}")
            except (OSError, ValueError) as exc:
                print(f"warning: {name}: could not pre-accept trust dialog in "
                      f"{claude_json_path} ({exc}), continuing", file=sys.stderr)

        try:
            if watch:
                proc = spawn_watch(claude_cmd, path, prompt, args)
            else:
                proc = spawn(claude_cmd, path, prompt, log_path, args)
        except OSError as exc:
            print(f"{name}: spawn failed: {exc}")
            exit_code = 1
            continue

        print(f"{name}: opened in new window" if watch else f"{name}: dispatched, log={log_path}")
        pids.append(proc.pid)
        atomic_append_line(dispatches_path(brief_home), {
            "type": "started", "batch_id": batch_id, "project": name, "pid": proc.pid,
            "started_at": _now(), "log": None if watch else log_path,
            "tasks": tasks_for(unconsumed, question_project),
        })

        for entry in unconsumed:
            updated = dict(entry["record"])
            updated["consumed"] = True
            atomic_append_line(answers_path, updated)

    if pids:
        spawn_waiter(brief_home, batch_id, pids)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
