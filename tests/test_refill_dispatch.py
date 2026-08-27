"""F28: tests for scripts/dispatch.py's context-refill entry point
(refill_question(), reached by the CLI's `--refill <question_id>`).

Run: python -m unittest tests.test_refill_dispatch -v  (stdlib only)

BRIEF_HOME is a fresh temp directory per test; real ~/.agent-brief is never
touched. `dispatch.spawn` / `dispatch.spawn_waiter` are mocked so no real
`claude` process is ever spawned - refill_question()'s own logic (which
project/args/prompt it builds, what it writes to dispatches.jsonl, the
dedup check) is what is under test here, not subprocess plumbing (already
covered by test_dispatch_batch.py / test_dispatch_trust.py).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import dispatch  # noqa: E402


class RefillDispatchTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="brief-home-")
        self.project_dir = tempfile.mkdtemp(prefix="brief-project-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.project_dir, ignore_errors=True)

    def _write_config(self, dispatch_extra=None, project="demo"):
        config = {"projects": [{"name": project, "path": self.project_dir}],
                  "collector": None, "notify": None}
        if dispatch_extra is not None:
            config["dispatch"] = dispatch_extra
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f)

    def _add_question(self, session_id=None, title="Pick a datastore", project="demo"):
        qid = str(uuid.uuid4())
        record = {"type": "question", "id": qid, "project": project, "title": title,
                  "body": "b", "severity": "normal", "created_at": "2026-08-27T00:00:00Z"}
        if session_id:
            record["session_id"] = session_id
        with open(os.path.join(self.home, "inbox.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return qid

    def _dispatches(self):
        path = os.path.join(self.home, "dispatches.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _write_dispatches(self, records):
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    # -- session_id present/absent -> resume vs fresh -----------------------
    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_session_id_present_resumes(self, mock_spawn, mock_waiter):
        self._write_config()
        mock_spawn.return_value = mock.Mock(pid=4242)
        qid = self._add_question(session_id="sess-123")

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertTrue(result["ok"], result)
        claude_cmd, cwd, prompt, log_path, args = mock_spawn.call_args[0]
        self.assertEqual(claude_cmd, "claude")
        self.assertEqual(cwd, self.project_dir)
        self.assertEqual(args[:2], ["--resume", "sess-123"])
        mock_waiter.assert_called_once()

    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_no_session_id_fresh_spawn(self, mock_spawn, mock_waiter):
        self._write_config()
        mock_spawn.return_value = mock.Mock(pid=4243)
        qid = self._add_question(session_id=None)

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertTrue(result["ok"], result)
        _, cwd, _, _, args = mock_spawn.call_args[0]
        self.assertEqual(cwd, self.project_dir)
        self.assertNotIn("--resume", args)

    # -- --model uses context_model, defaulting to sonnet --------------------
    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_model_defaults_to_sonnet_when_missing(self, mock_spawn, mock_waiter):
        self._write_config()  # no "dispatch" key at all
        mock_spawn.return_value = mock.Mock(pid=1)
        qid = self._add_question()

        dispatch.refill_question(self.home, "claude", qid)

        args = mock_spawn.call_args[0][4]
        self.assertEqual(args[args.index("--model") + 1], "sonnet")

    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_model_defaults_to_sonnet_when_null(self, mock_spawn, mock_waiter):
        self._write_config(dispatch_extra={"context_model": None})
        mock_spawn.return_value = mock.Mock(pid=1)
        qid = self._add_question()

        dispatch.refill_question(self.home, "claude", qid)

        args = mock_spawn.call_args[0][4]
        self.assertEqual(args[args.index("--model") + 1], "sonnet")

    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_model_uses_configured_context_model(self, mock_spawn, mock_waiter):
        self._write_config(dispatch_extra={"context_model": "haiku", "model": "opus"})
        mock_spawn.return_value = mock.Mock(pid=1)
        qid = self._add_question()

        dispatch.refill_question(self.home, "claude", qid)

        args = mock_spawn.call_args[0][4]
        self.assertEqual(args[args.index("--model") + 1], "haiku")  # independent of dispatch.model

    # -- allowlist excludes Edit/Write ---------------------------------------
    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_allowlist_excludes_edit_and_write(self, mock_spawn, mock_waiter):
        self._write_config()
        mock_spawn.return_value = mock.Mock(pid=1)
        qid = self._add_question()

        dispatch.refill_question(self.home, "claude", qid)

        args = mock_spawn.call_args[0][4]
        allowed = args[args.index("--allowedTools") + 1]
        self.assertEqual(allowed, "Bash,Read,Glob,Grep,Skill")
        self.assertNotIn("Edit", allowed.split(","))
        self.assertNotIn("Write", allowed.split(","))
        self.assertIn("--disallowedTools", args)
        self.assertEqual(args[args.index("--disallowedTools") + 1], "Agent")

    # -- prompt contents ------------------------------------------------------
    def test_prompt_has_template_and_cancel_instruction(self):
        question = {"id": "q-1", "project": "demo", "title": "Pick a datastore"}
        raw_line = json.dumps(question)

        prompt = dispatch.build_refill_prompt(question, raw_line)

        self.assertIn("Context:", prompt)
        self.assertIn("Options:", prompt)
        self.assertIn("Recommendation:", prompt)
        self.assertIn("q-1", prompt)
        self.assertIn("Pick a datastore", prompt)
        self.assertIn('"status": "cancelled"', prompt)

    # -- dedup: running refill batch blocks a second spawn --------------------
    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_dedup_blocks_duplicate_spawn(self, mock_spawn, mock_waiter):
        self._write_config()
        title = "Pick a datastore"
        qid = self._add_question(title=title)
        batch_id = str(uuid.uuid4())
        self._write_dispatches([{
            "type": "started", "batch_id": batch_id, "project": "demo",
            "pid": os.getpid(), "started_at": "2026-08-27T00:00:00Z", "log": "/tmp/x.log",
            "tasks": [{"feature": None, "kind": "refill", "title": title}],
        }])
        before = self._dispatches()

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result.get("already_running"))
        mock_spawn.assert_not_called()
        mock_waiter.assert_not_called()
        self.assertEqual(self._dispatches(), before)  # no new started line

    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_finished_batch_does_not_block_a_new_refill(self, mock_spawn, mock_waiter):
        self._write_config()
        title = "Pick a datastore"
        qid = self._add_question(title=title)
        batch_id = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch_id, "project": "demo",
             "pid": 999999999, "started_at": "2026-08-27T00:00:00Z", "log": "/tmp/x.log",
             "tasks": [{"feature": None, "kind": "refill", "title": title}]},
            {"type": "finished", "batch_id": batch_id,
             "finished_at": "2026-08-27T00:05:00Z", "exit_codes": [0]},
        ])
        mock_spawn.return_value = mock.Mock(pid=1)

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertTrue(result["ok"], result)
        self.assertNotIn("already_running", result)
        mock_spawn.assert_called_once()

    # -- writes exactly one started line with kind "refill" -------------------
    @mock.patch("dispatch.spawn_waiter")
    @mock.patch("dispatch.spawn")
    def test_started_line_has_refill_task_and_no_answer_or_status_written(self, mock_spawn, mock_waiter):
        self._write_config()
        mock_spawn.return_value = mock.Mock(pid=777)
        title = "F9 要走哪個 datastore"
        qid = self._add_question(title=title)

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertTrue(result["ok"], result)
        started = [r for r in self._dispatches() if r["type"] == "started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["tasks"], [{"feature": "F9", "kind": "refill", "title": title}])
        self.assertEqual(started[0]["pid"], 777)
        # never writes to inbox.jsonl / answers.jsonl itself
        self.assertFalse(os.path.exists(os.path.join(self.home, "answers.jsonl")))
        with open(os.path.join(self.home, "inbox.jsonl"), encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 1)  # only the original question, no status appended

    # -- error paths ------------------------------------------------------------
    @mock.patch("dispatch.spawn")
    def test_unknown_question_id_is_an_error_no_spawn(self, mock_spawn):
        self._write_config()

        result = dispatch.refill_question(self.home, "claude", str(uuid.uuid4()))

        self.assertFalse(result["ok"])
        self.assertIn("unknown question id", result["error"])
        mock_spawn.assert_not_called()

    @mock.patch("dispatch.spawn")
    def test_unknown_project_is_an_error_no_spawn(self, mock_spawn):
        self._write_config(project="demo")
        qid = self._add_question(project="ghost-project")

        result = dispatch.refill_question(self.home, "claude", qid)

        self.assertFalse(result["ok"])
        self.assertIn("unknown project", result["error"])
        mock_spawn.assert_not_called()

    def test_cli_refill_missing_argument_is_usage_error(self):
        self.assertEqual(dispatch.main(["--refill"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
