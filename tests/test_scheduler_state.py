"""The scheduler's mutable state: schedules.json and per-run outcomes.

Two defects motivated this file: `remove()` held the lock and called
`_save()`, which took the same non-reentrant lock (every remove deadlocked),
and both writes were non-atomic (a crash could truncate the schedule list or
a result file). The entry now also records the last outcome, so "it ran" is
not the only thing the state can say.
"""
import json
import threading

from neuralosd.scheduler import Scheduler


def test_remove_does_not_deadlock(tmp_path):
    scheduler = Scheduler(str(tmp_path), lambda _i, _q: {})
    scheduler.add("a", 60, "inst", "how many rows")
    result = {}

    def go():
        result["removed"] = scheduler.remove("a")

    thread = threading.Thread(target=go)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive(), "remove() deadlocked"
    assert result["removed"] == 1


def test_save_is_atomic_and_round_trips(tmp_path):
    scheduler = Scheduler(str(tmp_path), lambda _i, _q: {})
    scheduler.add("a", 60, "inst", "how many rows")
    path = tmp_path / "schedules.json"
    assert path.exists()
    assert not (tmp_path / "schedules.json.tmp").exists()

    reloaded = Scheduler(str(tmp_path), lambda _i, _q: {})
    assert reloaded.list()[0]["name"] == "a"


def test_corrupt_schedules_file_loads_empty(tmp_path):
    (tmp_path / "schedules.json").write_text("{not json", encoding="utf-8")
    scheduler = Scheduler(str(tmp_path), lambda _i, _q: {})
    assert scheduler.list() == []


def test_run_records_success(tmp_path):
    scheduler = Scheduler(str(tmp_path), lambda _i, _q: {"ok": True})
    entry = scheduler.add("a", 60, "inst", "how many rows")
    status, error = scheduler._run(entry)
    assert (status, error) == ("ok", None)
    out = tmp_path / "scheduled" / "a.json"
    assert json.loads(out.read_text(encoding="utf-8"))["result"] == {
        "ok": True}
    assert not (tmp_path / "scheduled" / "a.json.tmp").exists()


def test_run_records_failure_without_writing_a_result(tmp_path):
    def boom(_i, _q):
        raise ValueError("nope")

    scheduler = Scheduler(str(tmp_path), boom)
    entry = scheduler.add("b", 60, "inst", "how many rows")
    status, error = scheduler._run(entry)
    assert status == "error"
    assert "nope" in error
    assert not (tmp_path / "scheduled" / "b.json").exists()
