"""Tests for examples/feature-list-collector.py: the "Collector idempotency"
dedup rule in docs/schema.md, rationale-in-body, and legacy/superseded/closed
skipping.

Runs standalone: `python tests/test_example_collector.py`. stdlib unittest
only. BRIEF_HOME is pointed at a fresh temp directory per test; the real
~/.agent-brief is never touched.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid

_COLLECTOR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "examples", "feature-list-collector.py"
)
_spec = importlib.util.spec_from_file_location("feature_list_collector", _COLLECTOR_PATH)
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _question(title, qid, project="demo"):
    return {
        "type": "question",
        "id": qid,
        "project": project,
        "title": title,
        "body": "",
        "severity": "normal",
        "created_at": "2026-08-20T00:00:00Z",
    }


def _status(ref, status, at="2026-08-20T01:00:00Z"):
    return {"type": "status", "ref": ref, "status": status, "at": at}


def _answer(qid, consumed, answered_at="2026-08-20T02:00:00Z"):
    return {"question_id": qid, "chosen": "x", "answered_at": answered_at, "consumed": consumed}


class BlockedTitlesTestCase(unittest.TestCase):
    """docs/schema.md "Collector idempotency": which titles get re-filed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collector-test-")
        self.inbox = os.path.join(self.tmp, "inbox.jsonl")
        self.answers = os.path.join(self.tmp, "answers.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _blocked(self):
        return collector.blocked_titles(self.inbox, self.answers)

    def test_pending_is_blocked(self):
        qid = str(uuid.uuid4())
        _write_jsonl(self.inbox, [_question("t1", qid)])
        self.assertIn("t1", self._blocked())

    def test_answered_unconsumed_is_blocked(self):
        qid = str(uuid.uuid4())
        _write_jsonl(self.inbox, [_question("t1", qid), _status(qid, "answered")])
        _write_jsonl(self.answers, [_answer(qid, consumed=False)])
        self.assertIn("t1", self._blocked())

    def test_answered_consumed_is_not_blocked(self):
        qid = str(uuid.uuid4())
        _write_jsonl(self.inbox, [_question("t1", qid), _status(qid, "answered")])
        _write_jsonl(self.answers, [_answer(qid, consumed=True)])
        self.assertNotIn("t1", self._blocked())

    def test_cancelled_is_not_blocked(self):
        qid = str(uuid.uuid4())
        _write_jsonl(self.inbox, [_question("t1", qid), _status(qid, "cancelled")])
        self.assertNotIn("t1", self._blocked())

    def test_reopened_folds_back_to_pending(self):
        qid = str(uuid.uuid4())
        _write_jsonl(self.inbox, [
            _question("t1", qid),
            _status(qid, "cancelled", at="2026-08-20T01:00:00Z"),
            _status(qid, "reopened", at="2026-08-20T02:00:00Z"),
        ])
        self.assertIn("t1", self._blocked())


class ScanRepoTestCase(unittest.TestCase):
    """scan_repo(): rationale-in-body, and skipping closed/superseded/legacy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collector-repo-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_feature_list(self, features):
        path = os.path.join(self.tmp, "feature_list.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"features": features}, f)

    def test_rationale_prepended_to_body(self):
        self._write_feature_list([
            {"id": "F1", "title": "Do X", "status": "failing", "signed_off": False,
             "acceptance": ["a", "b"], "rationale": "Because Y."},
        ])
        questions, _ = collector.scan_repo("demo", self.tmp)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["body"], "Because Y.\n\n- a\n- b")

    def test_missing_rationale_body_unchanged(self):
        self._write_feature_list([
            {"id": "F1", "title": "Do X", "status": "failing", "signed_off": False,
             "acceptance": ["a", "b"]},
        ])
        questions, _ = collector.scan_repo("demo", self.tmp)
        self.assertEqual(questions[0]["body"], "- a\n- b")

    def test_empty_or_non_string_rationale_body_unchanged(self):
        self._write_feature_list([
            {"id": "F1", "title": "Do X", "status": "failing", "signed_off": False,
             "acceptance": ["a"], "rationale": "   "},
            {"id": "F2", "title": "Do Y", "status": "failing", "signed_off": False,
             "acceptance": ["a"], "rationale": 123},
        ])
        questions, _ = collector.scan_repo("demo", self.tmp)
        for q in questions:
            self.assertEqual(q["body"], "- a")

    def test_closed_and_superseded_and_passing_are_skipped(self):
        self._write_feature_list([
            {"id": "F1", "title": "closed one", "status": "closed", "signed_off": True},
            {"id": "F2", "title": "superseded one", "status": "failing", "signed_off": True,
             "superseded_by": "F9"},
            {"id": "F3", "title": "passing one", "status": "passing", "signed_off": True},
        ])
        questions, signed_failing = collector.scan_repo("demo", self.tmp)
        self.assertEqual(questions, [])
        self.assertEqual(signed_failing, [])

    def test_legacy_entry_ignored(self):
        self._write_feature_list([
            {"id": "F1", "title": "legacy", "status": "failing", "acceptance": ["a"]},
        ])
        questions, signed_failing = collector.scan_repo("demo", self.tmp)
        self.assertEqual(questions, [])
        self.assertEqual(signed_failing, [])


class MainEndToEndTestCase(unittest.TestCase):
    """main(): a title only gets re-filed when blocked_titles() allows it."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="brief-home-")
        self.repo = tempfile.mkdtemp(prefix="repo-")
        self._orig_env = dict(os.environ)
        self._orig_argv = sys.argv[:]
        os.environ["BRIEF_HOME"] = self.home
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"name": "demo", "path": self.repo}]}, f)
        sys.argv = ["feature-list-collector.py"]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        sys.argv[:] = self._orig_argv
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def _write_feature_list(self, features):
        with open(os.path.join(self.repo, "feature_list.json"), "w", encoding="utf-8") as f:
            json.dump({"features": features}, f)

    def _inbox_lines(self):
        path = os.path.join(self.home, "inbox.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def test_does_not_refile_while_pending(self):
        self._write_feature_list([
            {"id": "F1", "title": "Do X", "status": "failing", "signed_off": False,
             "acceptance": ["a"]},
        ])
        collector.main()
        self.assertEqual(len(self._inbox_lines()), 1)

        collector.main()
        self.assertEqual(
            len(self._inbox_lines()), 1, "re-running while pending must not duplicate"
        )


if __name__ == "__main__":
    unittest.main()
