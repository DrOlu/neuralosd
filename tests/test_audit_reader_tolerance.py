"""Readers must tolerate the legacy audit — never crash, never fabricate.

Existing `ask_audit.jsonl` files are NOT migrated (a patch release does not
rewrite production ledgers). The defect-era rows have no `ts` (and some have
no `mode`); a reader that skipped them silently made the SLO undercount, so
now the count of unattributable rows is REPORTED (`rows_missing_ts`) instead
of hidden. Timestamps are never invented for such rows — a row that cannot be
dated is not silently folded into the window.
"""
import json
import time

from neuralosd.loop import refusal_rate


def _write(tmp_path, rows):
    with open(tmp_path / "ask_audit.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_legacy_no_ts_row_is_skipped_and_counted(tmp_path):
    now = time.time()
    _write(tmp_path, [
        # the exact production row the field report showed: no ts, no mode
        {"ask_id": "legacy-1", "question": "xyzzy plugh", "probe": None,
         "menu_version": "abc123", "error": "no probe matched this question",
         "results": None, "refusal_reason": "no_probe_matches", "scores": []},
        # a modern refusal and a modern answer, both inside the window
        {"ask_id": "new-1", "ts": now, "question": "q1", "probe": None,
         "menu_version": "v", "mode": "refused", "results": None,
         "refusal_reason": "no_probe_matches", "scores": []},
        {"ask_id": "new-2", "ts": now, "question": "q2", "probe": "p",
         "menu_version": "v", "mode": "deterministic",
         "results": [{"ok": 1}], "scores": []},
    ])
    stats = refusal_rate(str(tmp_path))
    assert stats["asks"] == 2          # the legacy row is not an ask
    assert stats["refused"] == 1
    assert stats["rows_missing_ts"] == 1


def test_non_numeric_ts_is_unattributable_not_crash(tmp_path):
    now = time.time()
    _write(tmp_path, [
        {"ask_id": "junk-1", "ts": "not-a-number", "question": "q",
         "probe": None, "menu_version": "v", "mode": "refused",
         "results": None, "scores": []},
        {"ask_id": "ok-1", "ts": now, "question": "q", "probe": None,
         "menu_version": "v", "mode": "refused", "results": None,
         "scores": []},
        # outside the window: counted in neither asks nor refusals
        {"ask_id": "old-1", "ts": now - 10 * 24 * 3600, "question": "q",
         "probe": None, "menu_version": "v", "mode": "refused",
         "results": None, "scores": []},
    ])
    stats = refusal_rate(str(tmp_path), window_hours=24)
    assert stats["asks"] == 1
    assert stats["refused"] == 1
    assert stats["rows_missing_ts"] == 1


def test_blank_and_malformed_lines_are_tolerated(tmp_path):
    now = time.time()
    with open(tmp_path / "ask_audit.jsonl", "a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write("{truncated json\n")
        fh.write(json.dumps({"ask_id": "ok", "ts": now, "question": "q",
                             "probe": None, "menu_version": "v",
                             "mode": "refused", "results": None,
                             "scores": []}) + "\n")
    stats = refusal_rate(str(tmp_path))
    assert stats["asks"] == 1
    assert stats["rows_missing_ts"] == 0


def test_missing_ts_is_reported_not_fabricated(tmp_path):
    """A no-ts refusal must not silently INFLATE or DEFLATE the rate: it is
    reported as unattributable, full stop."""
    _write(tmp_path, [
        {"ask_id": "legacy-r", "question": "q", "probe": None,
         "menu_version": "v", "error": "refused", "results": None,
         "refusal_reason": "no_probe_matches", "scores": []},
    ])
    stats = refusal_rate(str(tmp_path))
    assert stats["asks"] == 0
    assert stats["refused"] == 0
    assert stats["refusal_rate"] == 0.0
    assert stats["rows_missing_ts"] == 1
