"""Concurrency smoke test for the atomic append rule in docs/schema.md.

Runs standalone: `python tests/test_concurrent_append.py`. No test framework,
no external dependencies (stdlib only). Operates on a temp-directory copy of
inbox.jsonl, never touches ~/.agent-brief/.
"""

import json
import os
import tempfile
import threading
import uuid

MAX_LINE_BYTES = 8 * 1024
N_WRITERS = 5
N_LINES_PER_WRITER = 20


def atomic_append_line(path, record):
    """Append one JSON record as one line, per the rule in docs/schema.md:
    open in append mode, hold an exclusive lock, write the whole line in a
    single write() call, release the lock.
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    data = line.encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("record exceeds 8 KB limit")

    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                written = os.write(fd, data)
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                written = os.write(fd, data)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    if written != len(data):
        raise IOError(f"short write: {written}/{len(data)} bytes")


def make_question():
    return {
        "type": "question",
        "id": str(uuid.uuid4()),
        "project": "agent-brief-me",
        "title": "concurrency smoke test",
        "body": "written by test_concurrent_append.py",
        "severity": "normal",
        "created_at": "2026-08-20T00:00:00Z",
    }


def writer_thread(path, errors):
    try:
        for _ in range(N_LINES_PER_WRITER):
            atomic_append_line(path, make_question())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the main thread
        errors.append(exc)


def main():
    with tempfile.TemporaryDirectory() as tmp_dir:
        inbox_path = os.path.join(tmp_dir, "inbox.jsonl")

        errors = []
        threads = [
            threading.Thread(target=writer_thread, args=(inbox_path, errors))
            for _ in range(N_WRITERS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"writer(s) raised: {errors}"

        with open(inbox_path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().split("\n") if ln]

        expected_total = N_WRITERS * N_LINES_PER_WRITER
        assert len(lines) == expected_total, (
            f"expected {expected_total} lines, got {len(lines)}"
        )

        records = [json.loads(ln) for ln in lines]  # raises if any line is corrupt

        ids = [r["id"] for r in records]
        assert len(set(ids)) == expected_total, (
            f"expected {expected_total} unique ids, got {len(set(ids))}"
        )

        for r in records:
            assert r["type"] == "question"

    print(
        f"OK: {expected_total} lines from {N_WRITERS} concurrent writers, "
        f"all valid JSON, all ids unique."
    )


if __name__ == "__main__":
    main()
