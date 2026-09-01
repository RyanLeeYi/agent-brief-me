"""Tests for the F8 Web UI backend (`brief_me.py serve`): the GET/POST
/api/* JSON contract and the `reopened` status folding rule.

Runs standalone: `python tests/test_brief_me_api.py`. stdlib unittest only.
BRIEF_HOME is pointed at a fresh temp directory per test; the server binds
127.0.0.1 on a random port (port 0) in a background thread. Dispatch tests
point BRIEF_CLAUDE_CMD at a fake "claude" so no real `claude` is invoked.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import brief_me  # noqa: E402

# F36: reuses test_dispatch_batch.py's real subprocess stand-in for the orca
# CLI (_write_fake_orca/FAKE_ORCA_PY) rather than a second copy - see
# BriefMeOrcaAPITestCase below.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_dispatch_batch  # noqa: E402


def _write_fake_claude(directory):
    """A stand-in for the real `claude` binary, for dispatch.py's
    BRIEF_CLAUDE_CMD hook. Headless mode (argv[1] == "-p") drains stdin
    before exiting, so dispatch.py's blocking stdin write never sees a
    broken pipe; --watch mode (any other first arg) exits immediately
    without touching stdin. subprocess.Popen can't launch a .bat directly
    without an absolute path, and can't launch a bare .py at all on
    Windows, so the .bat wraps `sys.executable fake_claude.py`.
    """
    script_path = os.path.join(directory, "fake_claude.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == '-p':\n"
            "    sys.stdin.read()\n"
            "sys.exit(0)\n"
        )
    if os.name == "nt":
        wrapper_path = os.path.join(directory, "fake_claude.bat")
        with open(wrapper_path, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n')
        return wrapper_path
    wrapper_path = os.path.join(directory, "fake_claude.sh")
    with open(wrapper_path, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n')
    os.chmod(wrapper_path, 0o755)
    return wrapper_path


class BriefMeAPITestCase(unittest.TestCase):
    """Each test gets a fresh BRIEF_HOME, inbox, and server instance."""

    def setUp(self):
        self._orig_env = dict(os.environ)
        self.home = tempfile.mkdtemp(prefix="brief-home-")
        self.fake_claude_dir = tempfile.mkdtemp(prefix="fake-claude-")
        os.environ["BRIEF_HOME"] = self.home
        os.environ["BRIEF_CLAUDE_CMD"] = _write_fake_claude(self.fake_claude_dir)

        self.server = brief_me.make_server(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.environ.clear()
        os.environ.update(self._orig_env)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.fake_claude_dir, ignore_errors=True)

    # -- fixtures ----------------------------------------------------
    def _inbox_path(self):
        return os.path.join(self.home, "inbox.jsonl")

    def _answers_path(self):
        return os.path.join(self.home, "answers.jsonl")

    def _add_question(self, project="demo", title="Pick one", body="body text",
                       severity="normal", choices=None, multi=False,
                       recommendation=None, session_id=None):
        record = {
            "type": "question",
            "id": str(uuid.uuid4()),
            "project": project,
            "title": title,
            "body": body,
            "severity": severity,
            "created_at": brief_me._now(),
        }
        if choices is not None:
            record["choices"] = choices
        if multi:
            record["multi"] = True
        if recommendation is not None:
            record["recommendation"] = recommendation
        if session_id is not None:
            record["session_id"] = session_id
        brief_me.atomic_append_line(self._inbox_path(), record)
        return record["id"]

    def _add_report(self, project="demo", summary="a report", severity="low"):
        record = {
            "type": "report",
            "id": str(uuid.uuid4()),
            "project": project,
            "summary": summary,
            "severity": severity,
            "created_at": brief_me._now(),
        }
        brief_me.atomic_append_line(self._inbox_path(), record)
        return record["id"]

    def _write_config(self, projects=None, collector=None, dispatch=None):
        config = {"projects": projects or []}
        if collector is not None:
            config["collector"] = collector
        if dispatch is not None:
            config["dispatch"] = dispatch
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f)

    def _file_sizes(self):
        return (
            os.path.getsize(self._inbox_path()) if os.path.exists(self._inbox_path()) else 0,
            os.path.getsize(self._answers_path()) if os.path.exists(self._answers_path()) else 0,
        )

    def _request(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, body):
        return self._request("POST", path, body)

    # -- GET / ---------------------------------------------------------
    def test_index_serves_html_or_500_when_missing(self):
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        try:
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIn(b"<html", resp.read().lower())
        except urllib.error.HTTPError as e:
            # brief_me.html absent (e.g. a worktree without F9): must be a JSON 500
            self.assertEqual(e.code, 500)
            data = json.loads(e.read().decode("utf-8"))
            self.assertFalse(data["ok"]); self.assertIn("error", data)

    # -- GET /api/state --------------------------------------------------
    def test_state_shape_and_multi_options(self):
        qid = self._add_question(choices=["A", "B"], multi=True, recommendation="A",
                                 session_id="sess-1")
        self._add_report()
        self._write_config(projects=[{"name": "demo", "path": self.home}])

        status, data = self._get("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("ok", data)  # same shape as CLI `load`, no ok wrapper
        self.assertIsNone(data["collector_warning"])
        self.assertEqual(data["projects"], ["demo"])
        self.assertEqual(data["unconsumed_counts"], {})
        self.assertEqual(data["dismissed"], {})

        q = data["pending_questions"]["demo"][0]
        self.assertEqual(q["id"], qid)
        self.assertTrue(q["multi"])
        self.assertEqual(q["options"], ["A", "B"])

    def test_state_does_not_run_collector_but_collect_does(self):
        sentinel = os.path.join(self.home, "sentinel.txt")
        self._write_config(collector=[sys.executable, "-c",
                                       "open('sentinel.txt', 'w').close()"])

        for _ in range(3):
            status, data = self._get("/api/state")
            self.assertEqual(status, 200)
            self.assertIsNone(data["collector_warning"])
        self.assertFalse(os.path.exists(sentinel))

        status, data = self._post("/api/collect", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIsNone(data["collector_warning"])
        self.assertTrue(os.path.exists(sentinel))

    # -- POST /api/answer --------------------------------------------------
    def test_answer_single_choice(self):
        qid = self._add_question(choices=["A", "B"])
        status, data = self._post("/api/answer", {"answers": [{"id": qid, "chosen": "A"}]})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "saved": 1, "dismissed": 0,
                                "dispatched": [], "log_paths": []})

        answers = [json.loads(ln) for ln in brief_me.iter_lines(self._answers_path())]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["question_id"], qid)
        self.assertEqual(answers[0]["chosen"], "A")
        self.assertNotIn("free_text", answers[0])

        _, state = self._get("/api/state")
        self.assertNotIn("demo", state["pending_questions"])

    def test_answer_multi_choice(self):
        qid = self._add_question(choices=["A", "B", "C"], multi=True)
        status, data = self._post("/api/answer", {
            "answers": [{"id": qid, "chosen": ["A", "C"], "free_text": "note"}]})
        self.assertEqual(status, 200)

        answers = [json.loads(ln) for ln in brief_me.iter_lines(self._answers_path())]
        self.assertEqual(answers[0]["chosen"], ["A", "C"])
        self.assertEqual(answers[0]["free_text"], "note")

    def test_answer_free_text_only(self):
        qid = self._add_question()
        status, data = self._post("/api/answer", {
            "answers": [{"id": qid, "free_text": "just text"}]})
        self.assertEqual(status, 200)

        answers = [json.loads(ln) for ln in brief_me.iter_lines(self._answers_path())]
        self.assertEqual(answers[0]["free_text"], "just text")
        self.assertNotIn("chosen", answers[0])

    # -- dismiss / reopen ----------------------------------------------
    def test_dismiss_appears_in_dismissed_with_dismissed_at(self):
        qid = self._add_question()
        status, data = self._post("/api/answer", {"dismiss": [qid]})
        self.assertEqual(status, 200)
        self.assertEqual(data["dismissed"], 1)
        self.assertEqual(data["saved"], 0)

        _, state = self._get("/api/state")
        self.assertNotIn("demo", state.get("pending_questions", {}))
        dismissed = state["dismissed"]["demo"]
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["id"], qid)
        self.assertIn("dismissed_at", dismissed[0])

    def test_reopen_then_dismiss_again(self):
        qid = self._add_question()
        self._post("/api/answer", {"dismiss": [qid]})
        first_dismissed_at = self._get("/api/state")[1]["dismissed"]["demo"][0]["dismissed_at"]

        status, data = self._post("/api/reopen", {"ids": [qid]})
        self.assertEqual(status, 200)
        self.assertEqual(data["reopened"], [qid])

        _, state = self._get("/api/state")
        self.assertEqual(state["pending_questions"]["demo"][0]["id"], qid)
        self.assertEqual(state["dismissed"], {})

        status, data = self._post("/api/answer", {"dismiss": [qid]})
        self.assertEqual(status, 200)

        _, state = self._get("/api/state")
        dismissed = state["dismissed"]["demo"]
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(dismissed[0]["id"], qid)
        self.assertIn("dismissed_at", dismissed[0])
        self.assertGreaterEqual(dismissed[0]["dismissed_at"], first_dismissed_at)

    # -- POST /api/read --------------------------------------------------
    def test_read_marks_report_and_clears_from_unread(self):
        rid = self._add_report()
        _, state = self._get("/api/state")
        self.assertEqual(len(state["unread_reports"]["demo"]), 1)

        status, data = self._post("/api/read", {"ids": [rid]})
        self.assertEqual(status, 200)
        self.assertEqual(data["read"], [rid])

        _, state = self._get("/api/state")
        self.assertNotIn("demo", state.get("unread_reports", {}))

    # -- 400 validation, file bytes unchanged ---------------------------
    def test_400_missing_chosen_and_free_text(self):
        qid = self._add_question()
        before = self._file_sizes()
        status, data = self._post("/api/answer", {"answers": [{"id": qid}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)
        self.assertEqual(self._file_sizes(), before)

    def test_400_id_conflict_between_answers_and_dismiss(self):
        qid = self._add_question(choices=["A"])
        before = self._file_sizes()
        status, data = self._post("/api/answer", {
            "answers": [{"id": qid, "chosen": "A"}], "dismiss": [qid]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._file_sizes(), before)

    def test_400_unknown_id(self):
        before = self._file_sizes()
        status, data = self._post("/api/answer", {
            "answers": [{"id": "does-not-exist", "chosen": "A"}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._file_sizes(), before)

    # -- dispatch ----------------------------------------------------------
    def test_dispatch_headless_returns_nonempty_log_paths(self):
        project_dir = tempfile.mkdtemp(prefix="brief-project-")
        try:
            self._write_config(projects=[{"name": "demo", "path": project_dir}])
            qid = self._add_question(choices=["A"])
            status, data = self._post("/api/answer", {
                "answers": [{"id": qid, "chosen": "A"}],
                "dispatch": {"projects": ["demo"], "watch": False}})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["dispatched"], ["demo"])
            self.assertEqual(len(data["log_paths"]), 1)
            self.assertNotIn("dispatch_error", data)
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_dispatch_watch_returns_empty_log_paths(self):
        project_dir = tempfile.mkdtemp(prefix="brief-project-")
        try:
            self._write_config(projects=[{"name": "demo", "path": project_dir}])
            qid = self._add_question(choices=["A"])
            status, data = self._post("/api/answer", {
                "answers": [{"id": qid, "chosen": "A"}],
                "dispatch": {"projects": ["demo"], "watch": True}})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["dispatched"], ["demo"])
            self.assertEqual(data["log_paths"], [])
            self.assertNotIn("dispatch_error", data)
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_dispatch_unknown_project_reports_error_but_saves_answer(self):
        project_dir = tempfile.mkdtemp(prefix="brief-project-")
        try:
            self._write_config(projects=[{"name": "demo", "path": project_dir}])
            qid = self._add_question(choices=["A"])
            status, data = self._post("/api/answer", {
                "answers": [{"id": qid, "chosen": "A"}],
                "dispatch": {"projects": ["demo", "ghost"], "watch": False}})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["dispatched"], ["demo"])
            self.assertIn("dispatch_error", data)
            self.assertIn("ghost", data["dispatch_error"])

            # dispatch.py appends a second "consumed: true" line for demo's
            # answer once it dispatches successfully (docs/schema.md's
            # answer-folding rule) - fold to the current value.
            answers_by_question = brief_me.fold_answers(self._answers_path())
            self.assertIn(qid, answers_by_question)
            self.assertEqual(answers_by_question[qid]["chosen"], "A")
            self.assertTrue(answers_by_question[qid]["consumed"])
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)

    # -- CLI `load` requeue (F24) ----------------------------------------
    def _dispatches_path(self):
        return os.path.join(self.home, "dispatches.jsonl")

    def _consumed_answer(self, qid, chosen="A"):
        """Question -> answered -> consumed, mirroring dispatch.py's own
        writes: an answer line, its "answered" status, then a second answer
        line flipping `consumed` to true."""
        answer, status = brief_me.build_answer_and_status(qid, chosen=chosen)
        brief_me.atomic_append_line(self._answers_path(), answer)
        brief_me.atomic_append_line(self._inbox_path(), status)
        brief_me.atomic_append_line(self._answers_path(), {**answer, "consumed": True})

    def _write_dispatches(self, records):
        with open(self._dispatches_path(), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    @staticmethod
    def _ts(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_load_requeues_started_batch_with_no_report(self):
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        data = brief_me._cmd_load([])

        self.assertEqual(data["requeued"], {"demo": ["Pick one"]})
        self.assertIn("demo", data["unconsumed_projects"])
        answers_by_question = brief_me.fold_answers(self._answers_path())
        self.assertFalse(answers_by_question[qid]["consumed"])
        self.assertEqual(answers_by_question[qid]["chosen"], "A")
        self.assertIn(batch, brief_me._requeued_batch_ids(self._dispatches_path()))

    def test_load_does_not_requeue_when_report_filed_after_started(self):
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])
        self._add_report(project="demo", summary="F1 passing")

        data = brief_me._cmd_load([])

        self.assertEqual(data["requeued"], {})
        answers_by_question = brief_me.fold_answers(self._answers_path())
        self.assertTrue(answers_by_question[qid]["consumed"])
        self.assertNotIn(batch, brief_me._requeued_batch_ids(self._dispatches_path()))

    def test_load_does_not_requeue_batch_twice(self):
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
            {"type": "requeued", "batch_id": batch, "at": self._ts(now)},
        ])

        data = brief_me._cmd_load([])

        self.assertEqual(data["requeued"], {})
        answers_by_question = brief_me.fold_answers(self._answers_path())
        self.assertTrue(answers_by_question[qid]["consumed"])  # untouched
        with open(self._dispatches_path(), encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 3)  # no duplicate "requeued" line appended

    def test_load_legacy_started_without_tasks_key_does_not_crash_or_requeue(self):
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        data = brief_me._cmd_load([])  # must not raise

        self.assertEqual(data["requeued"], {})
        self.assertNotIn(batch, brief_me._requeued_batch_ids(self._dispatches_path()))

    # -- GET /api/state requeue (F24/F33) --------------------------------
    def test_api_state_requeues_started_batch_with_no_report(self):
        """F33: GET /api/state runs F24's requeue too (previously CLI `load`
        only), so a Web user's answer eaten by a session that died without
        filing a report comes back as unconsumed."""
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        status, data = self._get("/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(data["requeued"], {"demo": ["Pick one"]})
        answers_by_question = brief_me.fold_answers(self._answers_path())
        self.assertFalse(answers_by_question[qid]["consumed"])
        self.assertIn(batch, brief_me._requeued_batch_ids(self._dispatches_path()))

    def test_api_state_does_not_requeue_batch_twice(self):
        """F33: idempotent via the same "requeued"-line dedup as CLI `load`
        - a second GET must not requeue the same batch again."""
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999999,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        _, first_data = self._get("/api/state")
        _, second_data = self._get("/api/state")

        self.assertEqual(first_data["requeued"], {"demo": ["Pick one"]})
        self.assertEqual(second_data["requeued"], {})
        with open(self._dispatches_path(), encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 3)  # started + finished + one requeued line

    # -- GET /api/state sessions (F14) ----------------------------------
    def test_state_sessions_running_finished_window_and_current(self):
        project_dir = tempfile.mkdtemp(prefix="brief-project-")
        try:
            self._write_config(projects=[{"name": "demo", "path": project_dir}])

            def ts(dt):
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            now = datetime.now(timezone.utc)
            batch_running = str(uuid.uuid4())
            batch_recent = str(uuid.uuid4())  # finished 3 days ago: kept
            batch_old = str(uuid.uuid4())     # finished 10 days ago: hidden

            records = [
                # F23/R1: a started record's tasks are objects; a legacy plain
                # string may still appear alongside them and must not 500.
                {"type": "started", "batch_id": batch_running, "project": "demo",
                 "pid": os.getpid(), "started_at": ts(now - timedelta(minutes=5)),
                 "log": "/tmp/demo.log",
                 "tasks": [{"feature": None, "kind": "question", "title": "Pick a datastore"},
                           "Legacy plain string task"]},
                # legacy started record with no "tasks" key - must not 500
                {"type": "started", "batch_id": batch_recent, "project": "demo",
                 "pid": 999001, "started_at": ts(now - timedelta(days=3, minutes=5)),
                 "log": None},
                {"type": "finished", "batch_id": batch_recent,
                 "finished_at": ts(now - timedelta(days=3)), "exit_codes": [0]},
                {"type": "started", "batch_id": batch_old, "project": "demo",
                 "pid": 999002, "started_at": ts(now - timedelta(days=10, minutes=5)),
                 "log": None, "tasks": []},
                {"type": "finished", "batch_id": batch_old,
                 "finished_at": ts(now - timedelta(days=10)), "exit_codes": [1]},
            ]
            with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")

            # current is null before .harness/current_feature exists.
            status, data = self._get("/api/state")
            self.assertEqual(status, 200)
            sessions = data["sessions"]
            self.assertEqual(len(sessions["running"]), 1)
            self.assertEqual(len(sessions["finished"]), 1)
            self.assertEqual(sessions["finished_hidden"], 1)

            run = sessions["running"][0]
            self.assertEqual(run["batch_id"], batch_running)
            self.assertEqual(run["project"], "demo")
            self.assertEqual(run["pid"], os.getpid())
            self.assertEqual(run["tasks"], [{"feature": None, "kind": "question", "title": "Pick a datastore"},
                                             "Legacy plain string task"])
            self.assertEqual(run["log"], "/tmp/demo.log")
            self.assertIsNone(run["current"])

            fin = sessions["finished"][0]
            self.assertEqual(fin["batch_id"], batch_recent)
            self.assertEqual(fin["tasks"], [])  # legacy record, no tasks key
            self.assertEqual(fin["exit_code"], 0)
            self.assertIsInstance(fin["duration_seconds"], int)
            self.assertGreater(fin["duration_seconds"], 0)

            # current reflects an existing .harness/current_feature file.
            harness_dir = os.path.join(project_dir, ".harness")
            os.makedirs(harness_dir, exist_ok=True)
            with open(os.path.join(harness_dir, "current_feature"), "w", encoding="utf-8") as f:
                f.write("F14\n")

            _, data = self._get("/api/state")
            current = data["sessions"]["running"][0]["current"]
            self.assertEqual(current["feature"], "F14")
            self.assertIn("mtime", current)
        finally:
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_state_sessions_ended_by_report(self):
        """F15: a live pid is ended by a report filed at/after started_at;
        a report filed before started_at does not count."""
        self._write_config(projects=[{"name": "demo", "path": self.home}])

        def ts(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        batch = str(uuid.uuid4())
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "started", "batch_id": batch, "project": "demo",
                                "pid": os.getpid(), "started_at": ts(started_at),
                                "log": None, "tasks": []}) + "\n")

        def report(created):
            return json.dumps({"type": "report", "id": str(uuid.uuid4()), "project": "demo",
                               "summary": "x", "severity": "normal", "created_at": ts(created)})

        # report older than started_at -> still running
        with open(self._inbox_path(), "w", encoding="utf-8") as f:
            f.write(report(started_at - timedelta(minutes=1)) + "\n")
        _, data = self._get("/api/state")
        self.assertEqual(len(data["sessions"]["running"]), 1)
        self.assertIn("demo", data["running"])

        # report at/after started_at -> finished, ended_by report
        with open(self._inbox_path(), "a", encoding="utf-8") as f:
            f.write(report(started_at + timedelta(minutes=3)) + "\n")
        _, data = self._get("/api/state")
        sessions = data["sessions"]
        self.assertEqual(sessions["running"], [])
        self.assertNotIn("demo", data["running"])
        fin = sessions["finished"][0]
        self.assertEqual(fin["ended_by"], "report")
        self.assertIsNone(fin["exit_code"])
        self.assertEqual(fin["finished_at"], ts(started_at + timedelta(minutes=3)))
        self.assertEqual(fin["duration_seconds"], 180)

    # -- F34: pid: null (orca-window) started records --------------------
    def test_state_pid_null_started_record_shows_running_and_not_requeued(self):
        """F34: an orca-window `started` record (pid: null, terminal:
        "term_...") must not raise in any pid-liveness check - it shows up
        in both `running` and `sessions.running` (pid: null there too),
        and F24's requeue must not touch its batch while it has no
        `finished` line and no report (still counts as alive)."""
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": None,
             "terminal": "term_abc123", "started_at": self._ts(now - timedelta(minutes=1)),
             "log": None, "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
        ])

        status, data = self._get("/api/state")

        self.assertEqual(status, 200)
        self.assertIn("demo", data["running"])
        run = data["sessions"]["running"][0]
        self.assertIsNone(run["pid"])
        self.assertEqual(run["batch_id"], batch)
        self.assertEqual(data["requeued"], {})  # still running -> not requeued
        answers_by_question = brief_me.fold_answers(self._answers_path())
        self.assertTrue(answers_by_question[qid]["consumed"])

    def test_state_pid_null_started_record_ends_via_report(self):
        """F34/F15: an orca-window session with no `finished` line still
        ends once a report is filed at/after its started_at, same as a
        real-pid session; the resulting finished row's pid is null."""
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": None,
             "terminal": "term_xyz", "started_at": self._ts(started_at), "log": None, "tasks": []},
        ])

        _, data = self._get("/api/state")
        self.assertEqual(len(data["sessions"]["running"]), 1)
        self.assertIn("demo", data["running"])

        self._add_report(project="demo", summary="x")
        _, data = self._get("/api/state")
        self.assertEqual(data["sessions"]["running"], [])
        self.assertNotIn("demo", data["running"])
        fin = data["sessions"]["finished"][0]
        self.assertEqual(fin["ended_by"], "report")
        self.assertIsNone(fin["pid"])

    def test_requeue_with_pid_null_and_finished_line_still_requeues(self):
        """F24 condition (b) is satisfied by a `finished` line regardless
        of pid; an orca-window batch's finished-based requeue must not
        regress just because its started record carries pid: null."""
        qid = self._add_question(project="demo", title="Pick one", choices=["A"])
        self._consumed_answer(qid)
        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": None,
             "terminal": "term_abc", "started_at": self._ts(now - timedelta(minutes=10)),
             "log": None, "tasks": [{"feature": None, "kind": "question", "title": "Pick one"}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        data = brief_me._cmd_load([])

        self.assertEqual(data["requeued"], {"demo": ["Pick one"]})

    # -- F19: report_view() parts + feature_chip ------------------------
    def test_report_view_three_part_summary_splits_into_parts_and_chip(self):
        rid = self._add_report(
            summary="F11 F12 F13 F14 F15 passing；stuck on OAuth client ID；see handoff.md"
        )
        _, state = self._get("/api/state")
        report = state["unread_reports"]["demo"][0]
        self.assertEqual(report["id"], rid)
        self.assertEqual(report["parts"], {
            "done": "F11 F12 F13 F14 F15 passing",
            "blocked": "stuck on OAuth client ID",
            "handoff": "see handoff.md",
        })
        self.assertEqual(report["feature_chip"], "F11-F15 passing")

    def test_report_view_no_delimiter_gives_null_parts_and_chip(self):
        rid = self._add_report(summary="Just a plain status update with no structure.")
        _, state = self._get("/api/state")
        report = state["unread_reports"]["demo"][0]
        self.assertEqual(report["id"], rid)
        self.assertEqual(report["parts"], {"done": None, "blocked": None, "handoff": None})
        self.assertIsNone(report["feature_chip"])

    # -- GET /api/state sessions outcome/report_id (F20) -----------------
    def test_state_sessions_outcome_exit_codes(self):
        """F20: exit_code -> outcome mapping (killed/ctrl_c/ok/exit), and
        finished_counts tallies report + killed (killed folds in ctrl_c)."""
        self._write_config(projects=[
            {"name": "killed-proj", "path": self.home},
            {"name": "ctrlc-proj", "path": self.home},
            {"name": "ok-proj", "path": self.home},
            {"name": "exit-proj", "path": self.home},
        ])

        def ts(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        finished_at = now - timedelta(minutes=1)
        batch = str(uuid.uuid4())
        projects = ["killed-proj", "ctrlc-proj", "ok-proj", "exit-proj"]
        exit_codes = [4294967295, 3221225786, 0, 2]
        records = [{"type": "started", "batch_id": batch, "project": p,
                    "pid": 900000 + i, "started_at": ts(started_at), "log": None, "tasks": []}
                   for i, p in enumerate(projects)]
        records.append({"type": "finished", "batch_id": batch, "finished_at": ts(finished_at),
                        "exit_codes": exit_codes})
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        _, data = self._get("/api/state")
        finished = {row["project"]: row for row in data["sessions"]["finished"]}
        self.assertEqual(finished["killed-proj"]["outcome"], "killed")
        self.assertEqual(finished["ctrlc-proj"]["outcome"], "ctrl_c")
        self.assertEqual(finished["ok-proj"]["outcome"], "ok")
        self.assertEqual(finished["exit-proj"]["outcome"], "exit")
        for row in finished.values():
            self.assertIsNone(row["report_id"])
        self.assertEqual(data["sessions"]["finished_counts"], {"report": 0, "killed": 2, "unknown": 0})

    def test_state_sessions_outcome_unknown_for_null_exit_code(self):
        """F33: an exit_codes entry that is JSON null (F32: the waiter could
        not observe the death, e.g. the agent-brief service itself was
        stopped mid-session) maps to outcome "unknown", not "ok" - and
        finished_counts tallies it separately from report/killed."""
        self._write_config(projects=[
            {"name": "unknown-proj", "path": self.home},
            {"name": "ok-proj", "path": self.home},
        ])

        def ts(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        finished_at = now - timedelta(minutes=1)
        batch = str(uuid.uuid4())
        records = [
            {"type": "started", "batch_id": batch, "project": "unknown-proj",
             "pid": 900200, "started_at": ts(started_at), "log": None, "tasks": []},
            {"type": "started", "batch_id": batch, "project": "ok-proj",
             "pid": 900201, "started_at": ts(started_at), "log": None, "tasks": []},
            {"type": "finished", "batch_id": batch, "finished_at": ts(finished_at),
             "exit_codes": [None, 0]},
        ]
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        _, data = self._get("/api/state")
        finished = {row["project"]: row for row in data["sessions"]["finished"]}
        self.assertEqual(finished["unknown-proj"]["outcome"], "unknown")
        self.assertIsNone(finished["unknown-proj"]["exit_code"])
        self.assertEqual(finished["ok-proj"]["outcome"], "ok")
        self.assertEqual(data["sessions"]["finished_counts"], {"report": 0, "killed": 0, "unknown": 1})

    def test_state_sessions_report_id_links_nearest_report_at_or_after_finish(self):
        """F20: report_id picks the earliest report at/after finished_at (an
        earlier report for the same project must not leak in), carries the
        report's "blocked" segment as report_note, and outcome stays as
        computed from exit_code even when a report_id is also present
        (Ctrl-C + a later report is still ctrl_c, not overridden to
        "report")."""
        self._write_config(projects=[{"name": "demo", "path": self.home}])

        def ts(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        finished_at = now - timedelta(minutes=5)
        batch = str(uuid.uuid4())
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "started", "batch_id": batch, "project": "demo",
                                "pid": 900010, "started_at": ts(started_at), "log": None, "tasks": []}) + "\n")
            f.write(json.dumps({"type": "finished", "batch_id": batch, "finished_at": ts(finished_at),
                                "exit_codes": [3221225786]}) + "\n")

        def report(rid, created, summary):
            return json.dumps({"type": "report", "id": rid, "project": "demo",
                               "summary": summary, "severity": "normal", "created_at": ts(created)})

        earlier_id, later_id = str(uuid.uuid4()), str(uuid.uuid4())
        with open(self._inbox_path(), "w", encoding="utf-8") as f:
            f.write(report(earlier_id, finished_at - timedelta(minutes=1), "should not be picked") + "\n")
            f.write(report(later_id, finished_at + timedelta(minutes=2),
                           "F1 passing；waiting on review；handoff.md") + "\n")

        _, data = self._get("/api/state")
        fin = data["sessions"]["finished"][0]
        self.assertEqual(fin["outcome"], "ctrl_c")
        self.assertEqual(fin["report_id"], later_id)
        self.assertEqual(fin["report_note"], "waiting on review")
        self.assertEqual(data["sessions"]["finished_counts"], {"report": 0, "killed": 1, "unknown": 0})

    # -- GET /api/state sessions tasks_qa (F31) --------------------------
    def test_state_sessions_tasks_qa_matched_and_unmatched(self):
        """F31: sessions_view() pairs each task with the question that
        produced it, by (project, title) - the same key structure
        _requeue_candidates() uses. A matched task's tasks_qa entry carries
        question_body + the last answer's raw fields; an unmatched task's
        entry carries only no_matching_question, for both running and
        finished rows."""
        title = "Pick a datastore"
        qid = self._add_question(project="demo", title=title, body="SQLite or JSONL?", choices=["SQLite", "JSONL"])
        status, _ = self._post("/api/answer", {"answers": [{"id": qid, "chosen": "JSONL"}]})
        self.assertEqual(status, 200)

        now = datetime.now(timezone.utc)
        batch_running = str(uuid.uuid4())
        batch_finished = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch_running, "project": "demo", "pid": os.getpid(),
             "started_at": self._ts(now - timedelta(minutes=1)), "log": None,
             "tasks": [{"feature": None, "kind": "dispatch", "title": title},
                       {"feature": None, "kind": "dispatch", "title": "No such question"}]},
            {"type": "started", "batch_id": batch_finished, "project": "demo", "pid": 999003,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "dispatch", "title": title}]},
            {"type": "finished", "batch_id": batch_finished,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        _, data = self._get("/api/state")
        sessions = data["sessions"]

        run = sessions["running"][0]
        self.assertEqual(len(run["tasks_qa"]), 2)
        matched, unmatched = run["tasks_qa"]
        self.assertEqual(matched["question_body"], "SQLite or JSONL?")
        self.assertEqual(matched["answer_chosen"], "JSONL")
        self.assertNotIn("answer_free_text", matched)
        self.assertNotIn("no_matching_question", matched)
        self.assertEqual(unmatched, {"no_matching_question": True})
        self.assertNotIn("question_body", unmatched)
        self.assertNotIn("answer_chosen", unmatched)

        fin = sessions["finished"][0]
        self.assertEqual(len(fin["tasks_qa"]), 1)
        self.assertEqual(fin["tasks_qa"][0]["question_body"], "SQLite or JSONL?")
        self.assertEqual(fin["tasks_qa"][0]["answer_chosen"], "JSONL")

    def test_state_sessions_tasks_qa_shows_answer_regardless_of_consumed_flag(self):
        """F31: unlike _requeue_candidates()'s `resolved` dict, tasks_qa's
        match is not gated on the answer's current `consumed` flag - a
        finished session's task still shows its original question/answer
        after F24's requeue later flips that answer's `consumed` back to
        false."""
        title = "Pick one"
        qid = self._add_question(project="demo", title=title, body="body text", choices=["A"])
        self._consumed_answer(qid, chosen="A")  # answered, consumed True
        brief_me.atomic_append_line(self._answers_path(), {  # F24 requeue: consumed flips back
            "question_id": qid, "answered_at": brief_me._now(), "consumed": False, "chosen": "A"})

        now = datetime.now(timezone.utc)
        batch = str(uuid.uuid4())
        self._write_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": 999005,
             "started_at": self._ts(now - timedelta(minutes=10)), "log": None,
             "tasks": [{"feature": None, "kind": "dispatch", "title": title}]},
            {"type": "finished", "batch_id": batch,
             "finished_at": self._ts(now - timedelta(minutes=5)), "exit_codes": [0]},
        ])

        _, data = self._get("/api/state")
        qa = data["sessions"]["finished"][0]["tasks_qa"][0]
        self.assertEqual(qa["question_body"], "body text")
        self.assertEqual(qa["answer_chosen"], "A")

    # -- F29: refilling questions move out of pending_questions -------------
    def _write_refill_dispatches(self, records):
        with open(os.path.join(self.home, "dispatches.jsonl"), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def test_state_moves_question_with_running_refill_to_refilling_questions(self):
        title = "Pick a datastore"
        qid = self._add_question(title=title)
        batch = str(uuid.uuid4())
        self._write_refill_dispatches([{
            "type": "started", "batch_id": batch, "project": "demo", "pid": os.getpid(),
            "started_at": brief_me._now(), "log": None,
            "tasks": [{"feature": None, "kind": "refill", "title": title}],
        }])

        _, data = self._get("/api/state")

        self.assertNotIn("demo", data.get("pending_questions", {}))
        refilling = data["refilling_questions"]["demo"]
        self.assertEqual(len(refilling), 1)
        self.assertEqual(refilling[0]["id"], qid)
        self.assertEqual(refilling[0]["title"], title)
        self.assertIsInstance(refilling[0]["refill_elapsed_seconds"], int)

    def test_state_dead_pid_refill_returns_question_to_pending(self):
        title = "Pick a datastore"
        qid = self._add_question(title=title)
        batch = str(uuid.uuid4())
        self._write_refill_dispatches([{
            "type": "started", "batch_id": batch, "project": "demo", "pid": 999999999,
            "started_at": brief_me._now(), "log": None,
            "tasks": [{"feature": None, "kind": "refill", "title": title}],
        }])

        _, data = self._get("/api/state")

        self.assertEqual(data.get("refilling_questions", {}), {})
        self.assertEqual(data["pending_questions"]["demo"][0]["id"], qid)

    def test_state_finished_refill_batch_returns_question_to_pending(self):
        title = "Pick a datastore"
        qid = self._add_question(title=title)
        batch = str(uuid.uuid4())
        self._write_refill_dispatches([
            {"type": "started", "batch_id": batch, "project": "demo", "pid": os.getpid(),
             "started_at": brief_me._now(), "log": None,
             "tasks": [{"feature": None, "kind": "refill", "title": title}]},
            {"type": "finished", "batch_id": batch, "finished_at": brief_me._now(), "exit_codes": [0]},
        ])

        _, data = self._get("/api/state")

        self.assertEqual(data.get("refilling_questions", {}), {})
        self.assertEqual(data["pending_questions"]["demo"][0]["id"], qid)

    # -- F30: Settings GET/POST /api/config ----------------------------------
    def test_get_config_returns_full_config_including_readonly_fields(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}], collector=["echo", "hi"])
        status, data = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(data["projects"], [{"name": "demo", "path": self.home}])
        self.assertEqual(data["collector"], ["echo", "hi"])

    def test_post_config_whitelisted_roundtrip(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        status, data = self._post("/api/config", {"dispatch": {
            "watch": True, "model": "opus", "context_model": "haiku",
            "context_language": "Traditional Chinese (keep technical terms in English)",
            "plain_language": "all", "delegate": True,
        }})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        _, config = self._get("/api/config")
        self.assertEqual(config["dispatch"]["watch"], True)
        self.assertEqual(config["dispatch"]["model"], "opus")
        self.assertEqual(config["dispatch"]["context_model"], "haiku")
        self.assertEqual(config["dispatch"]["context_language"],
                         "Traditional Chinese (keep technical terms in English)")
        self.assertEqual(config["dispatch"]["plain_language"], "all")
        self.assertEqual(config["dispatch"]["delegate"], True)

    def test_post_config_rejects_unknown_key_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            before = f.read()

        status, data = self._post("/api/config", {"dispatch": {"permission_mode": "bypassPermissions"}})

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_post_config_rejects_wrong_type_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            before = f.read()

        status, data = self._post("/api/config", {"dispatch": {"watch": "yes"}})

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_post_config_rejects_invalid_plain_language_value_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            before = f.read()

        status, data = self._post("/api/config", {"dispatch": {"plain_language": "loudly"}})

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_post_config_preserves_unknown_existing_keys(self):
        config = {"projects": [{"name": "demo", "path": self.home}], "collector": None,
                  "notify": None, "dispatch": {"permission_mode": "auto", "allowed_tools": "Bash,Read",
                                               "future_key": "keep-me"},
                  "another_future_top_level_key": 42}
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f)

        status, data = self._post("/api/config", {"dispatch": {"watch": True}})
        self.assertEqual(status, 200)

        _, current = self._get("/api/config")
        self.assertEqual(current["another_future_top_level_key"], 42)
        self.assertEqual(current["dispatch"]["permission_mode"], "auto")
        self.assertEqual(current["dispatch"]["allowed_tools"], "Bash,Read")
        self.assertEqual(current["dispatch"]["future_key"], "keep-me")
        self.assertEqual(current["dispatch"]["watch"], True)

    def test_post_config_then_dispatch_reads_new_value(self):
        """F30 acceptance #3: next dispatch (including refill) uses the new
        value with no serve restart - dispatch.py always reloads config.json
        from disk, so this only needs to prove the write landed."""
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._post("/api/config", {"dispatch": {"context_model": "haiku"}})

        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
        import dispatch  # noqa: E402
        reloaded = dispatch.load_config(self.home)
        self.assertEqual(dispatch.context_model_for(reloaded), "haiku")


# -- F36: GET /api/state's `orca` field + POST /api/config's window/projects
class BriefMeOrcaAPITestCase(BriefMeAPITestCase):
    """Reuses test_dispatch_batch.py's real subprocess stand-in for the orca
    CLI (_write_fake_orca/FAKE_ORCA_PY, reached via PATH, not a python-level
    mock of dispatch.run_orca) rather than a second copy of it."""

    def setUp(self):
        super().setUp()
        self.orca_dir = tempfile.mkdtemp(prefix="fake-orca-")
        self.orca_log_path = os.path.join(self.orca_dir, "orca-calls.jsonl")
        self.orca_config_path = os.path.join(self.orca_dir, "orca-config.json")
        test_dispatch_batch._write_fake_orca(self.orca_dir)
        os.environ["FAKE_ORCA_LOG"] = self.orca_log_path
        os.environ["FAKE_ORCA_CONFIG"] = self.orca_config_path
        os.environ["PATH"] = self.orca_dir + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.orca_dir, ignore_errors=True)
        super().tearDown()

    def _set_orca_config(self, cfg):
        with open(self.orca_config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def _orca_log_entries(self):
        if not os.path.exists(self.orca_log_path):
            return []
        with open(self.orca_log_path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def _config_raw(self):
        with open(os.path.join(self.home, "config.json"), encoding="utf-8") as f:
            return f.read()

    # -- /api/state's orca: three states -----------------------------------
    def test_orca_state_unavailable_when_cli_not_on_path(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        empty_path_dir = tempfile.mkdtemp(prefix="empty-path-")
        self.addCleanup(shutil.rmtree, empty_path_dir, ignore_errors=True)
        os.environ["PATH"] = empty_path_dir  # no orca anywhere on this PATH

        _, data = self._get("/api/state")

        orca = data["orca"]
        self.assertEqual(orca, {"available": False, "running": False, "version": None,
                                "repos": [], "project_ancestors": {"demo": []}})
        self.assertEqual(self._orca_log_entries(), [])  # never even invoked

    def test_orca_state_available_but_not_running(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._set_orca_config({"status": {"ok": False, "error": {"code": "not_running"}}})

        _, data = self._get("/api/state")

        orca = data["orca"]
        self.assertTrue(orca["available"])
        self.assertFalse(orca["running"])
        self.assertIsNone(orca["version"])
        self.assertEqual(orca["repos"], [])
        # status-not-ok short-circuits before the repo list call (F36 acceptance).
        self.assertEqual([e[:1] for e in self._orca_log_entries()], [["status"]])

    def test_orca_state_running_with_version_and_repos(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._set_orca_config({
            "status": {"ok": True, "result": {"app": {"running": True}, "runtime": {"appVersion": "1.4.2"}}},
            "repo_list": {"ok": True, "result": {"repos": [
                {"id": "repo-1", "displayName": "Repo One", "path": self.home},
            ]}},
        })

        _, data = self._get("/api/state")

        orca = data["orca"]
        self.assertTrue(orca["available"])
        self.assertTrue(orca["running"])
        self.assertEqual(orca["version"], "1.4.2")
        self.assertEqual(orca["repos"], [{"id": "repo-1", "displayName": "Repo One", "path": self.home}])

    def test_orca_state_project_ancestors_uses_is_ancestor_workspace(self):
        """project_ancestors (F36's own field, not part of the literal `orca`
        shape in the acceptance) is derived via dispatch.is_ancestor_workspace:
        a strict ancestor directory is included, the project's own path and
        an unrelated path are not."""
        nested = os.path.join(self.home, "nested", "child")
        os.makedirs(nested, exist_ok=True)
        unrelated = tempfile.mkdtemp(prefix="unrelated-")
        self.addCleanup(shutil.rmtree, unrelated, ignore_errors=True)
        self._write_config(projects=[{"name": "demo", "path": nested}])
        self._set_orca_config({"repo_list": {"ok": True, "result": {"repos": [
            {"id": "ancestor-repo", "displayName": "Ancestor", "path": self.home},
            {"id": "self-repo", "displayName": "Self", "path": nested},
            {"id": "unrelated-repo", "displayName": "Unrelated", "path": unrelated},
        ]}}})

        _, data = self._get("/api/state")

        self.assertEqual(data["orca"]["project_ancestors"]["demo"], ["ancestor-repo"])

    # -- POST /api/config: window + projects[].orca legal roundtrip --------
    def test_post_config_window_and_projects_orca_roundtrip(self):
        self._write_config(projects=[
            {"name": "demo", "path": self.home},
            {"name": "other", "path": self.orca_dir},
        ])
        self._set_orca_config({"repo_list": {"ok": True, "result": {"repos": [
            {"id": "repo-1", "displayName": "Repo One", "path": self.home},
        ]}}})

        status, data = self._post("/api/config", {
            "dispatch": {"window": "orca"},
            "projects": [
                {"name": "demo", "orca": {"mode": "bind", "repo_id": "repo-1"}},
                {"name": "other", "orca": {"mode": "repo"}},
            ],
        })

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["config"]["dispatch"]["window"], "orca")
        projects = {p["name"]: p for p in data["config"]["projects"]}
        self.assertEqual(projects["demo"]["orca"], {"mode": "bind", "repo_id": "repo-1"})
        self.assertEqual(projects["other"]["orca"], {"mode": "repo"})
        self.assertEqual(data["warnings"], [])  # "other" registers fine, "demo" is bind-mode (skipped)

        adds = [e for e in self._orca_log_entries() if e[:2] == ["repo", "add"]]
        self.assertEqual(len(adds), 1)
        self.assertEqual(adds[0][adds[0].index("--path") + 1], self.orca_dir)

    # -- POST /api/config #4 (a)-(g): each -> 400, config.json untouched ----
    def test_post_config_projects_not_array_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": {"name": "demo"}})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_entry_unknown_field_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [
            {"name": "demo", "orca": {"mode": "repo"}, "extra": 1}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_unknown_name_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [
            {"name": "does-not-exist", "orca": {"mode": "repo"}}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_orca_not_object_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [{"name": "demo", "orca": "repo"}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_bad_mode_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [
            {"name": "demo", "orca": {"mode": "weird"}}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_bind_missing_repo_id_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [
            {"name": "demo", "orca": {"mode": "bind"}}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_projects_bind_repo_id_not_in_orca_repo_list_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._set_orca_config({"repo_list": {"ok": True, "result": {"repos": []}}})
        before = self._config_raw()
        status, data = self._post("/api/config", {"projects": [
            {"name": "demo", "orca": {"mode": "bind", "repo_id": "ghost"}}]})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    def test_post_config_rejects_invalid_window_value_400_no_write(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        before = self._config_raw()
        status, data = self._post("/api/config", {"dispatch": {"window": "popup"}})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(self._config_raw(), before)

    # -- POST /api/config: orca repo add warnings ---------------------------
    def test_post_config_orca_not_running_skips_repo_add_and_warns(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._set_orca_config({"status": {"ok": False, "error": {"code": "not_running"}}})

        status, data = self._post("/api/config", {"dispatch": {"window": "orca"}})

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("not running", data["warnings"][0])
        self.assertEqual(data["config"]["dispatch"]["window"], "orca")  # save still happened

    def test_post_config_repo_add_failure_warns_but_save_succeeds(self):
        self._write_config(projects=[{"name": "demo", "path": self.home}])
        self._set_orca_config({"repo_add": {"ok": False, "error": {"code": "boom"}}})

        status, data = self._post("/api/config", {"dispatch": {"window": "orca"}})

        self.assertEqual(status, 200)
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("demo", data["warnings"][0])
        self.assertEqual(data["config"]["dispatch"]["window"], "orca")


if __name__ == "__main__":
    unittest.main(verbosity=2)
