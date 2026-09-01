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

`--refill <question_id>` (F28) re-dispatches a single pending question to a
narrowly-scoped session that investigates missing context and refiles a
self-contained replacement, then cancels the original; see
refill_question()'s docstring. F29 adds two config.json `dispatch` knobs that
tweak the prompts this file builds: `context_language` (a free-form language
requirement added to the refill prompt) and `plain_language` (a
non-technical-writing requirement added to the refill prompt when "refill" or
"all", and to the regular dispatch prompt too when "all").

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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

MAX_LINE_BYTES = 8 * 1024

# Windows CreateProcess flags shared by every background/detached child
# (spawn, spawn_waiter) so a process-group-based stop of the dispatching
# service (e.g. an external process manager) does not sweep up sessions
# that must
# outlive it (2026-08-27 incident: a manual stop killed 5 in-flight
# headless sessions started moments earlier).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

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


PLAIN_LANGUAGE_VALUES = ("all", "refill", "off")


def context_language_for(config: dict[str, Any]) -> str | None:
    """dispatch.context_language (F29): a free-form language description
    (e.g. "Traditional Chinese (keep technical terms in English)") applied
    to a context-refill session's replacement question, or None when
    missing/null/not a non-empty string."""
    dispatch_cfg = config.get("dispatch")
    if not isinstance(dispatch_cfg, dict):
        return None
    value = dispatch_cfg.get("context_language")
    return value if isinstance(value, str) and value.strip() else None


def plain_language_for(config: dict[str, Any]) -> str:
    """dispatch.plain_language (F29): "all" | "refill" | "off", defaulting
    to "off" on a missing key or any other value."""
    dispatch_cfg = config.get("dispatch")
    if not isinstance(dispatch_cfg, dict):
        return "off"
    value = dispatch_cfg.get("plain_language")
    return value if value in PLAIN_LANGUAGE_VALUES else "off"


def build_prompt(project: str, unconsumed: list[dict[str, Any]], delegate: bool = False,
                  config: dict[str, Any] | None = None) -> str:
    # ponytail: same prompt for headless and --watch; the BRIEF_SUBMIT
    # sentence is harmless in an interactive window (user just sees it).
    config = config or {}
    delegation = DELEGATION_SENTENCE if delegate else NO_DELEGATION_SENTENCE
    if unconsumed:
        answers_block = "\n".join(entry["line"] for entry in unconsumed)
    else:
        answers_block = "(no unconsumed answers for this project)"

    # F29: plain_language "all" also covers regular dispatch (not just
    # refill) - the language always comes from context_language, so this
    # sentence never hardcodes one itself (per docs/schema.md's contract:
    # plain_language "off"/"refill" leave this prompt byte-for-byte the
    # same as before F29).
    plain_block = ""
    if plain_language_for(config) == "all":
        language = context_language_for(config)
        in_language = f" in {language}" if language else ""
        plain_block = (
            "\n\nWrite any question or report you file via brief-submit in "
            f"plain language{in_language}, for a non-technical reader, avoiding jargon.\n"
        )

    return (
        f'You are a headless Claude Code session dispatched by agent-brief-me '
        f'for project "{project}".\n\n'
        f"Unconsumed answers for this project (raw JSONL records):\n"
        f"{answers_block}\n\n"
        f"{delegation}\n\n"
        f"{BRIEF_SUBMIT_SENTENCE}\n"
        f"{plain_block}"
    )


# Non-interactive `-p` auto-denies every permission prompt, so without an
# allowlist the worker is read-only (2026-08-20: two dispatched sessions spun
# for 0 changes). Defaults below; overridable via config.json "dispatch".
DISPATCH_DEFAULTS = {
    "watch": False,
    "permission_mode": "auto",
    "allowed_tools": "Bash,Read,Edit,Write,Glob,Grep,Skill",
    "model": None,
    "delegate": False,
    "window": "console",
}
PERMISSION_MODES = ("auto", "bypassPermissions")
WINDOW_VALUES = ("console", "orca")


def dispatch_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("dispatch") or {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DISPATCH_DEFAULTS, **{k: v for k, v in raw.items() if k in DISPATCH_DEFAULTS}}
    if merged["permission_mode"] not in PERMISSION_MODES:
        print(f'config dispatch.permission_mode {merged["permission_mode"]!r} unknown, using "auto"')
        merged["permission_mode"] = "auto"
    if merged["window"] is None:
        merged["window"] = "console"  # missing key or explicit null: same as "console" (F34)
    elif merged["window"] not in WINDOW_VALUES:
        print(f'config dispatch.window {merged["window"]!r} unknown, using "console"')
        merged["window"] = "console"
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

    Started in its own process group (same DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP on Windows / start_new_session on POSIX as
    spawn_waiter below), so a process-group-based stop/restart of the
    dispatching service does not kill the session it just started.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log_fh = open(log_path, "wb")
    isolation: dict[str, Any] = {}
    if sys.platform == "win32":
        isolation["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        isolation["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [claude_cmd, "-p", *args],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=clean_env(),
            **isolation,
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

    Isolation: CREATE_NEW_CONSOLE alone already gives this process its own
    console session, which Windows treats as a separate console process
    group from the parent's - the same isolation CREATE_NEW_PROCESS_GROUP
    would add explicitly, and which CREATE_NEW_PROCESS_GROUP is documented
    to be ignored under anyway when combined with CREATE_NEW_CONSOLE. No
    extra flag needed here; see spawn()/spawn_waiter() for the headless case
    where nothing already provides that isolation.
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


# ------------------------------------------------------------------ orca --
# F34: `dispatch.window: "orca"` opens a --watch session in an Orca terminal
# tab instead of a native console window, so it survives unattended (no GUI
# window that must stay visible/focused). Only reached from --watch; headless
# (-p) dispatch never touches orca.
# F35: `--no-orca` on the CLI forces this one dispatch to skip Orca and use
# console windows instead, without touching config.json - see `use_orca` in
# main(). `is_ancestor_workspace()` below is used by brief-init's per-project
# Orca binding question (skills/brief-init/SKILL.md), not by dispatch.py's
# own runtime path.

# Callers of run_orca often have no console of their own (the detached
# --wait waiter, the Web UI server): on Windows, launching a console program
# from such a parent makes the OS allocate a brand-new visible console
# window for it - a blocking `orca terminal wait` then sits in a cmd window
# for the whole session (observed 2026-09-01). CREATE_NO_WINDOW suppresses
# that; it is 0 (a no-op) elsewhere.
ORCA_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_orca(args: list[str]) -> dict[str, Any]:
    """Run `orca <args...> --json` and parse its JSON stdout. Resolved via
    shutil.which (not a bare "orca" argv[0]) because Windows CreateProcess
    only auto-appends ".exe" to an extension-less program name, never
    ".cmd"/".bat" - the shim a real (or, in tests, fake) Orca CLI installs -
    so a bare "orca" silently fails to launch there even when it is on
    PATH. Both an inability to start the process and non-JSON stdout fold
    into the same {"ok": False, "error": <str>} shape orca's own failure
    responses use, so callers only ever check `.get("ok")`."""
    cmd = shutil.which("orca") or "orca"
    try:
        result = subprocess.run([cmd, *args, "--json"], capture_output=True, text=True,
                                creationflags=ORCA_CREATION_FLAGS)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": (result.stdout + result.stderr)[-500:]}


def _orca_status_ok() -> bool:
    """The batch-wide preflight before any --watch dispatch with
    `window: "orca"`: "Orca is not running" is defined (per the F34
    acceptance) as the `orca status --json` invocation failing or
    returning `ok: false` - this does not inspect `result.app.running`."""
    return bool(run_orca(["status"]).get("ok"))


def _project_orca_mode(project_cfg: dict[str, Any]) -> dict[str, Any]:
    """A project's optional `orca` object in config.json's `projects[]`
    entry: `{"mode": "bind", "repo_id": "..."}` when present and valid,
    else `{"mode": "repo"}` (the default - also used for a missing key or
    any other shape)."""
    orca_cfg = project_cfg.get("orca")
    if isinstance(orca_cfg, dict) and orca_cfg.get("mode") == "bind":
        return orca_cfg
    return {"mode": "repo"}


def _orca_repo_path(repo_id: str) -> str | None:
    """`path` of the Orca-tracked repo with this id (`orca repo list
    --json`), or None if no such repo is registered."""
    result = run_orca(["repo", "list"])
    if not result.get("ok"):
        return None
    for repo in result.get("result", {}).get("repos", []):
        if repo.get("id") == repo_id:
            return repo.get("path")
    return None


def is_ancestor_workspace(repo_path: str, project_path: str) -> bool:
    """True when `repo_path` is a strict path-component ancestor of
    `project_path` (F35, used by brief-init's per-project Orca binding
    question - see skills/brief-init/SKILL.md). Both sides are normalized
    via os.path.normcase(os.path.abspath(...)) then split into path
    components, so case and trailing-slash differences never affect the
    comparison. Equal paths (a project already registered as its own Orca
    repo) are NOT an ancestor - only a strictly shorter prefix counts."""
    def parts(path: str) -> list[str]:
        drive, rest = os.path.splitdrive(os.path.normcase(os.path.abspath(path)))
        return [drive, *[p for p in rest.split(os.sep) if p]]

    repo_parts, project_parts = parts(repo_path), parts(project_path)
    return len(repo_parts) < len(project_parts) and project_parts[:len(repo_parts)] == repo_parts


def _quote_command(argv: list[str]) -> str:
    """Join argv into one shell command-line string, for orca terminal
    create's --command (a single string, unlike spawn()/spawn_watch()'s
    argv list) - Windows quoting via the stdlib's own list2cmdline (the
    same rules CreateProcess itself uses), POSIX via shlex.join."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _orca_worktree(project_path: str, project_cfg: dict[str, Any]):
    """(worktree_arg, command_prefix, error) for `orca terminal create
    --worktree` (F34): repo mode (default) targets the project path
    directly; bind mode resolves config's `repo_id` via `orca repo list`
    and prefixes --command with a `cd` into the project path (the bound
    Orca repo's own worktree may be a different directory, e.g. a
    monorepo). `error` is set (the other two None) only when a bind
    `repo_id` is not found in `orca repo list` - the caller then falls
    back to a console window for this one project."""
    mode = _project_orca_mode(project_cfg)
    if mode.get("mode") != "bind":
        return f"path:{project_path}", "", None
    repo_id = mode.get("repo_id")
    repo_path = _orca_repo_path(repo_id)
    if repo_path is None:
        return None, None, f"orca repo_id {repo_id!r} not found in `orca repo list`"
    return f"id:{repo_id}::{repo_path}", f'cd "{project_path}" && ', None


def spawn_orca_watch(brief_home: str, project_name: str, project_path: str, claude_cmd: str,
                      prompt: str, args: list[str], project_cfg: dict[str, Any]):
    """Open an interactive claude session inside an Orca terminal tab
    instead of a native console window (F34). Returns (terminal_handle,
    error): terminal_handle is orca's "term_..." handle on success; error
    is a human-readable reason on failure (worktree resolution, or
    `orca terminal create` itself), and the caller then falls back to a
    console window for this one project.

    Same prompt-file/opener convention as spawn_watch() (see its
    docstring): the multi-line prompt is written to a file and referenced
    by a one-line opener sentence, since a long --command string with
    embedded newlines is no safer here than it was as a console argv.

    Repo-mode only (`--worktree path:...`): a `selector_not_found` error
    from `orca terminal create` triggers one `orca repo add --path
    <project_path>` followed by exactly one retry, per the F34 acceptance
    - bind mode's repo_id is expected to already be registered, so it gets
    no such retry.
    """
    prompt_path = os.path.join(brief_home, "prompts", f"{os.path.basename(project_path)}.md")
    os.makedirs(os.path.dirname(prompt_path), exist_ok=True)
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    opener = f"Read {prompt_path} and follow it as your task brief."

    worktree_arg, cmd_prefix, err = _orca_worktree(project_path, project_cfg)
    if err:
        return None, err

    command_line = cmd_prefix + _quote_command([claude_cmd, opener, *args])
    create_args = ["terminal", "create", "--worktree", worktree_arg,
                   "--command", command_line, "--title", project_name]
    result = run_orca(create_args)
    if not result.get("ok") and worktree_arg.startswith("path:"):
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "selector_not_found":
            run_orca(["repo", "add", "--path", project_path])
            result = run_orca(create_args)
    if not result.get("ok"):
        return None, f"orca terminal create failed: {result.get('error')}"
    handle = result.get("result", {}).get("terminal", {}).get("handle")
    if not handle:
        return None, "orca terminal create returned no terminal handle"
    return handle, None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dispatches_path(brief_home: str) -> str:
    return os.path.join(brief_home, "dispatches.jsonl")


# Sentinel for "the process is gone but we could not learn its real exit
# code" (Windows: OpenProcess failed, e.g. the process object no longer
# exists; POSIX: os.kill can tell alive/dead but never the code, since these
# sessions are not our children once the waiter is detached, so no waitpid).
# Must be distinguishable from any real exit code so a lookup failure is
# never mistaken for a clean exit(0) - see the 2026-08-27 incident notes on
# DETACHED_PROCESS above. Real Windows exit codes are non-negative DWORDs,
# so -1 can never collide with one.
EXIT_CODE_UNKNOWN = -1


def pid_exit_code(pid: int | None):
    """None while the process is alive - also when pid is None, which
    dispatch.py itself never passes here but a caller reading someone
    else's `started` record might (an orca-terminal session's record
    always carries `pid: null`, F34); its exit code once gone, or
    EXIT_CODE_UNKNOWN when it is gone but the real code could not be
    determined."""
    if pid is None:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return EXIT_CODE_UNKNOWN
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
        return EXIT_CODE_UNKNOWN
    except PermissionError:
        return None


def spawn_waiter(brief_home: str, batch_id: str, targets: list[Any]) -> None:
    """Detached `dispatch.py --wait` so the caller returns immediately.
    `targets` is a list of pids (int, console/headless sessions) and/or
    orca terminal handles (str, "term_...", F34) - see wait_batch()."""
    cmd = [sys.executable, os.path.abspath(__file__), "--wait", batch_id, *map(str, targets)]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "cwd": brief_home}
    if sys.platform == "win32":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


# --------------------------------------------------------- context-refill --
# F28: re-dispatch a single pending question to a narrowly-scoped session
# that investigates missing context and refiles a self-contained
# replacement, then cancels the original. Reached via this file's `--refill
# <question_id>` CLI, the Web UI's "Add context" button, and brief-me's
# per-question review.

REFILL_ALLOWED_TOOLS = "Bash,Read,Glob,Grep,Skill"  # no Edit/Write: a refill
# session investigates and files a replacement question, it never touches
# the repo.

REFILL_PROMPT = (
    'You are a headless Claude Code session dispatched by agent-brief-me to add '
    'missing context to one pending inbox question, id "{qid}".\n\n'
    'Original question (raw JSONL record, for full context):\n{raw_line}\n\n'
    'Title: "{title}"\n\n'
    "Investigate whatever this project needs (read files, recent history, prior "
    "discussion) to fill the gaps that keep the current card from being "
    "decidable by a reader with no repo access. Then invoke "
    'Skill(skill="brief-submit") to file ONE new, self-contained question for '
    "the same project, with body written in brief-submit's body template "
    "exactly:\n\n"
    "Context: <why this decision is needed now - one sentence>\n"
    "Options:\n"
    "- <choice 1>: <its consequence - one sentence>\n"
    "- <choice 2>: <its consequence - one sentence>\n"
    "Recommendation: <choice> - <the reason - one sentence>\n\n"
    "Once the new question is filed successfully, append one status line to "
    "inbox.jsonl cancelling the ORIGINAL question: "
    '{{"type": "status", "ref": "{qid}", "status": "cancelled", "at": <now, UTC '
    "ISO8601>}}, using the atomic append rule in docs/schema.md. Do nothing "
    "else: do not answer the original question yourself, do not edit any "
    "file, and do not pick up unrelated work."
)


def find_question(inbox_path: str, question_id: str):
    """The `question` record with id == question_id in inbox.jsonl, and its
    raw line, or (None, None) if no such question exists."""
    for line in iter_lines(inbox_path):
        record = json.loads(line)
        if record.get("type") == "question" and record.get("id") == question_id:
            return record, line
    return None, None


def context_model_for(config: dict[str, Any]) -> str:
    """dispatch.context_model, defaulting to "sonnet" when missing or null
    (F28) - independent of dispatch.model, which regular workers use."""
    dispatch_cfg = config.get("dispatch")
    if not isinstance(dispatch_cfg, dict):
        dispatch_cfg = {}
    return dispatch_cfg.get("context_model") or "sonnet"


def refill_claude_args(context_model: str, brief_home: str, session_id: str | None = None) -> list[str]:
    """Flags for a context-refill session: read-only tool allowlist (no
    Edit/Write - see REFILL_ALLOWED_TOOLS), its own --model, and --resume
    prepended when the original question carries a session_id."""
    args = ["--permission-mode", "auto", "--allowedTools", REFILL_ALLOWED_TOOLS,
            "--add-dir", brief_home, "--model", str(context_model), "--disallowedTools", "Agent"]
    return ["--resume", session_id] + args if session_id else args


def build_refill_prompt(question: dict[str, Any], raw_line: str, config: dict[str, Any] | None = None) -> str:
    """The context-refill prompt (F28), plus F29's two optional additions -
    a language requirement (dispatch.context_language) and a plain-language
    requirement (dispatch.plain_language "refill"/"all") - each appended as
    its own sentence, in that order. `config=None` (or one with neither key
    set) reproduces F28's prompt byte-for-byte."""
    config = config or {}
    prompt = REFILL_PROMPT.format(qid=question["id"], raw_line=raw_line, title=question["title"])
    language = context_language_for(config)
    if language:
        prompt += f"\n\nWrite the new question's title and body in {language}."
    if plain_language_for(config) in ("all", "refill"):
        prompt += (
            "\n\nWrite the new question's title and body in plain language, "
            "for a non-technical reader, avoiding jargon."
        )
    return prompt


def find_running_refill(brief_home: str, project: str, title: str) -> dict[str, Any] | None:
    """The `started` dispatches.jsonl record for an unfinished batch (no
    `finished` line, pid still alive) that already carries a `kind: "refill"`
    task for this exact (project, title), or None. Refactored out of
    is_refill_running (F29) so the Web UI can also read the record's
    `started_at` to render the "waiting on context" card state."""
    path = dispatches_path(brief_home)
    finished_batches = {r["batch_id"] for r in (json.loads(l) for l in iter_lines(path))
                        if r.get("type") == "finished"}
    for line in iter_lines(path):
        record = json.loads(line)
        if record.get("type") != "started" or record.get("project") != project:
            continue
        if record.get("batch_id") in finished_batches:
            continue
        tasks = record.get("tasks", [])
        matches = any(isinstance(t, dict) and t.get("kind") == "refill" and t.get("title") == title for t in tasks)
        if matches and pid_exit_code(record.get("pid")) is None:
            return record
    return None


def is_refill_running(brief_home: str, project: str, title: str) -> bool:
    """True when an unfinished batch already carries a `kind: "refill"` task
    for this exact (project, title) - the dedup rule that stops a second
    click from spawning a duplicate session (F28)."""
    return find_running_refill(brief_home, project, title) is not None


def refill_question(brief_home: str, claude_cmd: str, question_id: str) -> dict[str, Any]:
    """F28 entry point: re-dispatch one pending question. Writes only a
    `started` dispatches.jsonl line - never an answer or status line for the
    original question (that is the dispatched session's job, once its
    replacement question is filed)."""
    question, raw_line = find_question(os.path.join(brief_home, "inbox.jsonl"), question_id)
    if question is None:
        return {"ok": False, "error": f"unknown question id: {question_id}"}

    project, title = question["project"], question["title"]
    if is_refill_running(brief_home, project, title):
        return {"ok": True, "already_running": True, "project": project}

    config = load_config(brief_home)
    path = {p["name"]: p["path"] for p in config.get("projects", [])}.get(project)
    if path is None:
        return {"ok": False, "error": f"{project}: unknown project"}

    args = refill_claude_args(context_model_for(config), brief_home, session_id=question.get("session_id"))
    prompt = build_refill_prompt(question, raw_line, config)
    log_path = make_log_path(brief_home, project)
    try:
        proc = spawn(claude_cmd, path, prompt, log_path, args)
    except OSError as exc:
        return {"ok": False, "error": f"spawn failed: {exc}"}

    batch_id = str(uuid.uuid4())
    task = {**classify_task(title), "kind": "refill"}
    atomic_append_line(dispatches_path(brief_home), {
        "type": "started", "batch_id": batch_id, "project": project, "pid": proc.pid,
        "started_at": _now(), "log": log_path, "tasks": [task],
    })
    spawn_waiter(brief_home, batch_id, [proc.pid])
    return {"ok": True, "project": project, "pid": proc.pid, "log": log_path}


def wait_batch(brief_home: str, batch_id: str, targets: list[Any]) -> int:
    """Block until every element of `targets` has ended, then write one
    `finished` line whose `exit_codes` is index-aligned with `targets`
    (the same order the batch's `started` records were written in, per
    docs/schema.md). Each target is either a numeric pid (console/headless
    sessions, polled via pid_exit_code()) or an orca terminal handle
    ("term_...", F34) waited on with a blocking `orca terminal wait --for
    exit` call, run in its own thread so pid polling and terminal waits
    proceed concurrently rather than one after another. A terminal wait
    that yields no integer exitCode records None, the same sentinel used
    for an undeterminable pid exit code."""
    n = len(targets)
    results: list[int | None] = [None] * n
    term_indices = [i for i, t in enumerate(targets) if str(t).startswith("term_")]
    pid_by_index = {i: int(t) for i, t in enumerate(targets) if i not in term_indices}

    def wait_terminal(i: int, handle: str) -> None:
        outcome = run_orca(["terminal", "wait", "--terminal", handle, "--for", "exit"])
        code = outcome.get("result", {}).get("wait", {}).get("exitCode") if outcome.get("ok") else None
        results[i] = code if isinstance(code, int) else None

    threads = [threading.Thread(target=wait_terminal, args=(i, str(targets[i]))) for i in term_indices]
    for t in threads:
        t.start()

    codes: dict[int, int] = {}
    while len(codes) < len(pid_by_index):
        for pid in pid_by_index.values():
            if pid not in codes:
                code = pid_exit_code(pid)
                if code is not None:
                    codes[pid] = code
        if len(codes) < len(pid_by_index):
            time.sleep(2)
    # EXIT_CODE_UNKNOWN is this module's internal sentinel (never a real exit
    # code); record it as JSON null rather than a made-up number.
    for i, pid in pid_by_index.items():
        results[i] = None if codes[pid] == EXIT_CODE_UNKNOWN else codes[pid]

    for t in threads:
        t.join()

    atomic_append_line(dispatches_path(brief_home), {
        "type": "finished", "batch_id": batch_id, "finished_at": _now(),
        "exit_codes": results,
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
                                           f"{n}/{n} sessions finished"], check=False)
        except OSError:
            pass
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--wait"]:
        return wait_batch(get_brief_home(), argv[1], argv[2:])
    if argv[:1] == ["--refill"]:
        if len(argv) != 2:
            print(json.dumps({"ok": False, "error": "usage: dispatch.py --refill <question_id>"}))
            return 2
        claude_cmd = os.environ.get("BRIEF_CLAUDE_CMD", "claude")
        result = refill_question(get_brief_home(), claude_cmd, argv[1])
        print(json.dumps(result))
        return 0 if result.get("ok") else 1
    watch_flag, no_watch_flag = "--watch" in argv, "--no-watch" in argv
    no_orca_flag = "--no-orca" in argv  # F35: this run only, never writes config.json
    argv = [a for a in argv if a not in ("--watch", "--no-watch", "--no-orca")]
    if not argv or (watch_flag and no_watch_flag):
        print("usage: dispatch.py [--watch | --no-watch] [--no-orca] <project> [<project> ...]", file=sys.stderr)
        return 2 if (watch_flag and no_watch_flag) else 1

    brief_home = get_brief_home()
    claude_cmd = os.environ.get("BRIEF_CLAUDE_CMD", "claude")
    claude_json_path = get_claude_json_path()

    config = load_config(brief_home)
    projects_by_name = {p["name"]: p for p in config.get("projects", [])}
    settings = dispatch_settings(config)
    # CLI flag is an explicit override; config dispatch.watch only applies when neither is given.
    watch = watch_flag if (watch_flag or no_watch_flag) else bool(settings["watch"])
    args = claude_args(settings, brief_home)

    question_project = fold_question_projects(os.path.join(brief_home, "inbox.jsonl"))
    answers_path = os.path.join(brief_home, "answers.jsonl")
    answers_by_question = fold_answers(answers_path)

    # F34: window: "orca" only ever applies to a --watch batch; headless (-p)
    # dispatch never calls orca. The "Orca running?" check is batch-wide (one
    # call, not one per project) and never calls `orca open` - starting the
    # GUI unattended is out of bounds.
    use_orca = watch and settings["window"] == "orca" and not no_orca_flag
    if use_orca and not _orca_status_ok():
        print("orca status check failed or Orca is not running; falling back to console windows")
        use_orca = False

    exit_code = 0
    batch_id = str(uuid.uuid4())
    targets: list[Any] = []
    for name in argv:
        project_cfg = projects_by_name.get(name)
        if project_cfg is None:
            print(f"{name}: unknown project, skipping")
            exit_code = 1
            continue
        path = project_cfg["path"]

        unconsumed = unconsumed_answers_for(name, question_project, answers_by_question)
        prompt = build_prompt(name, unconsumed, delegate=bool(settings["delegate"]), config=config)
        log_path = make_log_path(brief_home, name)

        if watch:
            try:
                if ensure_trusted(claude_json_path, path):
                    print(f"{name}: trusted {path}")
            except (OSError, ValueError) as exc:
                print(f"warning: {name}: could not pre-accept trust dialog in "
                      f"{claude_json_path} ({exc}), continuing", file=sys.stderr)

        terminal_handle = None
        if use_orca:
            terminal_handle, orca_error = spawn_orca_watch(
                brief_home, name, path, claude_cmd, prompt, args, project_cfg)
            if orca_error:
                print(f"{name}: {orca_error}, falling back to console window")

        if watch and terminal_handle is None:
            try:
                proc = spawn_watch(claude_cmd, path, prompt, args)
            except OSError as exc:
                print(f"{name}: spawn failed: {exc}")
                exit_code = 1
                continue
            print(f"{name}: opened in new window")
            target, record_pid, record_terminal = proc.pid, proc.pid, None
        elif watch:
            print(f"{name}: opened in Orca terminal")
            target, record_pid, record_terminal = terminal_handle, None, terminal_handle
        else:
            try:
                proc = spawn(claude_cmd, path, prompt, log_path, args)
            except OSError as exc:
                print(f"{name}: spawn failed: {exc}")
                exit_code = 1
                continue
            print(f"{name}: dispatched, log={log_path}")
            target, record_pid, record_terminal = proc.pid, proc.pid, None

        targets.append(target)
        started_record = {
            "type": "started", "batch_id": batch_id, "project": name, "pid": record_pid,
            "started_at": _now(), "log": None if watch else log_path,
            "tasks": tasks_for(unconsumed, question_project),
        }
        if record_terminal is not None:
            started_record["terminal"] = record_terminal
        atomic_append_line(dispatches_path(brief_home), started_record)

        for entry in unconsumed:
            updated = dict(entry["record"])
            updated["consumed"] = True
            atomic_append_line(answers_path, updated)

    if targets:
        spawn_waiter(brief_home, batch_id, targets)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
