"""F12: notify runs once per submission with <project> <type> <severity> <text>.
Run: python tests/test_brief_submit_notify.py  (stdlib only)"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(ROOT, "skills", "brief-submit", "SKILL.md")


def load_impl():
    text = open(SKILL, encoding="utf-8").read()
    m = re.search(r"## Reference implementation.*?```python\n(.*?)```", text, re.S)
    assert m, "python fence not found under Reference implementation"
    ns = {}
    exec(m.group(1), ns)
    return ns


FAKE_NOTIFY = "import sys,json;open(sys.argv[1],'a',encoding='utf-8').write(json.dumps(sys.argv[2:])+chr(10))"


def run(notify_argv, submits):
    impl = load_impl()
    base = tempfile.mkdtemp()
    with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [], "collector": None, "notify": notify_argv}, f)
    results = [fn(impl, payload, base) for fn, payload in submits]
    return base, results


def q(impl, payload, base):
    return impl["submit_question"](payload, base)


def r(impl, payload, base):
    return impl["submit_report"](payload, base)


def main():
    out = os.path.join(tempfile.mkdtemp(), "calls.jsonl")
    base, results = run([sys.executable, "-c", FAKE_NOTIFY, out], [
        (q, {"project": "p", "title": "low q", "severity": "low"}),
        (q, {"project": "p", "title": "high q", "severity": "high"}),
        (q, {"project": "p", "title": "default q"}),
        (r, {"project": "p", "summary": "F12 passing\nsecond line"}),
        (r, {"project": "p", "summary": "bad", "severity": "high"}),
    ])
    assert all(x["ok"] for x in results), results
    calls = [json.loads(l) for l in open(out, encoding="utf-8")]
    assert calls == [
        ["p", "question", "low", "low q"],
        ["p", "question", "high", "high q"],
        ["p", "question", "normal", "default q"],
        ["p", "report", "none", "F12 passing"],
        ["p", "report", "high", "bad"],
    ], calls
    print("notify argv for 5 submissions - OK")

    out2 = os.path.join(tempfile.mkdtemp(), "calls.jsonl")
    base, results = run(None, [(r, {"project": "p", "summary": "s"})])
    assert results[0]["ok"] and not os.path.exists(out2)
    print("notify null -> not run - OK")

    base, results = run(["definitely-not-a-program-xyz"], [(r, {"project": "p", "summary": "s"})])
    assert results[0]["ok"] and "warning" in results[0], results
    lines = open(os.path.join(base, "inbox.jsonl"), encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    print("notify missing program -> append kept, warning - OK")
    print("OK: F12 notify tests passed.")


if __name__ == "__main__":
    main()
