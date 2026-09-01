"""Playwright UI test for scripts/brief_me.html (F9).

Runs standalone: `py -3.13 tests/test_brief_me_html.py`. Uses headless
chromium via Python Playwright (already installed in this environment).

Backend: if BRIEF_API_URL is set, the test drives the browser against that
server (used later to re-verify against a real `brief_me.py serve`) and
expects BRIEF_HOME to already be set to that server's inbox directory too -
this script writes its fixture data straight into BRIEF_HOME's inbox.jsonl,
bypassing the API, so both processes must agree on where that directory is.
When BRIEF_API_URL is not set (the normal case here), this script starts
tests/_stub_api.py in-process on a random port and manages its own temp
BRIEF_HOME.

No test framework: each `scene_*` function is one assert-based check,
called in sequence from main(). See docs/evidence/F9.md for the mapping
from F9's acceptance list to these scenes.
"""
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _stub_api  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import brief_me  # noqa: E402

from playwright.sync_api import sync_playwright, expect  # noqa: E402

# F19 added `parts`/`feature_chip` to the real report_view(); _stub_api's
# minimal _r_view() predates that split/regex logic, so point it at the
# real implementation instead of duplicating it here.
_stub_api._r_view = brief_me.report_view

PROJECT = "demo-project"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_line(path, record):
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def make_question(project, title, choices, severity="normal", recommendation=None, multi=None, session_id=None):
    q = {
        "type": "question", "id": str(uuid.uuid4()), "project": project,
        "title": title, "body": f"Body text for: {title}", "choices": choices,
        "severity": severity, "created_at": now_iso(),
    }
    if recommendation is not None:
        q["recommendation"] = recommendation
    if multi is not None:
        q["multi"] = multi
    if session_id is not None:
        q["session_id"] = session_id
    return q


def make_report(project, summary, severity="normal", session_id=None):
    r = {
        "type": "report", "id": str(uuid.uuid4()), "project": project,
        "summary": summary, "severity": severity, "created_at": now_iso(),
    }
    if session_id is not None:
        r["session_id"] = session_id
    return r


def build_fixture(brief_home):
    """Writes config.json + inbox.jsonl fixture data required by F9
    acceptance item 13: a single-choice question with a recommendation, a
    multi-choice question, a high-severity question, a question already
    cancelled before the page loads, and two reports (one with session_id).
    Returns a dict of the fixture records for use by the test scenes.
    """
    os.makedirs(brief_home, exist_ok=True)
    with open(os.path.join(brief_home, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [{"name": PROJECT, "path": "/tmp/does-not-matter"}]}, f)

    inbox_path = os.path.join(brief_home, "inbox.jsonl")

    q1 = make_question(PROJECT, "Which datastore?", ["SQLite", "JSONL"], recommendation="JSONL")
    q2 = make_question(PROJECT, "Which features to ship?", ["Alpha", "Beta", "Gamma"], multi=True)
    q3 = make_question(PROJECT, "Deploy to prod now?", ["Yes", "No"], severity="high")
    q4 = make_question(PROJECT, "Old stale question", ["X", "Y"])  # pre-cancelled below
    q5 = make_question(PROJECT, "Pick a color (other only)", ["Red", "Blue"])
    q6 = make_question(PROJECT, "Will be dismissed live", ["Keep", "Drop"])
    q7 = make_question(PROJECT, "Left untouched on purpose", ["A", "B"])
    r1 = make_report(PROJECT, "First line of report one.\nMore details follow.", session_id="sess-report-1")
    r2 = make_report(PROJECT, "Report two summary line.")

    for q in (q1, q2, q3, q4, q5, q6, q7):
        append_line(inbox_path, q)
    append_line(inbox_path, {"type": "status", "ref": q4["id"], "status": "cancelled", "at": now_iso()})
    for r in (r1, r2):
        append_line(inbox_path, r)

    return {"q1": q1, "q2": q2, "q3": q3, "q4": q4, "q5": q5, "q6": q6, "q7": q7, "r1": r1, "r2": r2}


def read_inbox(brief_home):
    path = os.path.join(brief_home, "inbox.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_answers(brief_home):
    path = os.path.join(brief_home, "answers.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def statuses_for(brief_home, ref):
    return [r for r in read_inbox(brief_home) if r.get("type") == "status" and r.get("ref") == ref]


def wait_for(condition, timeout=5, interval=0.05, message="condition not met in time"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(interval)
    raise AssertionError(message)


def answers_for(brief_home, qid):
    return [a for a in read_answers(brief_home) if a["question_id"] == qid]


def card(page, qid):
    return page.locator(f'.question-card[data-id="{qid}"]')


def click_option(page, qid, value):
    card(page, qid).locator(f'[data-action="option"][data-value="{value}"]').click()


def click_skip(page, qid):
    card(page, qid).locator('[data-action="skip"]').click()


def click_other(page, qid):
    card(page, qid).locator('[data-action="other"]').click()


def fill_other(page, qid, text):
    card(page, qid).locator('.other-input').fill(text)


def click_dismiss(page, qid):
    card(page, qid).locator('[data-action="dismiss"]').click()


def main():
    if os.environ.get("BRIEF_API_URL"):
        base_url = os.environ["BRIEF_API_URL"].rstrip("/")
        brief_home = os.environ.get("BRIEF_HOME")
        if not brief_home:
            raise SystemExit(
                "BRIEF_API_URL is set but BRIEF_HOME is not - this test writes "
                "fixture data directly into BRIEF_HOME, so it must point at the "
                "same directory the running server was started with."
            )
        server = None
    else:
        brief_home = tempfile.mkdtemp(prefix="brief-me-html-test-")
        os.environ["BRIEF_HOME"] = brief_home
        server, base_url = _stub_api.start_server()

    fixture = build_fixture(brief_home)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            run_scenes(page, base_url, brief_home, fixture)
        finally:
            browser.close()
            if server is not None:
                server.shutdown()

    print("OK: all F9 Playwright scenes passed.")


def run_scenes(page, base_url, brief_home, fx):
    page.goto(base_url + "/")

    # ---- initial load / layout (acceptance items 1-3, 7, part of 12) ----
    expect(page.locator("#header-counts")).to_contain_text("6 questions")
    expect(page.locator("#header-counts")).to_contain_text("2 unread reports")
    expect(page.locator("#dismissed-entry")).to_contain_text("Dismissed (1)")  # q4 pre-cancelled
    expect(card(page, fx["q2"]["id"]).locator(".badge-multi")).to_contain_text("multi")
    expect(card(page, fx["q3"]["id"]).locator(".badge-high")).to_contain_text("high")
    # acceptance #7: watch control disabled until "Dispatch after save" is checked
    expect(page.locator("#watch-checkbox")).to_be_disabled()
    page.locator("#dispatch-checkbox").check()
    expect(page.locator("#watch-checkbox")).to_be_enabled()
    page.locator("#watch-checkbox").check()
    page.locator("#dispatch-checkbox").uncheck()
    expect(page.locator("#watch-checkbox")).to_be_disabled()
    expect(page.locator("#watch-checkbox")).not_to_be_checked()
    print("scene: initial layout / header counts / badges / watch-disabled - OK")

    # ---- build up all the answer/skip/dismiss/leave-untouched decisions ----
    # Q1: single-choice with recommendation -> select the recommended option
    click_option(page, fx["q1"]["id"], "JSONL")
    expect(page.locator("#save-btn")).to_contain_text("Save 1 answers")
    # acceptance #8 (answers panel): per-project "n new" row reflects the local selection
    expect(page.locator("#answers-projects")).to_contain_text(f"{PROJECT}: 1 new")

    # Q2: multi-choice -> two options + Other with text
    click_option(page, fx["q2"]["id"], "Alpha")
    click_option(page, fx["q2"]["id"], "Gamma")
    click_other(page, fx["q2"]["id"])
    fill_other(page, fx["q2"]["id"], "Also consider Delta")

    # Q3 (high): Skip
    click_skip(page, fx["q3"]["id"])

    # ---- acceptance #6: Other validation on blur, then filled in ----
    click_other(page, fx["q5"]["id"])
    card(page, fx["q5"]["id"]).locator(".other-input").click()  # focus it, still empty
    card(page, fx["q5"]["id"]).locator(".card-title").click()  # blur the other-input while empty
    expect(card(page, fx["q5"]["id"]).locator(".other-error-msg")).to_be_visible()
    print("scene: Other blur validation shows error - OK")

    # acceptance #6: Save is blocked (no confirm dialog, no write) while Other is
    # selected-but-empty anywhere - it must not silently fall through to the
    # untouched-question confirm flow.
    page.locator("#save-btn").click()
    expect(page.locator("#confirm-overlay")).to_be_hidden()
    assert answers_for(brief_home, fx["q1"]["id"]) == [], "Save must not write anything while an Other answer is empty"
    print("scene: Save blocked while an Other answer is selected but empty - OK")

    fill_other(page, fx["q5"]["id"], "Purple")
    expect(card(page, fx["q5"]["id"]).locator(".other-error-msg")).to_be_hidden()

    # Q6: Dismiss (pending, not sent until Save)
    click_dismiss(page, fx["q6"]["id"])
    expect(card(page, fx["q6"]["id"])).to_contain_text("Will be dismissed on save")

    # Q7: left completely untouched on purpose
    print("scene: all decisions entered (option/multi+other/skip/other-only/dismiss/untouched) - OK")

    # ---- acceptance #8: Save with an untouched question shows the confirm dialog ----
    page.locator("#save-btn").click()
    expect(page.locator("#confirm-overlay")).to_be_visible()
    expect(page.locator("#confirm-list")).to_contain_text(fx["q7"]["title"])
    page.locator("#confirm-skip-save").click()
    expect(page.locator("#confirm-overlay")).to_be_hidden()
    print("scene: confirm dialog appears for untouched question, proceeds on Skip-and-save - OK")

    # wait for save round-trip to finish: answered/dismissed questions drop out of the list
    expect(card(page, fx["q1"]["id"])).to_have_count(0)
    expect(card(page, fx["q6"]["id"])).to_have_count(0)
    # skipped questions (Q3, Q7) get no status line and stay pending, so they remain visible
    expect(card(page, fx["q3"]["id"])).to_have_count(1)
    expect(card(page, fx["q7"]["id"])).to_have_count(1)

    # ---- acceptance #5 payload shapes, #4 Skip writes nothing ----
    q1_answers = answers_for(brief_home, fx["q1"]["id"])
    assert len(q1_answers) == 1, q1_answers
    assert q1_answers[0].get("chosen") == "JSONL", q1_answers[0]
    assert "free_text" not in q1_answers[0], q1_answers[0]

    q2_answers = answers_for(brief_home, fx["q2"]["id"])
    assert len(q2_answers) == 1, q2_answers
    assert set(q2_answers[0].get("chosen", [])) == {"Alpha", "Gamma"}, q2_answers[0]
    assert q2_answers[0].get("free_text") == "Also consider Delta", q2_answers[0]

    q5_answers = answers_for(brief_home, fx["q5"]["id"])
    assert len(q5_answers) == 1, q5_answers
    assert q5_answers[0].get("free_text") == "Purple", q5_answers[0]
    assert "chosen" not in q5_answers[0], q5_answers[0]

    assert answers_for(brief_home, fx["q3"]["id"]) == []
    assert statuses_for(brief_home, fx["q3"]["id"]) == [], "Skip must not write a status line"
    assert answers_for(brief_home, fx["q7"]["id"]) == []
    assert statuses_for(brief_home, fx["q7"]["id"]) == [], "untouched-confirmed-skip must not write a status line"

    # ---- acceptance #7 (Dismiss -> cancelled) ----
    q6_statuses = statuses_for(brief_home, fx["q6"]["id"])
    assert len(q6_statuses) == 1 and q6_statuses[0]["status"] == "cancelled", q6_statuses
    assert answers_for(brief_home, fx["q6"]["id"]) == []
    print("scene: answers.jsonl / inbox.jsonl payload shapes for save - OK")

    # ---- acceptance #6: Dismissed page + Restore (-> reopened, back in QUESTIONS) ----
    page.locator("#dismissed-entry").click()
    expect(page.locator("#dismissed-view")).to_be_visible()
    expect(page.locator("#dismissed-match-count")).to_contain_text("2 / 2")  # q4 + q6

    page.locator("#dismissed-search").fill(fx["q6"]["title"][:6])
    expect(page.locator("#dismissed-match-count")).to_contain_text("1 / 2")
    page.locator("#dismissed-search").fill("")
    expect(page.locator("#dismissed-match-count")).to_contain_text("2 / 2")

    page.locator(f'.dismissed-card[data-id="{fx["q6"]["id"]}"] [data-action="restore"]').click()
    expect(page.locator("#inbox-view")).to_be_visible()
    expect(page.locator("#dismissed-view")).to_be_hidden()
    expect(card(page, fx["q6"]["id"])).to_have_count(1)  # back in QUESTIONS, answerable again
    # a reopened question is a completely ordinary pending question again: no
    # leftover "will be dismissed" state, and it can be dismissed again
    expect(card(page, fx["q6"]["id"])).not_to_have_class("dismiss-pending")
    expect(card(page, fx["q6"]["id"]).locator('[data-action="dismiss"]')).to_be_visible()

    q6_statuses_after = statuses_for(brief_home, fx["q6"]["id"])
    assert q6_statuses_after[-1]["status"] == "reopened", q6_statuses_after
    print("scene: Dismissed page filters + Restore -> reopened, back in QUESTIONS - OK")

    # acceptance #11: clicking any sidebar project while on the Dismissed page returns to the inbox view
    page.locator("#dismissed-entry").click()
    expect(page.locator("#dismissed-view")).to_be_visible()
    page.locator('.side-item[data-project="__all__"]').click()
    expect(page.locator("#inbox-view")).to_be_visible()
    expect(page.locator("#dismissed-view")).to_be_hidden()
    print("scene: sidebar project click returns from Dismissed view to inbox - OK")

    # ---- F11 (replaces F9 #9): expand does not mark read; Mark read button does, once ----
    r1_id = fx["r1"]["id"]
    r1_card = page.locator(f'.report-card[data-id="{r1_id}"]')
    r1_card.locator('[data-action="toggle-report"]').click()
    expect(r1_card.locator(".report-full")).to_be_visible()
    expect(r1_card.locator(".resume-cmd")).to_contain_text("claude --resume sess-report-1")
    expect(r1_card.locator(".dot")).to_be_visible()  # expanding alone must not mark read

    def read_count():
        return len(statuses_for(brief_home, r1_id))

    time.sleep(0.3)
    assert read_count() == 0, "expanding a report must not POST /api/read"
    r1_card.locator('[data-action="mark-read"]').click()
    wait_for(lambda: read_count() == 1, message=f"expected 1 read status, got {statuses_for(brief_home, r1_id)}")
    expect(r1_card.locator(".dot")).to_be_hidden()
    expect(r1_card.locator('[data-action="mark-read"]')).to_be_disabled()
    expect(r1_card.locator('[data-action="mark-read"]')).to_have_text("Read")
    expect(page.locator("#header-counts")).to_contain_text("1 unread reports")  # counts update without refresh

    r1_card.locator('[data-action="toggle-report"]').click()  # collapse
    r1_card.locator('[data-action="toggle-report"]').click()  # expand again
    expect(r1_card.locator(".report-full")).to_be_visible()
    time.sleep(0.3)  # give a wrongly-duplicated POST a chance to land before asserting
    assert read_count() == 1, "re-expanding a read report must not POST /api/read again"

    r2_id = fx["r2"]["id"]
    r2_card = page.locator(f'.report-card[data-id="{r2_id}"]')
    r2_card.locator('[data-action="toggle-report"]').click()
    expect(r2_card.locator(".resume-cmd")).to_have_count(0)  # no session_id -> no resume command
    page.reload()
    expect(page.locator(f'.report-card[data-id="{r1_id}"]')).to_have_count(0)  # marked -> gone
    expect(page.locator(f'.report-card[data-id="{r2_id}"]')).to_have_count(1)  # expanded only -> still unread
    print("scene: Mark read button marks once; expand-only survives reload; resume command conditional on session_id - OK")

    # ---- F13: sidebar shows running <elapsed> for a dispatched project ----
    proj = fx["r2"]["project"]
    with open(os.path.join(brief_home, "running.json"), "w", encoding="utf-8") as f:
        json.dump({proj: {"started_at": "2026-08-22T00:00:00Z", "elapsed_seconds": 754}}, f)
    page.locator("#refresh-btn").click()
    # F14 acceptance #3: the elapsed text became a plain dot (no text).
    expect(page.locator(f'.side-item[data-project="{proj}"] .side-item-running')).to_be_visible()
    expect(page.locator(f'.side-item[data-project="{proj}"] .side-item-running')).to_have_text("")
    expect(page.locator('.side-item[data-project="__all__"] .side-item-running')).to_have_count(0)
    os.remove(os.path.join(brief_home, "running.json"))
    page.locator("#refresh-btn").click()
    expect(page.locator(f'.side-item[data-project="{proj}"] .side-item-running')).to_have_count(0)
    print("scene: sidebar running indicator shows as a dot (no elapsed text) and clears - OK")

    # ---- F14 (front-end): Sessions sidebar entry + Sessions view ----
    session_fixture = {
        "running": [{
            "batch_id": "batch-1",
            "project": proj,
            "pid": 4242,
            "started_at": "2026-08-22T00:00:00Z",
            "elapsed_seconds": 754,
            "tasks": [
                {"feature": "F14", "kind": "dispatch", "title": "Task Alpha"},
                "Task Beta",
            ],
            "log": None,
            "current": {"feature": "F14", "mtime": now_iso()},
        }],
        "finished": [],
        "finished_hidden": 1,
    }
    with open(os.path.join(brief_home, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump(session_fixture, f)
    page.locator("#refresh-btn").click()
    expect(page.locator("#sessions-entry")).to_contain_text("Sessions")
    expect(page.locator("#sessions-entry")).to_contain_text("1 running")
    expect(page.locator(f'.side-item[data-project="{proj}"]')).not_to_contain_text("running ")
    page.locator("#sessions-entry").click()
    expect(page.locator("#sessions-view")).to_be_visible()
    expect(page.locator("#sessions-running-list .task-chip")).to_have_count(2)
    # F23: taskLabel formats an object task as "<feature> · <kind label>" and
    # highlights it via t.feature, a legacy string task passes through as-is.
    expect(page.locator("#sessions-running-list .task-chip--current")).to_have_text("F14 · 派工")
    expect(page.locator("#sessions-running-list .task-chip").nth(1)).to_have_text("Task Beta")
    expect(page.locator("#sessions-finished-hidden")).to_contain_text("older hidden: 1")
    os.remove(os.path.join(brief_home, "sessions.json"))
    print("scene: Sessions sidebar entry + Sessions view (tasks chips, older hidden) - OK")

    # ---- F15: finished row ended by report shows REPORT badge, no exit code ----
    with open(os.path.join(brief_home, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump({"running": [], "finished_hidden": 0, "finished": [
            {"batch_id": "b1", "project": proj, "pid": 1, "started_at": "2026-08-22T01:00:00Z",
             "finished_at": "2026-08-22T01:10:00Z", "duration_seconds": 600,
             "exit_code": None, "ended_by": "report", "tasks": [], "log": None},
            {"batch_id": "b2", "project": proj, "pid": 2, "started_at": "2026-08-22T02:00:00Z",
             "finished_at": "2026-08-22T02:10:00Z", "duration_seconds": 600,
             "exit_code": 0, "ended_by": "exit", "tasks": [], "log": None}]}, f)
    page.locator("#refresh-btn").click()
    rows = page.locator("#sessions-finished-list .session-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("REPORT")
    expect(rows.nth(0)).not_to_contain_text("exit")
    expect(rows.nth(1)).to_contain_text("Exit 0")
    os.remove(os.path.join(brief_home, "sessions.json"))
    print("scene: Finished row ended_by=report shows REPORT badge - OK")

    # ---- F19: report card three-part summary -> Done/Blocked/Handoff + feature chip ----
    # the F14/F15 scenes above leave state.view === 'sessions'; a report card
    # is only (re)rendered while viewing the inbox, so switch back first.
    page.locator('.side-item[data-project="__all__"]').click()
    expect(page.locator("#inbox-view")).to_be_visible()

    r3 = make_report(
        PROJECT,
        "F30 F31 F32 passing；waiting on review approval；docs/handoff/F30.md",
    )
    append_line(os.path.join(brief_home, "inbox.jsonl"), r3)
    page.locator("#refresh-btn").click()
    expect(page.locator(".report-card")).to_have_count(2)
    r3_card = page.locator(f'.report-card[data-id="{r3["id"]}"]')
    expect(r3_card.locator(".feature-chip")).to_have_text("F30-F32 passing")
    expect(r3_card.locator(".feature-chip")).to_have_class("feature-chip feature-chip-passing")
    expect(r3_card.locator(".report-summary-first-line")).to_have_text("waiting on review approval")
    r3_card.locator('[data-action="toggle-report"]').click()
    labels = r3_card.locator(".report-part-label")
    expect(labels).to_have_text(["Done", "Blocked", "Handoff"])
    expect(r3_card.locator(".report-part-label-blocked")).to_have_text("Blocked")
    expect(r3_card.locator(".report-parts")).to_contain_text("F30 F31 F32 passing")
    expect(r3_card.locator(".report-parts")).to_contain_text("waiting on review approval")
    expect(r3_card.locator(".report-parts")).to_contain_text("docs/handoff/F30.md")
    print("scene: report card shows Done/Blocked/Handoff parts and feature chip - OK")

    # a report with no semicolon falls back to the plain summary paragraph and a grey chip
    r2_card = page.locator(f'.report-card[data-id="{fx["r2"]["id"]}"]')
    expect(r2_card.locator(".feature-chip")).to_have_text("no change")
    expect(r2_card.locator(".feature-chip")).to_have_class("feature-chip feature-chip-none")
    r2_card.locator('[data-action="toggle-report"]').click()
    expect(r2_card.locator(".report-full p")).to_have_text(fx["r2"]["summary"])
    expect(r2_card.locator(".report-parts")).to_have_count(0)
    print("scene: report card without a three-part summary falls back to plain summary - OK")

    # ---- F20: Finished table outcome badges, killed styling, open-report link ----
    with open(os.path.join(brief_home, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump({"running": [], "finished_hidden": 0,
                   "finished_counts": {"report": 1, "killed": 2}, "finished": [
            {"batch_id": "b3", "project": proj, "pid": 3, "started_at": "2026-08-22T03:00:00Z",
             "finished_at": "2026-08-22T03:05:00Z", "duration_seconds": 300,
             "exit_code": 4294967295, "ended_by": "exit",
             "tasks": [{"feature": "F19", "kind": "sign-off", "title": "F19 sign-off"}, "F20"],
             "log": None, "outcome": "killed", "report_id": None, "report_note": None},
            {"batch_id": "b4", "project": proj, "pid": 4, "started_at": "2026-08-22T04:00:00Z",
             "finished_at": "2026-08-22T04:05:00Z", "duration_seconds": 300,
             "exit_code": None, "ended_by": "report", "tasks": [], "log": None,
             "outcome": "report", "report_id": fx["r2"]["id"], "report_note": "waiting on review"}]}, f)
    page.locator("#refresh-btn").click()
    # the F19 scenes above switch back to the inbox view; re-enter Sessions
    page.locator("#sessions-entry").click()
    frows = page.locator("#sessions-finished-list .session-row")
    expect(frows).to_have_count(2)
    expect(frows.nth(0)).to_contain_text("KILLED")
    # F23: an object task renders "F19 · 簽核"; a legacy string task ("F20")
    # passes through unchanged - both as one task-chip per line.
    expect(frows.nth(0)).to_contain_text("F19 · 簽核")
    expect(frows.nth(0)).to_contain_text("F20")
    expect(frows.nth(0)).to_contain_text("no report")
    assert "session-row--danger" in (frows.nth(0).get_attribute("class") or ""), "killed row must get the danger row background class"
    expect(frows.nth(1)).to_contain_text("REPORT")
    expect(frows.nth(1)).to_contain_text("waiting on review")
    expect(page.locator("#sessions-finished-hidden")).to_contain_text("1 reported")
    expect(page.locator("#sessions-finished-hidden")).to_contain_text("2 killed")

    frows.nth(1).locator('[data-action="toggle-session"]').click()  # F31: open-report lives in the expanded detail
    frows.nth(1).locator('[data-action="open-report"]').click()
    expect(page.locator("#inbox-view")).to_be_visible()
    expect(page.locator("#sessions-view")).to_be_hidden()
    r2_card_reopened = page.locator(f'.report-card[data-id="{fx["r2"]["id"]}"]')
    expect(r2_card_reopened.locator(".report-full")).to_be_visible()
    os.remove(os.path.join(brief_home, "sessions.json"))
    print("scene: F20 Finished table outcome badges (KILLED/REPORT), row styling, open-report link - OK")

    # ---- F31: Sessions rows/cards collapse by default; expand shows task Q&A ----
    with open(os.path.join(brief_home, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump({
            "finished_hidden": 0, "finished_counts": {"report": 0, "killed": 0},
            "running": [{
                "batch_id": "run-qa", "project": proj, "pid": 5555,
                "started_at": "2026-08-22T05:00:00Z", "elapsed_seconds": 42,
                "tasks": [{"feature": None, "kind": "dispatch", "title": "Pick a datastore"}],
                "tasks_qa": [{"question_body": "SQLite or JSONL?", "answer_chosen": "JSONL"}],
                "log": None, "current": None,
            }],
            "finished": [{
                "batch_id": "b5", "project": proj, "pid": 6, "started_at": "2026-08-22T06:00:00Z",
                "finished_at": "2026-08-22T06:05:00Z", "duration_seconds": 300,
                "exit_code": 0, "ended_by": "exit",
                "tasks": [
                    {"feature": None, "kind": "dispatch", "title": "Pick a datastore"},
                    {"feature": None, "kind": "dispatch", "title": "Mystery task"},
                ],
                "tasks_qa": [
                    {"question_body": "SQLite or JSONL?", "answer_chosen": "JSONL"},
                    {"no_matching_question": True},
                ],
                "log": "/tmp/x.log", "outcome": "ok", "report_id": None, "report_note": None,
            }],
        }, f)
    page.locator("#refresh-btn").click()
    page.locator("#sessions-entry").click()

    # default collapsed: the detail panel is hidden on both the running card
    # and the finished row.
    run_card = page.locator('.session-card-running[data-batch-id="run-qa"]')
    expect(run_card.locator(".session-row-detail")).to_be_hidden()
    fin_row = page.locator('.session-row[data-batch-id="b5"]')
    expect(fin_row.locator(".session-row-detail")).to_be_hidden()
    # collapsed finished row shows a task count, not the per-task detail.
    expect(fin_row.locator(".session-row-summary .cell-tasks")).to_have_text("2 tasks")
    # a single-task running card shows that task's own label instead.
    expect(run_card.locator(".session-row-summary .cell-tasks")).to_have_text("Pick a datastore")

    # expand the finished row: per-task question/answer breakdown appears.
    fin_row.locator('[data-action="toggle-session"]').click()
    expect(fin_row.locator(".session-row-detail")).to_be_visible()
    items = fin_row.locator(".session-task-item")
    expect(items).to_have_count(2)
    expect(items.nth(0)).to_contain_text("SQLite or JSONL?")
    expect(items.nth(0)).to_contain_text("JSONL")
    expect(items.nth(1)).to_contain_text("No matching question found.")
    # clicking an existing action inside the detail (open log) must not
    # collapse it back.
    fin_row.locator('[data-action="open-log"]').click()
    expect(fin_row.locator(".session-row-detail")).to_be_visible()
    # clicking the toggle again collapses it.
    fin_row.locator('[data-action="toggle-session"]').click()
    expect(fin_row.locator(".session-row-detail")).to_be_hidden()

    # the running card expands/collapses the same way.
    run_card.locator('[data-action="toggle-session"]').click()
    expect(run_card.locator(".session-row-detail")).to_be_visible()
    run_items = run_card.locator(".session-task-item")
    expect(run_items).to_have_count(1)
    expect(run_items.first).to_contain_text("SQLite or JSONL?")
    expect(run_items.first).to_contain_text("JSONL")
    run_card.locator('[data-action="toggle-session"]').click()
    expect(run_card.locator(".session-row-detail")).to_be_hidden()

    os.remove(os.path.join(brief_home, "sessions.json"))
    print("scene: F31 sessions rows/cards collapse by default, expand shows per-task question/answer - OK")

    # ---- F21: mobile layout (390px) - sessions table + report card don't overflow ----
    original_viewport = page.viewport_size
    with open(os.path.join(brief_home, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump({"running": [], "finished_hidden": 0,
                   "finished_counts": {"report": 1, "killed": 2}, "finished": [
            {"batch_id": "b3", "project": proj, "pid": 3, "started_at": "2026-08-22T03:00:00Z",
             "finished_at": "2026-08-22T03:05:00Z", "duration_seconds": 300,
             "exit_code": 4294967295, "ended_by": "exit",
             "tasks": [{"feature": "F19", "kind": "sign-off", "title": "F19 sign-off"}, "F20"],
             "log": None, "outcome": "killed", "report_id": None, "report_note": None},
            {"batch_id": "b4", "project": proj, "pid": 4, "started_at": "2026-08-22T04:00:00Z",
             "finished_at": "2026-08-22T04:05:00Z", "duration_seconds": 300,
             "exit_code": None, "ended_by": "report", "tasks": [], "log": None,
             "outcome": "report", "report_id": fx["r2"]["id"], "report_note": "waiting on review"}]}, f)
    page.locator("#refresh-btn").click()
    page.set_viewport_size({"width": 390, "height": 844})

    # sidebar is an off-canvas drawer at this width; open it to reach the nav
    # buttons it contains, same as a real mobile user would.
    page.locator("#sidebar-toggle").click()
    page.locator("#sessions-entry").click()
    expect(page.locator("#sessions-view")).to_be_visible()
    expect(page.locator("#sessions-finished-list .session-row")).to_have_count(2)
    sessions_width = page.evaluate("document.documentElement.scrollWidth")
    assert sessions_width <= 390, f"sessions view overflows at 390px: scrollWidth={sessions_width}"
    # F22: stacked card - tasks text spans the row, next-action button is a 44px tap target
    first_row = page.locator("#sessions-finished-list .session-row").first
    tasks_w = first_row.locator(".cell-tasks").evaluate("el => el.getBoundingClientRect().width")
    assert tasks_w >= 300, f"F22: .cell-tasks should span the row at 390px, got {tasks_w}px"
    second_row = page.locator("#sessions-finished-list .session-row").nth(1)  # report row carries "open report"
    # F31: the row toggle itself is the collapsed-state tap target.
    toggle_h = second_row.locator('[data-action="toggle-session"]').evaluate("el => el.getBoundingClientRect().height")
    assert toggle_h >= 44, f"F31: session row toggle must be >=44px tall, got {toggle_h}px"
    second_row.locator('[data-action="toggle-session"]').click()
    next_h = second_row.locator(".session-row-next button").evaluate("el => el.getBoundingClientRect().height")
    assert next_h >= 44, f"F22: next-action button must be >=44px tall, got {next_h}px"

    page.locator("#sidebar-toggle").click()
    page.locator('.side-item[data-project="__all__"]').click()
    expect(page.locator("#inbox-view")).to_be_visible()
    inbox_width = page.evaluate("document.documentElement.scrollWidth")
    assert inbox_width <= 390, f"inbox view overflows at 390px: scrollWidth={inbox_width}"

    page.set_viewport_size(original_viewport)
    os.remove(os.path.join(brief_home, "sessions.json"))
    print("scene: mobile layout (390px) - sessions table and report card fit without horizontal overflow - OK")

    # ---- F36: Settings "Dispatch windows (--watch)" + per-project Orca cards ----
    ancestor_project = "proj-with-ancestor"
    ancestor_project_2 = "proj-with-ancestor-2"
    no_ancestor_project = "proj-no-ancestor"
    self_registered_project = "proj-self-registered"
    unregistered_project = "proj-unregistered"

    write_json(os.path.join(brief_home, "config.json"), {"projects": [
        {"name": PROJECT, "path": "/tmp/does-not-matter"},
        {"name": ancestor_project, "path": "/repos/root/sub/proj-with-ancestor"},
        {"name": ancestor_project_2, "path": "/repos/root/sub2/proj-with-ancestor-2"},
        {"name": no_ancestor_project, "path": "/isolated/proj-no-ancestor"},
        {"name": self_registered_project, "path": "/repos/self/proj-self-registered"},
        {"name": unregistered_project, "path": "/repos/orphan/proj-unregistered",
         "orca": {"mode": "bind", "repo_id": "repo-gone"}},
    ]})
    orca_repos = [
        {"id": "repo-root", "displayName": "RootRepo", "path": "/repos/root"},
        {"id": "repo-self", "displayName": "SelfRepo", "path": "/repos/self/proj-self-registered"},
        {"id": "repo-other", "displayName": "OtherRepo", "path": "/somewhere/else"},
    ]
    orca_project_ancestors = {
        PROJECT: [], ancestor_project: ["repo-root"], ancestor_project_2: ["repo-root"],
        no_ancestor_project: [], self_registered_project: [], unregistered_project: [],
    }
    orca_json_path = os.path.join(brief_home, "orca.json")

    def open_settings():
        page.locator("#settings-entry").click()
        expect(page.locator("#settings-view")).to_be_visible()

    # -- badge state 1/3: orca CLI not on PATH --------------------------------
    write_json(orca_json_path, {"available": False, "running": False, "version": None,
                                "repos": [], "project_ancestors": {}})
    open_settings()
    expect(page.locator("#settings-window-badge")).to_have_text("Orca CLI not found")
    expect(page.locator("#settings-window-badge")).to_have_class("badge")
    expect(page.locator("#settings-window")).to_be_disabled()
    expect(page.locator("#settings-window")).to_have_value("console")
    expect(page.locator("#settings-window-note")).to_contain_text("Install Orca")
    expect(page.locator("#settings-orca-projects")).to_be_hidden()
    print("scene: F36 badge state - Orca CLI not found (select disabled, forced console) - OK")

    # -- badge state 2/3: available but not running ---------------------------
    write_json(orca_json_path, {"available": True, "running": False, "version": None,
                                "repos": orca_repos, "project_ancestors": orca_project_ancestors})
    open_settings()
    expect(page.locator("#settings-window-badge")).to_have_text("Orca not running")
    expect(page.locator("#settings-window-badge")).to_have_class("badge badge-orca-off")
    expect(page.locator("#settings-window")).to_be_enabled()
    expect(page.locator("#settings-window-warn-note")).to_be_visible()
    expect(page.locator("#settings-window-warn-note")).to_contain_text("falls back to console windows")
    print("scene: F36 badge state - Orca not running (warning note shown) - OK")

    # -- badge state 3/3: running, with a version ------------------------------
    write_json(orca_json_path, {"available": True, "running": True, "version": "2.0.1",
                                "repos": orca_repos, "project_ancestors": orca_project_ancestors})
    open_settings()
    expect(page.locator("#settings-window-badge")).to_have_text("Orca running - v2.0.1")
    expect(page.locator("#settings-window-badge")).to_have_class("badge badge-orca-on")
    expect(page.locator("#settings-window-warn-note")).to_be_hidden()
    print("scene: F36 badge state - Orca running (version in badge) - OK")

    # -- select "Orca tab": one card per project, ancestor-filtered dropdowns -
    page.locator("#settings-window").select_option("orca")
    expect(page.locator("#settings-orca-projects")).to_be_visible()
    expect(page.locator(".orca-project-card")).to_have_count(6)

    def option_texts(project):
        return page.locator(f'.orca-open-as[data-project="{project}"] option').all_text_contents()

    expect(page.locator(f'.orca-open-as[data-project="{ancestor_project}"]')).to_have_value("repo")
    assert option_texts(ancestor_project) == ["Own repo", "Under RootRepo"], option_texts(ancestor_project)

    # repos containing the project's own path must not produce an Under option.
    assert option_texts(self_registered_project) == ["Own repo"], option_texts(self_registered_project)

    # no ancestor at all -> Own repo only.
    assert option_texts(no_ancestor_project) == ["Own repo"], option_texts(no_ancestor_project)

    # existing bind repo_id absent from repos -> disabled "Under (unregistered)", selected, warned.
    unreg_select = page.locator(f'.orca-open-as[data-project="{unregistered_project}"]')
    assert option_texts(unregistered_project) == ["Own repo", "Under (unregistered)"], option_texts(unregistered_project)
    expect(unreg_select).to_have_value("bind:repo-gone")
    unreg_option = page.locator(f'.orca-open-as[data-project="{unregistered_project}"] option[value="bind:repo-gone"]')
    expect(unreg_option).to_be_disabled()
    unreg_card = page.locator(".orca-project-card", has=unreg_select)
    expect(unreg_card.locator(".field-note-warning")).to_be_visible()
    print("scene: F36 per-project cards - ancestor filtering, self-path exclusion, unregistered warning - OK")

    # -- only touched cards are sent; an untouched unregistered card survives Save
    page.locator(f'.orca-open-as[data-project="{ancestor_project}"]').select_option("bind:repo-root")
    page.locator(f'.orca-open-as[data-project="{ancestor_project_2}"]').select_option("bind:repo-root")
    _stub_api.last_config_patch = None
    page.locator("#settings-save-btn").click()
    expect(page.locator(".toast")).to_contain_text("Settings saved.")

    sent_projects = {p["name"]: p["orca"] for p in _stub_api.last_config_patch["projects"]}
    assert set(sent_projects) == {ancestor_project, ancestor_project_2}, sent_projects
    assert sent_projects[ancestor_project] == {"mode": "bind", "repo_id": "repo-root"}, sent_projects
    assert sent_projects[ancestor_project_2] == {"mode": "bind", "repo_id": "repo-root"}, sent_projects

    with open(os.path.join(brief_home, "config.json"), encoding="utf-8") as f:
        saved_config = json.load(f)
    saved_by_name = {p["name"]: p for p in saved_config["projects"]}
    assert saved_by_name[unregistered_project]["orca"] == {"mode": "bind", "repo_id": "repo-gone"}, \
        "an untouched unregistered card must not be rewritten on Save"
    assert saved_by_name[ancestor_project]["orca"] == {"mode": "bind", "repo_id": "repo-root"}
    print("scene: F36 Save sends only touched cards; untouched unregistered project's config is unchanged - OK")

    # -- selecting console hides the whole section -----------------------------
    page.locator("#settings-window").select_option("console")
    expect(page.locator("#settings-orca-projects")).to_be_hidden()
    print("scene: F36 selecting console hides .orca-projects - OK")

    os.remove(orca_json_path)


if __name__ == "__main__":
    main()
