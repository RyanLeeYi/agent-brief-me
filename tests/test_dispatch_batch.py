"""F13: dispatch.py records started/finished lines and notifies once per batch.
Run: python tests/test_dispatch_batch.py  (stdlib only; fake claude = sleep 1)"""
import json
import os
import subprocess
import sys
import tempfile
import time

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


if __name__ == "__main__":
    main()
