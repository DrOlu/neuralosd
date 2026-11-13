"""The two new mutable state machines: async runs and HITL confirmations.

Both are in-memory, both are shared across threads, and both used to have
implicit transitions. These tests pin the explicit tables:

  runs    queued -> running -> done | failed | cancelled
  tokens  pending -> confirmed | expired

Terminal states are final; a cancellation observed mid-run wins over a late
success; a token is single-use and its three failure modes are distinct.
"""
import threading
import time

import pytest

from neuralosd.agentic import (RUN_LEGAL_TRANSITIONS, RUN_STATES,
                               IllegalRunTransition, RunStore)
from neuralosd.hitl import CONFIRM_STATES, ConfirmStore


def _wait(store, run_id, wanted, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = store.get(run_id)
        if run and run["status"] in wanted:
            return run
        time.sleep(0.005)
    return store.get(run_id)


# ── runs ─────────────────────────────────────────────────────────────────

def test_run_reaches_done():
    store = RunStore()
    run_id = store.submit(lambda: {"ok": True})
    run = _wait(store, run_id, {"done", "failed"})
    assert run["status"] == "done"
    assert run["result"] == {"ok": True}
    assert run["started"] is not None
    assert run["finished"] is not None


def test_run_failure_is_failed():
    store = RunStore()

    def boom():
        raise ValueError("nope")

    run_id = store.submit(boom)
    run = _wait(store, run_id, {"done", "failed"})
    assert run["status"] == "failed"
    assert "ValueError" in run["error"]
    assert run["result"] is None


def test_cancel_during_run_wins_over_a_late_success():
    store = RunStore()
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        assert release.wait(2)
        return {"late": True}

    run_id = store.submit(slow)
    assert started.wait(2)
    assert store.get(run_id)["status"] == "running"
    assert store.cancel(run_id) is True
    release.set()
    run = _wait(store, run_id, {"done", "failed", "cancelled"})
    assert run["status"] == "cancelled"
    assert run["result"] is None          # the late success is not published


def test_cancel_terminal_run_is_false_and_does_not_change_it():
    store = RunStore()
    run_id = store.submit(lambda: {"ok": True})
    run = _wait(store, run_id, {"done", "failed"})
    assert run["status"] == "done"
    assert store.cancel(run_id) is False
    assert store.get(run_id)["status"] == "done"


def test_cancel_unknown_run_is_false():
    assert RunStore().cancel("nope") is False


def test_terminal_run_transitions_are_refused_by_name():
    store = RunStore()
    run = {"run_id": "r", "status": "done"}
    with pytest.raises(IllegalRunTransition) as exc:
        store._transition(run, "running")
    assert "done -> running is not legal" in str(exc.value)
    assert RUN_LEGAL_TRANSITIONS["done"] == set()
    assert RUN_LEGAL_TRANSITIONS["queued"] == {"running", "cancelled"}
    assert RUN_LEGAL_TRANSITIONS["running"] == {"done", "failed",
                                                "cancelled"}
    assert set(RUN_STATES) == set(RUN_LEGAL_TRANSITIONS)


# ── confirmations ────────────────────────────────────────────────────────

def _store_with_token(ttl=300):
    store = ConfirmStore(ttl=ttl)
    token = store.create(lambda: {"done": True}, {}, "risky")["confirm_token"]
    return store, token


def test_confirm_pending_to_confirmed():
    store, token = _store_with_token()
    assert store.pending_count() == 1
    assert store.confirm(token) == {"done": True}
    assert store.pending_count() == 0
    assert set(CONFIRM_STATES) == {"pending", "confirmed", "expired"}


def test_second_confirm_is_already_used():
    store, token = _store_with_token()
    assert store.confirm(token) == {"done": True}
    second = store.confirm(token)
    assert second["code"] == "already_used"
    assert "already used" in second["error"]


def test_expired_token_reports_expired():
    store, token = _store_with_token(ttl=-1)
    first = store.confirm(token)
    assert first["code"] == "expired"
    second = store.confirm(token)
    assert second["code"] == "expired"


def test_unknown_token_reports_invalid():
    assert ConfirmStore().confirm("nope")["code"] == "invalid"


def test_expire_moves_pending_to_expired():
    store = ConfirmStore(ttl=-1)
    store.create(lambda: {"x": 1}, {}, "a")
    store.create(lambda: {"x": 2}, {}, "b")
    assert store.pending_count() == 2
    assert store.expire() == 2
    assert store.pending_count() == 0


# ── trace bound ──────────────────────────────────────────────────────────

def test_trace_is_bounded_per_run(tmp_path):
    from neuralosd.agentic import TraceStore

    store = TraceStore(str(tmp_path), max_events=3)
    for i in range(5):
        store.record("r", {"i": i})
    events = store.read("r")
    assert len(events) == 3
    assert [e["i"] for e in events] == [2, 3, 4]
    assert not (tmp_path / ".agentic" / "traces" / "r.jsonl.tmp").exists()
