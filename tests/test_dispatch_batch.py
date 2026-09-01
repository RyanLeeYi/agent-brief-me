"""F13: dispatch.py records started/finished lines and notifies once per batch.
Run: python tests/test_dispatch_batch.py  (stdlib only; fake claude = sleep 1)
F23-A: tasks_for's {feature, kind, title} normalization - run via
`python -m unittest tests.test_dispatch_batch`.
F32: spawn() isolates its child into its own process group (so a
service restart doesn't kill it), and pid_exit_code()/wait_batch() report an
undeterminable exit code as EXIT_CODE_UNKNOWN / JSON null rather than 0."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCH = os.path.join(ROOT, "scripts", "dispatch.py")
FAKE_NOTIFY = "import sys,json;open(sys.argv[1],'a',encoding='utf-8').write(json.dumps(sys.argv[2:])+chr(10))"


def wait_for(cond, timeout=20, msg="timeout"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(msg)


def main():
    home = tempfile.mkdtemp()
    calls = os.path.join(home, "calls.jsonl")
    pa, pb = tempfile.mkdtemp(), tempfile.mkdtemp()
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [{"name": "a", "path": pa}, {"name": "b", "path": pb}],
                   "collector": None, "notify": [sys.executable, "-c", FAKE_NOTIFY, calls]}, f)
    # F14: a pending question + unconsumed answer for project "a", so its
    # started record's `tasks` should carry the question's title; "b" has
    # none, so its `tasks` should be [].
    qid = "11111111-1111-4111-8111-111111111111"
    with open(os.path.join(home, "inbox.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "question", "id": qid, "project": "a",
                            "title": "Pick a datastore", "body": "b", "severity": "normal",
                            "created_at": "2026-08-20T09:00:00Z"}) + "\n")
    with open(os.path.join(home, "answers.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"question_id": qid, "chosen": "JSONL",
                            "answered_at": "2026-08-20T09:05:00Z", "consumed": False}) + "\n")
    # fake claude: a python script that sleeps 1s and exits 0, ignoring args/stdin
    fake = os.path.join(home, "fake_claude.py")
    with open(fake, "w", encoding="utf-8") as f:
        f.write("import time; time.sleep(1)\n")
    env = {**os.environ, "BRIEF_HOME": home, "BRIEF_CLAUDE_CMD": sys.executable}
    # dispatch passes `-p <prompt>` etc. after the positional; python ignores unknown
    # options only if the script comes first -> use a wrapper .cmd/.sh that prepends the script
    if sys.platform == "win32":
        wrapper = os.path.join(home, "claude.cmd")
        with open(wrapper, "w") as f:
            f.write(f'@"{sys.executable}" "{fake}"\n')
    else:
        wrapper = os.path.join(home, "claude")
        with open(wrapper, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{fake}"\n')
        os.chmod(wrapper, 0o755)
    env["BRIEF_CLAUDE_CMD"] = wrapper

    out = subprocess.run([sys.executable, DISPATCH, "a", "b"], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    path = os.path.join(home, "dispatches.jsonl")
    recs = [json.loads(l) for l in open(path, encoding="utf-8")]
    started = [r for r in recs if r["type"] == "started"]
    assert [r["project"] for r in started] == ["a", "b"], recs
    assert len({r["batch_id"] for r in started}) == 1
    assert all(r["pid"] and r["started_at"] and r["log"] for r in started)
    print("two started lines, one batch_id - OK")

    by_project = {r["project"]: r for r in started}
    assert by_project["a"]["tasks"] == [
        {"feature": None, "kind": "question", "title": "Pick a datastore"}
    ], by_project["a"]
    assert by_project["b"]["tasks"] == [], by_project["b"]
    print("started tasks include consumed answer's normalized task object - OK")

    def finished():
        rs = [json.loads(l) for l in open(path, encoding="utf-8")]
        return [r for r in rs if r["type"] == "finished"]
    wait_for(lambda: len(finished()) == 1, msg="waiter never wrote finished line")
    fin = finished()[0]
    assert fin["batch_id"] == started[0]["batch_id"] and fin["exit_codes"] == [0, 0], fin
    print("finished line with exit_codes [0, 0] - OK")

    wait_for(lambda: os.path.exists(calls), msg="notify never called")
    argv = [json.loads(l) for l in open(calls, encoding="utf-8")]
    assert argv == [["a,b", "batch", "none", "2/2 sessions finished"]], argv
    print("batch notify argv - OK")
    print("OK: F13 dispatch batch tests passed.")


class TasksForTests(unittest.TestCase):
    """F23-A: tasks_for normalizes question titles into
    {"feature": "F13"|None, "kind": "sign-off"|"dispatch"|"question", "title": ...}."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from dispatch import tasks_for  # noqa: E402

        self.tasks_for = tasks_for

    def _run(self, titles):
        question_project = {str(i): {"project": "p", "title": t} for i, t in enumerate(titles)}
        unconsumed = [{"record": {"question_id": str(i)}} for i in range(len(titles))]
        return self.tasks_for(unconsumed, question_project)

    def test_sign_off_prefix_extracts_feature_after_project(self):
        title = "[sign-off] offer-radar F13: 查詢…"
        self.assertEqual(
            self._run([title]), [{"feature": "F13", "kind": "sign-off", "title": title}]
        )

    def test_dispatch_prefix_feature_is_null(self):
        title = "[dispatch] mission-control: run tonight? (4 signed failing: F11, F13)"
        self.assertEqual(
            self._run([title]), [{"feature": None, "kind": "dispatch", "title": title}]
        )

    def test_no_prefix_is_question_kind_with_leading_feature(self):
        title = "F22 要走 agy subprocess…"
        self.assertEqual(
            self._run([title]), [{"feature": "F22", "kind": "question", "title": title}]
        )

    def test_title_without_a_feature_token_is_null(self):
        self.assertEqual(
            self._run(["Pick a datastore"]),
            [{"feature": None, "kind": "question", "title": "Pick a datastore"}],
        )

    def test_same_feature_and_kind_deduplicated_keeping_first(self):
        titles = [
            "[sign-off] offer-radar F13: first copy",
            "[sign-off] offer-radar F13: stale duplicate",
            "[sign-off] offer-radar F14: different feature",
        ]
        self.assertEqual(
            self._run(titles),
            [
                {"feature": "F13", "kind": "sign-off", "title": titles[0]},
                {"feature": "F14", "kind": "sign-off", "title": titles[2]},
            ],
        )

    def test_null_feature_entries_are_not_deduplicated(self):
        titles = ["Pick a datastore", "Pick a datastore"]
        self.assertEqual(len(self._run(titles)), 2)


class IsAncestorWorkspaceTests(unittest.TestCase):
    """F35: is_ancestor_workspace() - strict path-component prefix check
    used by brief-init's per-project Orca binding question."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from dispatch import is_ancestor_workspace  # noqa: E402

        self.is_ancestor_workspace = is_ancestor_workspace

    def test_strict_prefix_is_ancestor(self):
        base = tempfile.mkdtemp(prefix="brief-anc-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        child = os.path.join(base, "proj")
        os.makedirs(child)
        self.assertTrue(self.is_ancestor_workspace(base, child))

    def test_equal_paths_not_ancestor(self):
        base = tempfile.mkdtemp(prefix="brief-anc-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.assertFalse(self.is_ancestor_workspace(base, base))

    def test_trailing_slash_difference_still_matches(self):
        base = tempfile.mkdtemp(prefix="brief-anc-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        child = os.path.join(base, "proj")
        os.makedirs(child)
        self.assertTrue(self.is_ancestor_workspace(base + os.sep, child))

    @unittest.skipUnless(sys.platform == "win32", "os.path.normcase only folds case on Windows")
    def test_case_difference_still_matches_on_windows(self):
        base = tempfile.mkdtemp(prefix="brief-anc-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        child = os.path.join(base, "proj")
        os.makedirs(child)
        self.assertTrue(self.is_ancestor_workspace(base.upper(), child))

    def test_unrelated_paths_not_ancestor(self):
        a = tempfile.mkdtemp(prefix="brief-anc-a-")
        b = tempfile.mkdtemp(prefix="brief-anc-b-")
        self.addCleanup(shutil.rmtree, a, ignore_errors=True)
        self.addCleanup(shutil.rmtree, b, ignore_errors=True)
        self.assertFalse(self.is_ancestor_workspace(a, b))


class DelegateDefaultTests(unittest.TestCase):
    """F25: dispatch.delegate defaults to False (no config, or a dispatch
    section missing the key); an explicit `delegate: true` in config.json
    restores the DELEGATION_SENTENCE + no --disallowedTools behavior."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402

        self.dispatch = dispatch

    def test_no_config_defaults_to_no_delegation(self):
        settings = self.dispatch.dispatch_settings({})
        self.assertFalse(settings["delegate"])
        prompt = self.dispatch.build_prompt("p", [], delegate=settings["delegate"])
        self.assertIn(self.dispatch.NO_DELEGATION_SENTENCE, prompt)
        args = self.dispatch.claude_args(settings, "/tmp/brief")
        self.assertIn("--disallowedTools", args)
        self.assertIn("Agent", args)

    def test_dispatch_section_without_delegate_key_defaults_false(self):
        settings = self.dispatch.dispatch_settings({"dispatch": {"watch": True}})
        self.assertFalse(settings["delegate"])

    def test_config_delegate_true_restores_delegation_sentence(self):
        settings = self.dispatch.dispatch_settings({"dispatch": {"delegate": True}})
        self.assertTrue(settings["delegate"])
        prompt = self.dispatch.build_prompt("p", [], delegate=settings["delegate"])
        self.assertIn(self.dispatch.DELEGATION_SENTENCE, prompt)
        args = self.dispatch.claude_args(settings, "/tmp/brief")
        self.assertNotIn("--disallowedTools", args)


class SpawnProcessGroupTests(unittest.TestCase):
    """F32: spawn() starts its child in its own process group (Windows:
    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP; POSIX: start_new_session),
    the same isolation spawn_waiter already uses, so a process-group-based
    stop of the dispatching service doesn't kill the session it just
    started."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402

        self.dispatch = dispatch

    def test_spawn_isolates_into_its_own_process_group(self):
        with mock.patch.object(self.dispatch.subprocess, "Popen") as popen:
            popen.return_value.stdin = mock.Mock()
            with tempfile.TemporaryDirectory() as tmp:
                self.dispatch.spawn("claude", tmp, "prompt",
                                     os.path.join(tmp, "logs", "x.log"), [])
            kwargs = popen.call_args.kwargs
        if sys.platform == "win32":
            self.assertEqual(
                kwargs.get("creationflags"),
                self.dispatch.DETACHED_PROCESS | self.dispatch.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertIs(kwargs.get("start_new_session"), True)


class PidExitCodeUnknownTests(unittest.TestCase):
    """F32: a pid whose real exit code cannot be looked up (Windows:
    OpenProcess fails; POSIX: os.kill reports ProcessLookupError) must not be
    reported as exit code 0."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402

        self.dispatch = dispatch

    def test_unreachable_pid_is_not_reported_as_zero(self):
        # No OS hands out this PID; OpenProcess/os.kill both report
        # "no such process" for it, which is exactly the "gone but
        # undeterminable" case this sentinel exists for.
        code = self.dispatch.pid_exit_code(999999999)
        self.assertIsNotNone(code)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, self.dispatch.EXIT_CODE_UNKNOWN)


class WaitBatchUnknownExitTests(unittest.TestCase):
    """F32: wait_batch() writes EXIT_CODE_UNKNOWN as JSON null in the
    finished record's exit_codes, distinguishable from a real exit(0)."""

    def test_undeterminable_exit_code_recorded_as_null(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402

        home = tempfile.mkdtemp()
        with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": []}, f)
        dispatch.wait_batch(home, "batch-unknown", [999999999])
        recs = [json.loads(l) for l in open(os.path.join(home, "dispatches.jsonl"), encoding="utf-8")]
        finished = [r for r in recs if r["type"] == "finished"][0]
        self.assertEqual(finished["exit_codes"], [None])


def _write_immediate_exit_claude(directory):
    """A stand-in for the real `claude` binary for --watch mode: exits
    immediately, ignoring all args/stdin (same shape as
    test_dispatch_trust.py's own helper, duplicated here rather than
    imported - each test file in this repo is runnable standalone)."""
    if sys.platform == "win32":
        wrapper = os.path.join(directory, "claude.cmd")
        with open(wrapper, "w") as f:
            f.write(f'@"{sys.executable}" -c "pass"\n')
        return wrapper
    wrapper = os.path.join(directory, "claude")
    with open(wrapper, "w") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" -c "pass"\n')
    os.chmod(wrapper, 0o755)
    return wrapper


# F34: a real subprocess stand-in for the `orca` CLI, reached only via PATH
# (never a python-level mock of run_orca()) so these tests also prove
# dispatch.py's shutil.which()-based resolution actually finds a fake
# ".cmd"/".bat" shim the way a real npm-style Orca install would ship one -
# a bare "orca" argv[0] does NOT get auto-resolved to ".cmd"/".bat" by
# Windows CreateProcess (only ".exe" is auto-appended for an extension-less
# name), so this also guards against dispatch.py regressing back to that.
#
# Every invocation's argv is appended (as one JSON array) to FAKE_ORCA_LOG.
# Canned responses come from the JSON object at FAKE_ORCA_CONFIG (missing/
# absent keys fall back to a generic success shape); `terminal create`
# additionally supports a `terminal_create_responses` list, indexed by how
# many `terminal create` calls have been logged so far (including this
# one), so a test can script "fails once, then succeeds" for the
# selector_not_found -> `repo add` -> retry path.
FAKE_ORCA_PY = '''
import json, os, sys

argv = sys.argv[1:]
log_path = os.environ["FAKE_ORCA_LOG"]
with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + chr(10))

config = {}
config_path = os.environ.get("FAKE_ORCA_CONFIG")
if config_path and os.path.exists(config_path):
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)


def respond(obj):
    print(json.dumps(obj))
    sys.exit(0)


if argv[:1] == ["status"]:
    respond(config.get("status", {"ok": True, "result": {
        "app": {"running": True}, "runtime": {"state": "ready"}}}))
if argv[:2] == ["terminal", "create"]:
    with open(log_path, encoding="utf-8") as f:
        prior = [json.loads(l) for l in f if l.strip()]
    n = sum(1 for c in prior if c[:2] == ["terminal", "create"])
    responses = config.get("terminal_create_responses")
    if responses:
        respond(responses[min(n - 1, len(responses) - 1)])
    respond(config.get("terminal_create", {"ok": True, "result": {
        "terminal": {"handle": "term_fake1", "tabId": 1}}}))
if argv[:2] == ["repo", "add"]:
    respond(config.get("repo_add", {"ok": True, "result": {}}))
if argv[:2] == ["repo", "list"]:
    respond(config.get("repo_list", {"ok": True, "result": {"repos": []}}))
if argv[:2] == ["terminal", "wait"]:
    respond(config.get("terminal_wait", {"ok": True, "result": {"wait": {
        "satisfied": True, "status": "exited", "exitCode": 0}}}))
respond({"ok": False, "error": {"code": "unknown_command"}})
'''


def _write_fake_orca(directory):
    script_path = os.path.join(directory, "fake_orca.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(FAKE_ORCA_PY)
    if os.name == "nt":
        wrapper_path = os.path.join(directory, "orca.cmd")
        with open(wrapper_path, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n')
        return wrapper_path
    wrapper_path = os.path.join(directory, "orca")
    with open(wrapper_path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n')
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


class RunOrcaNoWindowTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402

        self.dispatch = dispatch

    def test_run_orca_passes_create_no_window_flag(self):
        dispatch = self.dispatch
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

        with mock.patch.object(dispatch.subprocess, "run", fake_run):
            self.assertEqual(dispatch.run_orca(["status"]), {"ok": True})
        self.assertEqual(seen.get("creationflags"), getattr(subprocess, "CREATE_NO_WINDOW", 0))


class OrcaWindowTests(unittest.TestCase):
    """F34: dispatch.window: "orca" opens --watch sessions in an Orca
    terminal tab (`orca terminal create`) instead of a native console
    window; headless (-p) dispatch never touches orca either way."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import dispatch  # noqa: E402
        self.dispatch = dispatch

        self.home = tempfile.mkdtemp(prefix="brief-home-")
        self.orca_dir = tempfile.mkdtemp(prefix="fake-orca-")
        self.project_dir = tempfile.mkdtemp(prefix="brief-project-")
        self.log_path = os.path.join(self.orca_dir, "orca-calls.jsonl")
        self.config_path = os.path.join(self.orca_dir, "orca-config.json")
        self.claude_path = _write_immediate_exit_claude(self.orca_dir)
        _write_fake_orca(self.orca_dir)

        with open(os.path.join(self.home, "claude.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": {}}, f)

        self._orig_environ = dict(os.environ)
        os.environ["BRIEF_HOME"] = self.home
        os.environ["BRIEF_CLAUDE_CMD"] = self.claude_path
        os.environ["BRIEF_CLAUDE_JSON"] = os.path.join(self.home, "claude.json")
        os.environ["FAKE_ORCA_LOG"] = self.log_path
        os.environ["FAKE_ORCA_CONFIG"] = self.config_path
        os.environ["PATH"] = self.orca_dir + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_environ)
        for d in (self.home, self.orca_dir, self.project_dir):
            shutil.rmtree(d, ignore_errors=True)

    def _write_config(self, projects, window="orca"):
        dispatch_cfg = {} if window is None else {"window": window}
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": projects, "collector": None, "notify": None,
                       "dispatch": dispatch_cfg}, f)

    def _set_orca_config(self, cfg):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def _log_entries(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def _started_records(self):
        path = os.path.join(self.home, "dispatches.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [r for r in map(json.loads, f) if r.get("type") == "started"]

    # -- AC1: console (default/missing key) is unaffected -----------------
    def test_window_missing_key_no_orca_call_and_pid_present(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "a", "path": self.project_dir}],
                       "collector": None, "notify": None}, f)

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        self.assertEqual(self._log_entries(), [])
        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0]["pid"])
        self.assertNotIn("terminal", started[0])

    # -- AC2/AC4: headless never touches orca, even with window: "orca" ---
    def test_headless_with_window_orca_never_calls_orca(self):
        self._write_config([{"name": "a", "path": self.project_dir}])

        rc = self.dispatch.main(["--no-watch", "a"])

        self.assertEqual(rc, 0)
        self.assertEqual(self._log_entries(), [])
        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0]["pid"])
        self.assertNotIn("terminal", started[0])

    # -- AC2: repo-mode terminal create argv -------------------------------
    def test_orca_repo_mode_terminal_create_argv(self):
        self._write_config([{"name": "a", "path": self.project_dir}])

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        creates = [e for e in self._log_entries() if e[:2] == ["terminal", "create"]]
        self.assertEqual(len(creates), 1)
        argv = creates[0]
        self.assertEqual(argv[argv.index("--worktree") + 1], f"path:{self.project_dir}")
        self.assertEqual(argv[argv.index("--title") + 1], "a")
        command = argv[argv.index("--command") + 1]
        prompt_path = os.path.join(self.home, "prompts", f"{os.path.basename(self.project_dir)}.md")
        self.assertIn(f"Read {prompt_path} and follow it as your task brief.", command)
        self.assertIn(os.path.basename(self.claude_path), command)

        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNone(started[0]["pid"])
        self.assertEqual(started[0]["terminal"], "term_fake1")

    # -- AC3: selector_not_found -> repo add -> retry ----------------------
    def test_selector_not_found_triggers_repo_add_then_retry(self):
        self._write_config([{"name": "a", "path": self.project_dir}])
        self._set_orca_config({"terminal_create_responses": [
            {"ok": False, "error": {"code": "selector_not_found"}},
            {"ok": True, "result": {"terminal": {"handle": "term_retry", "tabId": 2}}},
        ]})

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        entries = self._log_entries()
        create_pos = [i for i, e in enumerate(entries) if e[:2] == ["terminal", "create"]]
        add_pos = [i for i, e in enumerate(entries) if e[:2] == ["repo", "add"]]
        self.assertEqual(len(create_pos), 2)
        self.assertEqual(len(add_pos), 1)
        self.assertTrue(create_pos[0] < add_pos[0] < create_pos[1])
        self.assertEqual(entries[add_pos[0]][entries[add_pos[0]].index("--path") + 1], self.project_dir)

        started = self._started_records()
        self.assertEqual(started[0]["terminal"], "term_retry")

    def test_selector_not_found_falls_back_to_console_when_retry_also_fails(self):
        self._write_config([{"name": "a", "path": self.project_dir}])
        self._set_orca_config({"terminal_create_responses": [
            {"ok": False, "error": {"code": "selector_not_found"}},
            {"ok": False, "error": {"code": "selector_not_found"}},
        ]})

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        entries = self._log_entries()
        self.assertEqual(len([e for e in entries if e[:2] == ["terminal", "create"]]), 2)
        self.assertEqual(len([e for e in entries if e[:2] == ["repo", "add"]]), 1)
        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0]["pid"])
        self.assertNotIn("terminal", started[0])

    # -- AC2: bind mode -----------------------------------------------------
    def test_bind_mode_argv_has_id_worktree_and_cd_prefix(self):
        self._write_config([{"name": "a", "path": self.project_dir,
                             "orca": {"mode": "bind", "repo_id": "repo-1"}}])
        self._set_orca_config({"repo_list": {"ok": True, "result": {"repos": [
            {"id": "repo-1", "path": "C:/some/bound/repo", "displayName": "x"}]}}})

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        creates = [e for e in self._log_entries() if e[:2] == ["terminal", "create"]]
        self.assertEqual(len(creates), 1)
        argv = creates[0]
        self.assertEqual(argv[argv.index("--worktree") + 1], "id:repo-1::C:/some/bound/repo")
        command = argv[argv.index("--command") + 1]
        self.assertIn(f'cd "{self.project_dir}"', command)

        started = self._started_records()
        self.assertEqual(started[0]["terminal"], "term_fake1")

    def test_bind_mode_unknown_repo_id_falls_back_to_console(self):
        self._write_config([{"name": "a", "path": self.project_dir,
                             "orca": {"mode": "bind", "repo_id": "missing-repo"}}])
        self._set_orca_config({"repo_list": {"ok": True, "result": {"repos": []}}})

        rc = self.dispatch.main(["--watch", "a"])

        self.assertEqual(rc, 0)
        creates = [e for e in self._log_entries() if e[:2] == ["terminal", "create"]]
        self.assertEqual(creates, [])
        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0]["pid"])
        self.assertNotIn("terminal", started[0])

    # -- AC4: orca not running -> whole batch falls back -------------------
    def test_orca_status_not_running_falls_back_whole_batch(self):
        project_b = tempfile.mkdtemp(prefix="brief-project-")
        self.addCleanup(shutil.rmtree, project_b, ignore_errors=True)
        self._write_config([{"name": "a", "path": self.project_dir},
                             {"name": "b", "path": project_b}])
        self._set_orca_config({"status": {"ok": False, "error": {"code": "not_running"}}})

        rc = self.dispatch.main(["--watch", "a", "b"])

        self.assertEqual(rc, 0)
        entries = self._log_entries()
        self.assertEqual(len(entries), 1)  # only the status preflight
        self.assertEqual(entries[0][:1], ["status"])
        self.assertEqual([e for e in entries if e[:2] == ["terminal", "create"]], [])
        started = self._started_records()
        self.assertEqual(len(started), 2)
        for rec in started:
            self.assertIsNotNone(rec["pid"])
            self.assertNotIn("terminal", rec)

    # -- F35: --no-orca forces console windows, config.json untouched ------
    def test_no_orca_flag_skips_orca_even_when_window_orca(self):
        self._write_config([{"name": "a", "path": self.project_dir}])
        config_path = os.path.join(self.home, "config.json")
        before = open(config_path, encoding="utf-8").read()

        rc = self.dispatch.main(["--watch", "--no-orca", "a"])

        self.assertEqual(rc, 0)
        self.assertEqual(self._log_entries(), [])  # orca never invoked at all
        started = self._started_records()
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0]["pid"])
        self.assertNotIn("terminal", started[0])
        self.assertEqual(open(config_path, encoding="utf-8").read(), before)

    # -- AC6: waiter on terminal records ------------------------------------
    def test_wait_batch_terminal_record_writes_finished_line(self):
        self._set_orca_config({"terminal_wait": {"ok": True, "result": {"wait": {
            "satisfied": True, "status": "exited", "exitCode": 7}}}})

        rc = self.dispatch.wait_batch(self.home, "batch-1", ["term_x"])

        self.assertEqual(rc, 0)
        recs = [json.loads(l) for l in open(os.path.join(self.home, "dispatches.jsonl"), encoding="utf-8")]
        fin = [r for r in recs if r["type"] == "finished"][0]
        self.assertEqual(fin["exit_codes"], [7])
        waits = [e for e in self._log_entries() if e[:2] == ["terminal", "wait"]]
        self.assertEqual(len(waits), 1)
        self.assertIn("term_x", waits[0])

    def test_wait_batch_mixed_terminal_then_pid_aligns_exit_codes(self):
        self._set_orca_config({"terminal_wait": {"ok": True, "result": {"wait": {
            "satisfied": True, "status": "exited", "exitCode": 3}}}})

        rc = self.dispatch.wait_batch(self.home, "batch-mix", ["term_first", "999999999"])

        self.assertEqual(rc, 0)
        recs = [json.loads(l) for l in open(os.path.join(self.home, "dispatches.jsonl"), encoding="utf-8")]
        fin = [r for r in recs if r["type"] == "finished"][0]
        self.assertEqual(fin["exit_codes"][0], 3)
        self.assertIsNone(fin["exit_codes"][1])  # unreachable pid -> EXIT_CODE_UNKNOWN -> null


if __name__ == "__main__":
    main()
    print()
    unittest.main(verbosity=2)
