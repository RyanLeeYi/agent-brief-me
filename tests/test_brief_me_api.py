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

    def _write_config(self, projects=None, collector=None):
        config = {"projects": projects or []}
        if collector is not None:
            config["collector"] = collector
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
        self.assertEqual(data["sessions"]["finished_counts"], {"report": 0, "killed": 2})

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
        self.assertEqual(data["sessions"]["finished_counts"], {"report": 0, "killed": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
