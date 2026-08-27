"""F13: dispatch.py records started/finished lines and notifies once per batch.
Run: python tests/test_dispatch_batch.py  (stdlib only; fake claude = sleep 1)
F23-A: tasks_for's {feature, kind, title} normalization - run via
`python -m unittest tests.test_dispatch_batch`.
F32: spawn() isolates its child into its own process group (so a
service restart doesn't kill it), and pid_exit_code()/wait_batch() report an
undeterminable exit code as EXIT_CODE_UNKNOWN / JSON null rather than 0."""
import json
import os
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


if __name__ == "__main__":
    main()
    print()
    unittest.main(verbosity=2)
