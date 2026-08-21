"""Minimal stub of the F8 API contract, used by test_brief_me_html.py when
BRIEF_API_URL is not set. Implements only feature_list.json's F8 acceptance
item 2 (the API contract) plus GET / serving scripts/brief_me.html - not the
rest of F8 (that is scripts/brief_me.py's job). Data is appended to the two
JSONL files under BRIEF_HOME, using the same append rule as brief_me.py.

Dispatch is faked (no real subprocess.run of dispatch.py): every project in
the request is reported as dispatched, with a fake log path per project when
not watching. The last dispatch call's {projects, watch} is recorded on
`last_dispatch` for assertions from the same Python process.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(REPO_ROOT, "scripts", "brief_me.html")

# Recorded here (not over HTTP) so the test process can assert on it directly.
last_dispatch = None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _brief_home():
    return os.environ.get("BRIEF_HOME", os.path.expanduser("~/.agent-brief"))


def _append(path, record):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(line)


def _iter_lines(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line:
                yield line


def _build_option_list(question):
    options = []
    seen = set()
    recommendation = question.get("recommendation")
    if recommendation:
        options.append(recommendation)
        seen.add(recommendation)
    for choice in question.get("choices", []):
        if choice not in seen:
            options.append(choice)
            seen.add(choice)
    return options


def _fold(brief_home):
    """Fold inbox.jsonl per docs/schema.md + the F8-added `reopened` status:
    for a question ref, the last of {answered, cancelled, reopened} wins;
    `reopened` puts it back to pending. Reports only ever get `read`.
    Returns (questions, reports, last_q_status, last_q_status_at, r_read).
    """
    questions, reports = {}, {}
    last_q_status, last_q_status_at = {}, {}
    r_read = set()
    for line in _iter_lines(os.path.join(brief_home, "inbox.jsonl")):
        record = json.loads(line)
        rtype = record["type"]
        if rtype == "question":
            questions[record["id"]] = record
        elif rtype == "report":
            reports[record["id"]] = record
        elif rtype == "status":
            if record["status"] == "read":
                r_read.add(record["ref"])
            else:  # answered | cancelled | reopened
                last_q_status[record["ref"]] = record["status"]
                last_q_status_at[record["ref"]] = record["at"]
    return questions, reports, last_q_status, last_q_status_at, r_read


def _fold_answers(brief_home):
    result = {}
    for line in _iter_lines(os.path.join(brief_home, "answers.jsonl")):
        record = json.loads(line)
        result[record["question_id"]] = record
    return result


def _group_by_project(records):
    groups = {}
    for r in records:
        groups.setdefault(r["project"], []).append(r)
    return {p: groups[p] for p in sorted(groups)}


def _q_view(q):
    v = {k: q.get(k) for k in ("id", "project", "title", "body", "severity", "session_id", "multi") if q.get(k) is not None}
    v["options"] = _build_option_list(q)
    v["age"] = "0s ago"
    return v


def _r_view(r):
    v = {k: r.get(k) for k in ("id", "project", "summary", "severity", "session_id") if r.get(k) is not None}
    v["age"] = "0s ago"
    return v


def _config_projects(brief_home):
    path = os.path.join(brief_home, "config.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return [p["name"] for p in config.get("projects", [])]


def build_state(brief_home):
    questions, reports, last_status, last_status_at, r_read = _fold(brief_home)
    answers_by_question = _fold_answers(brief_home)

    pending = [q for qid, q in questions.items() if last_status.get(qid) not in ("answered", "cancelled")]
    unread = [r for rid, r in reports.items() if rid not in r_read]
    cancelled = [q for qid, q in questions.items() if last_status.get(qid) == "cancelled"]

    unconsumed_projects = sorted({
        questions[qid]["project"] for qid, a in answers_by_question.items()
        if not a.get("consumed", False) and qid in questions
    })
    unconsumed_counts = {}
    for qid, a in answers_by_question.items():
        if not a.get("consumed", False) and qid in questions:
            p = questions[qid]["project"]
            unconsumed_counts[p] = unconsumed_counts.get(p, 0) + 1

    dismissed_grouped = {}
    for p, qs in _group_by_project(cancelled).items():
        dismissed_grouped[p] = []
        for q in qs:
            v = _q_view(q)
            v["dismissed_at"] = last_status_at.get(q["id"])
            dismissed_grouped[p].append(v)

    return {
        "collector_warning": None,
        "unread_reports": {p: [_r_view(r) for r in rs] for p, rs in _group_by_project(unread).items()},
        "pending_questions": {p: [_q_view(q) for q in qs] for p, qs in _group_by_project(pending).items()},
        "unconsumed_projects": unconsumed_projects,
        "projects": _config_projects(brief_home),
        "unconsumed_counts": unconsumed_counts,
        "dismissed": dismissed_grouped,
    }, questions, reports


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep test output quiet

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self):
        if self.path == "/":
            if not os.path.exists(HTML_PATH):
                self._send_json(500, {"ok": False, "error": "brief_me.html not found"})
                return
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                html = f.read()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            state, _, _ = build_state(_brief_home())
            self._send_json(200, state)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON body"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"ok": False, "error": "body must be a JSON object"})
            return

        brief_home = _brief_home()
        inbox_path = os.path.join(brief_home, "inbox.jsonl")
        answers_path = os.path.join(brief_home, "answers.jsonl")

        if self.path == "/api/collect":
            self._send_json(200, {"ok": True, "collector_warning": None})
            return

        if self.path == "/api/read":
            ids = body.get("ids")
            if not isinstance(ids, list):
                self._send_json(400, {"ok": False, "error": "ids must be a list"})
                return
            _, _, reports = self._folded(brief_home)
            for rid in ids:
                if rid not in reports:
                    self._send_json(400, {"ok": False, "error": f"unknown report id {rid}"})
                    return
            for rid in ids:
                _append(inbox_path, {"type": "status", "ref": rid, "status": "read", "at": _now()})
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/reopen":
            ids = body.get("ids")
            if not isinstance(ids, list):
                self._send_json(400, {"ok": False, "error": "ids must be a list"})
                return
            questions, _, _ = self._folded(brief_home)
            for qid in ids:
                if qid not in questions:
                    self._send_json(400, {"ok": False, "error": f"unknown question id {qid}"})
                    return
            for qid in ids:
                _append(inbox_path, {"type": "status", "ref": qid, "status": "reopened", "at": _now()})
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/answer":
            self._handle_answer(body, brief_home, inbox_path, answers_path)
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def _folded(self, brief_home):
        questions, reports, _, _, _ = _fold(brief_home)
        return questions, None, reports

    def _handle_answer(self, body, brief_home, inbox_path, answers_path):
        answers = body.get("answers", [])
        dismiss = body.get("dismiss", [])
        dispatch = body.get("dispatch")
        if not isinstance(answers, list) or not isinstance(dismiss, list):
            self._send_json(400, {"ok": False, "error": "answers/dismiss must be lists"})
            return

        questions, _, _ = self._folded(brief_home)

        answer_ids = []
        for entry in answers:
            if not isinstance(entry, dict) or "id" not in entry:
                self._send_json(400, {"ok": False, "error": "each answer needs an id"})
                return
            qid = entry["id"]
            answer_ids.append(qid)
            if qid not in questions:
                self._send_json(400, {"ok": False, "error": f"unknown question id {qid}"})
                return
            has_chosen = "chosen" in entry
            free_text = entry.get("free_text")
            has_free_text = "free_text" in entry and isinstance(free_text, str) and free_text.strip() != ""
            if not has_chosen and not has_free_text:
                self._send_json(400, {"ok": False, "error": f"answer {qid} needs chosen and/or non-empty free_text"})
                return

        for qid in dismiss:
            if qid not in questions:
                self._send_json(400, {"ok": False, "error": f"unknown question id {qid}"})
                return

        if set(answer_ids) & set(dismiss):
            self._send_json(400, {"ok": False, "error": "an id cannot be in both answers and dismiss"})
            return

        for entry in answers:
            record = {"question_id": entry["id"], "answered_at": _now(), "consumed": False}
            if "chosen" in entry:
                record["chosen"] = entry["chosen"]
            if "free_text" in entry:
                record["free_text"] = entry["free_text"]
            _append(answers_path, record)
            _append(inbox_path, {"type": "status", "ref": entry["id"], "status": "answered", "at": _now()})

        for qid in dismiss:
            _append(inbox_path, {"type": "status", "ref": qid, "status": "cancelled", "at": _now()})

        dispatched, log_paths, dispatch_error = [], [], None
        if dispatch:
            projects = dispatch.get("projects", [])
            watch = bool(dispatch.get("watch"))
            global last_dispatch
            last_dispatch = {"projects": list(projects), "watch": watch}
            for p in projects:
                dispatched.append(p)
                if not watch:
                    log_paths.append(os.path.join(brief_home, "logs", f"{p}-{uuid.uuid4().hex}.log"))

        result = {
            "ok": True,
            "saved": len(answers),
            "dismissed": len(dismiss),
            "dispatched": dispatched,
            "log_paths": log_paths,
        }
        if dispatch_error:
            result["dispatch_error"] = dispatch_error
        self._send_json(200, result)


def start_server(port=0):
    """Start the stub in a background thread bound to 127.0.0.1. Returns
    (server, base_url); call server.shutdown() to stop it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


if __name__ == "__main__":
    srv, url = start_server(8765)
    print(f"stub API serving at {url} (BRIEF_HOME={_brief_home()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
