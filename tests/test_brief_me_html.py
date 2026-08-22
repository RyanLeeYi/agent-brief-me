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

from playwright.sync_api import sync_playwright, expect  # noqa: E402

PROJECT = "demo-project"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_line(path, record):
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


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


if __name__ == "__main__":
    main()
