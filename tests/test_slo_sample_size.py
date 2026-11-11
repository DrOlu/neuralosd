"""E8 — a refusal rate needs a sample.

`refusal_rate()` breached whenever ``asks > 0 and rate > slo``, so a single
refusal in a 24 h window was a 100 % refusal rate and a fleet incident.
On a 21-instance fleet three low-traffic instances flapped to BREACH on one
correctly-refused "status" ask each, and `fleet` exited 1 — paging a
scheduler on a statistical artefact.

The fix adds ``MIN_ASKS_FOR_SLO`` (``--min-asks`` / ``NEURALOSD_MIN_ASKS``):
undersized windows still report their rate but cannot breach, and gain
``insufficient_data`` + ``min_asks``. These tests are pure arithmetic over a
hand-written ``ask_audit.jsonl`` — no instance scaffolding, no probes.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from neuralosd.loop import MIN_ASKS_FOR_SLO, fleet_report, refusal_rate


@pytest.fixture(autouse=True)
def _no_ambient_min_asks(monkeypatch):
    """The developer's shell must not change what these tests measure."""
    monkeypatch.delenv("NEURALOSD_MIN_ASKS", raising=False)


def _row(refused=True, ts=None, source="user", reason="ambiguous"):
    return {"ask_id": "x",
            "ts": time.time() if ts is None else ts,
            "question": "status", "normalized": "status",
            "probe": None if refused else "status_probe",
            "mode": "refused" if refused else "answered",
            "source": source,
            "results": None if refused else [{"value": 1}],
            "refusal_reason": reason if refused else None}


def _ledger(inst, rows):
    os.makedirs(inst, exist_ok=True)
    with open(os.path.join(inst, "ask_audit.jsonl"), "w",
              encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(inst)


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "neuralosd", *args],
        capture_output=True, text=True, timeout=120)


def test_single_refusal_does_not_breach(tmp_path):
    inst = _ledger(tmp_path / "one", [_row()])
    stats = refusal_rate(inst)
    assert stats["asks"] == 1
    assert stats["refused"] == 1
    assert stats["refusal_rate"] == 1.0          # the number is still shown
    assert stats["breach"] is False
    assert stats["insufficient_data"] is True
    assert stats["min_asks"] == MIN_ASKS_FOR_SLO


def test_zero_asks_is_insufficient(tmp_path):
    stats = refusal_rate(str(tmp_path / "empty"))
    assert stats["asks"] == 0
    assert stats["refused"] == 0
    assert stats["refusal_rate"] == 0.0
    assert stats["breach"] is False
    assert stats["insufficient_data"] is True


def test_boundary_exactly_min_asks_is_judged(tmp_path):
    inst = _ledger(tmp_path / "boundary",
                   [_row(refused=i < 5) for i in range(10)])
    stats = refusal_rate(inst, min_asks=10)
    assert stats["asks"] == 10
    assert stats["breach"] is True
    assert stats["insufficient_data"] is False
    # max_refusal_rate >= 1.0 can never breach: a rate cannot exceed 1.
    loose = refusal_rate(inst, min_asks=10, max_refusal_rate=1.0)
    assert loose["breach"] is False
    assert loose["insufficient_data"] is False


def test_one_below_min_asks_is_not_judged(tmp_path):
    inst = _ledger(tmp_path / "below", [_row() for _ in range(9)])
    stats = refusal_rate(inst, min_asks=10)
    assert stats["asks"] == 9
    assert stats["breach"] is False
    assert stats["insufficient_data"] is True


def test_adequate_sample_below_slo_is_healthy(tmp_path):
    inst = _ledger(tmp_path / "healthy",
                   [_row(refused=i < 2) for i in range(10)])
    stats = refusal_rate(inst, min_asks=10)
    assert stats["breach"] is False
    assert stats["insufficient_data"] is False


def test_params_and_env_override(tmp_path, monkeypatch):
    inst = _ledger(tmp_path / "two", [_row(), _row()])
    # Default 10: a two-ask window is undersized, so 100 % cannot breach.
    assert refusal_rate(inst)["breach"] is False
    assert refusal_rate(inst, min_asks=2)["breach"] is True
    monkeypatch.setenv("NEURALOSD_MIN_ASKS", "2")
    assert refusal_rate(inst)["breach"] is True


def test_slo_rate_is_never_hidden(tmp_path):
    cases = [
        ([], 0),
        ([_row()], 1),
        ([_row() for _ in range(9)], 9),
        ([_row(refused=i < 5) for i in range(10)], 10),
        ([_row(refused=i < 2) for i in range(10)], 10),
    ]
    for n, (rows, asks) in enumerate(cases):
        stats = refusal_rate(_ledger(tmp_path / f"c{n}", rows))
        assert stats["asks"] == asks
        expected = round(stats["refused"] / asks, 4) if asks else 0.0
        assert stats["refusal_rate"] == expected


def test_fleet_counts_undersized_separately(tmp_path):
    under = _ledger(tmp_path / "under", [_row()])
    breach = _ledger(tmp_path / "breach",
                     [_row(refused=i < 5) for i in range(10)])
    healthy = _ledger(tmp_path / "healthy",
                      [_row(refused=i < 2) for i in range(10)])
    report = fleet_report([under, breach, healthy])
    assert report["undersized"] == 1
    assert report["breached"] == 1
    assert report["healthy"] == 1
    by_name = {i["instance"]: i for i in report["instances"]}
    assert by_name["under"]["insufficient_data"] is True
    assert by_name["under"]["breach"] is False
    assert by_name["breach"]["breach"] is True


def test_fleet_exit_code_ignores_undersized(tmp_path):
    under = _ledger(tmp_path / "under", [_row()])
    only = _run_cli("fleet", under)
    assert only.returncode == 0, only.stderr
    assert "n/a" in only.stdout
    assert "undersized" in only.stdout

    breach = _ledger(tmp_path / "breach",
                     [_row(refused=i < 5) for i in range(10)])
    mixed = _run_cli("fleet", under, breach)
    assert mixed.returncode == 1, mixed.stdout
    assert "BREACH" in mixed.stdout


def test_slo_cli_exits_zero_when_undersized(tmp_path):
    inst = _ledger(tmp_path / "one", [_row()])
    proc = _run_cli("slo", "--instance-dir", inst)
    assert proc.returncode == 0, proc.stderr
    assert "insufficient data" in proc.stdout
    assert '"breach": false' in proc.stdout
    assert '"insufficient_data": true' in proc.stdout


def test_rows_missing_ts_unaffected(tmp_path):
    rows = [_row() for _ in range(9)]
    rows.append(_row(ts="not-a-timestamp"))
    stats = refusal_rate(_ledger(tmp_path / "ts", rows), min_asks=10)
    assert stats["rows_missing_ts"] == 1
    assert stats["asks"] == 9          # the undated row is in neither bucket
    assert stats["refused"] == 9
    assert stats["insufficient_data"] is True


def test_window_and_sources_still_apply(tmp_path):
    rows = [_row() for _ in range(9)]
    rows += [_row(source="golden") for _ in range(3)]
    inst = _ledger(tmp_path / "sources", rows)
    # Unfiltered: 12 asks, all refused → an adequate sample that breaches.
    unfiltered = refusal_rate(inst, min_asks=10)
    assert unfiltered["asks"] == 12
    assert unfiltered["breach"] is True
    # Filtered to human traffic first: 9 asks → undersized, cannot breach.
    filtered = refusal_rate(inst, min_asks=10, sources=("user",))
    assert filtered["asks"] == 9
    assert filtered["refused"] == 9
    assert filtered["insufficient_data"] is True
    assert filtered["breach"] is False
