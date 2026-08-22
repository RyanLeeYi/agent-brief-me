"""F16: regression tests for `ensure_trusted` in scripts/dispatch.py.

Run: python -m unittest tests.test_dispatch_trust -v  (stdlib only)

`ensure_trusted` is implemented by another worker in the same feature; at
the time this file is written it does not exist yet in this worktree, so
every test below is expected to fail with ImportError (raised from
setUp(), before any assertion runs) until that lands. The import happens
in setUp() rather than at module scope specifically so unittest can still
discover and report all four tests individually instead of collapsing
into one module-load failure.

Contract under test (frozen, see the F16 acceptance):
    ensure_trusted(claude_json_path: str, project_path: str) -> bool
- Reads claude_json_path; if projects[<key>].hasTrustDialogAccepted is not
  true, sets it true, writes the file, returns True.
- If already true, returns False and leaves the file byte-for-byte
  unchanged.
- key is project_path with forward slashes and no trailing slash.
- All other projects and top-level fields in claude.json are preserved
  verbatim.
- dispatch.py calls this only in --watch mode; missing/invalid
  claude.json or a failed write must not abort dispatch (fail-open): one
  warning line on stderr, dispatch continues.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
DISPATCH = os.path.join(SCRIPTS, "dispatch.py")

PROJECT_PATH = "C:/Users/user/OneDrive/Desktop/SideProject/lift-log"
OTHER_PROJECT_PATH = "C:/Users/user/OneDrive/Desktop/SideProject/other-repo"


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _make_immediate_exit_claude(directory):
    """A stand-in for the real `claude` binary for --watch mode: exits
    immediately, ignoring all args/stdin, so the subprocess call under
    test doesn't hang on a real console window."""
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


class EnsureTrustedTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        from dispatch import ensure_trusted  # noqa: E402 - expected ImportError until implemented
        self.ensure_trusted = ensure_trusted
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_untrusted_project_gets_written_true_and_returns_true(self):
        """Not yet trusted -> sets hasTrustDialogAccepted true, returns True."""
        path = os.path.join(self.tmpdir, "claude.json")
        _write_json(path, {"projects": {PROJECT_PATH: {"hasTrustDialogAccepted": False}}})

        result = self.ensure_trusted(path, PROJECT_PATH)

        self.assertTrue(result)
        with open(path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertTrue(written["projects"][PROJECT_PATH]["hasTrustDialogAccepted"])

    def test_already_trusted_project_left_byte_for_byte_unchanged(self):
        """Already trusted -> returns False, file bytes identical."""
        path = os.path.join(self.tmpdir, "claude.json")
        _write_json(path, {"projects": {PROJECT_PATH: {"hasTrustDialogAccepted": True}}})
        with open(path, "rb") as f:
            before = f.read()

        result = self.ensure_trusted(path, PROJECT_PATH)

        self.assertFalse(result)
        with open(path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_other_projects_and_top_level_fields_preserved(self):
        """Write must leave other projects and top-level misc fields verbatim."""
        path = os.path.join(self.tmpdir, "claude.json")
        other_entry = {"hasTrustDialogAccepted": True, "someOtherSetting": "keep-me"}
        _write_json(path, {
            "numStartups": 3,
            "projects": {
                PROJECT_PATH: {"hasTrustDialogAccepted": False},
                OTHER_PROJECT_PATH: other_entry,
            },
        })

        result = self.ensure_trusted(path, PROJECT_PATH)

        self.assertTrue(result)
        with open(path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["numStartups"], 3)
        self.assertEqual(written["projects"][OTHER_PROJECT_PATH], other_entry)
        self.assertTrue(written["projects"][PROJECT_PATH]["hasTrustDialogAccepted"])

    def test_missing_claude_json_warns_on_stderr_and_dispatch_still_exits_0(self):
        """Missing claude.json -> fail-open: dispatch.py --watch still exits
        0 with one warning line on stderr; the batch is not aborted."""
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        project_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project_dir, ignore_errors=True)
        _write_json(os.path.join(home, "config.json"),
                    {"projects": [{"name": "a", "path": project_dir}], "collector": None, "notify": None})

        fake_claude = _make_immediate_exit_claude(home)
        missing_claude_json = os.path.join(home, "does-not-exist-claude.json")
        env = {**os.environ, "BRIEF_HOME": home, "BRIEF_CLAUDE_CMD": fake_claude,
               "BRIEF_CLAUDE_JSON": missing_claude_json}

        out = subprocess.run([sys.executable, DISPATCH, "--watch", "a"],
                              env=env, capture_output=True, text=True, timeout=30)

        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("warning", out.stderr.lower(), out.stdout + out.stderr)


class WatchOverrideTests(unittest.TestCase):
    """F17: CLI --watch / --no-watch overrides config dispatch.watch."""

    def _run(self, config_watch, flags):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        project_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project_dir, ignore_errors=True)
        _write_json(os.path.join(home, "config.json"),
                    {"projects": [{"name": "a", "path": project_dir}], "collector": None, "notify": None,
                     "dispatch": {"watch": config_watch}})
        claude_json = os.path.join(home, "claude.json")
        _write_json(claude_json, {"projects": {}})
        env = {**os.environ, "BRIEF_HOME": home, "BRIEF_CLAUDE_CMD": _make_immediate_exit_claude(home),
               "BRIEF_CLAUDE_JSON": claude_json}
        out = subprocess.run([sys.executable, DISPATCH, *flags, "a"],
                             env=env, capture_output=True, text=True, timeout=30)
        dispatches = os.path.join(home, "dispatches.jsonl")
        started = []
        if os.path.exists(dispatches):
            with open(dispatches, encoding="utf-8") as f:
                started = [r for r in map(json.loads, f) if r.get("type") == "started"]
        with open(claude_json, encoding="utf-8") as f:
            trusted = json.load(f)["projects"]
        return out, started, trusted

    def test_no_watch_flag_overrides_config_true(self):
        out, started, trusted = self._run(True, ["--no-watch"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNotNone(started[0]["log"])
        self.assertEqual(trusted, {})

    def test_watch_flag_overrides_config_false(self):
        out, started, trusted = self._run(False, ["--watch"])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIsNone(started[0]["log"])

    def test_both_flags_is_usage_error(self):
        out, started, _ = self._run(True, ["--watch", "--no-watch"])
        self.assertEqual(out.returncode, 2)
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
