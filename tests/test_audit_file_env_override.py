"""Self-test traffic must never write into the user refusal ledger.

`neuralosd golden` (+ trap replay, + truth) walk the same Router a live
instance serves. On one production instance the user audit held 1677 rows of
which 1569 were bank replays (~17x across menu versions), 44 watchdog ticks
and 64 human asks — a "15% user refusal rate" that was actually bank replays
plus one QA session.

Two layers of defence, both tested here:

  * `NEURALOSD_AUDIT_FILE` overrides the audit path for any Instance
    (unset behaviour is byte-identical to before: `ask_audit.jsonl`);
  * `golden_run` redirects to `ask_audit.selftest.jsonl` BY DEFAULT — an env
    var nobody sets does not fix the bug — and restores the user ledger
    afterwards. An explicit `audit_file=` argument wins over both.
"""
import json
import os

import pytest

from neuralosd import probe as probe_dec
from neuralosd.instance import Instance


def _probe(name, triggers, fn):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args={}, name=name)(fn)


def _probes():
    def total_sales():
        return [{"total": 42}]

    return [_probe("total_sales", ["total sales"], total_sales)]


def _bank():
    return {"items": [
        {"q": "total sales", "expect_probe": "total_sales"},
        {"q": "delete all sales", "expect_refusal": True},
        {"q": "xyzzy plugh", "expect_refusal": True},
    ]}


def _read(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _rows(path):
    raw = _read(path)
    if raw is None:
        return []
    return [json.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]


def test_unset_env_keeps_the_default_path(tmp_path, monkeypatch):
    monkeypatch.delenv("NEURALOSD_AUDIT_FILE", raising=False)
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    assert inst.router.audit_file == os.path.join(str(tmp_path),
                                                  "ask_audit.jsonl")


def test_env_override_redirects_the_audit_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURALOSD_AUDIT_FILE", "custom_audit.jsonl")
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    assert inst.router.audit_file == os.path.join(str(tmp_path),
                                                  "custom_audit.jsonl")
    inst.ask("total sales")
    assert len(_rows(os.path.join(str(tmp_path), "custom_audit.jsonl"))) == 1
    assert _read(os.path.join(str(tmp_path), "ask_audit.jsonl")) is None


def test_golden_never_touches_the_user_ledger_by_default(tmp_path, monkeypatch):
    """No env var set: the bank replay still must not write ask_audit.jsonl."""
    monkeypatch.delenv("NEURALOSD_AUDIT_FILE", raising=False)
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    user_ledger = os.path.join(str(tmp_path), "ask_audit.jsonl")
    selftest = os.path.join(str(tmp_path), "ask_audit.selftest.jsonl")

    before = _read(user_ledger)
    summary = inst.golden_run(_bank())
    assert summary["total"] == 3

    # the user ledger is byte-identical (absent, here — golden created it not)
    assert _read(user_ledger) == before
    # the selftest ledger grew by exactly the bank size
    rows = _rows(selftest)
    assert len(rows) == 3
    assert {r["source"] for r in rows} == {"golden", "trap"}
    # positives and traps are stamped distinctly
    assert [r["source"] for r in rows] == ["golden", "trap", "trap"]

    # and the user path is restored after the run
    inst.ask("total sales")
    assert len(_rows(user_ledger)) == 1
    assert _rows(user_ledger)[0]["source"] == "user"


def test_golden_honours_explicit_audit_argument(tmp_path, monkeypatch):
    """--audit <path> (CLI) / audit_file= (API) wins over env AND default."""
    monkeypatch.setenv("NEURALOSD_AUDIT_FILE", "from_env.jsonl")
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    mine = os.path.join(str(tmp_path), "mine.jsonl")
    inst.golden_run(_bank(), audit_file=mine)
    assert len(_rows(mine)) == 3
    assert _read(os.path.join(str(tmp_path), "from_env.jsonl")) is None
    assert _read(os.path.join(str(tmp_path),
                              "ask_audit.selftest.jsonl")) is None


def test_golden_respects_env_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURALOSD_AUDIT_FILE", "ask_audit.selftest.jsonl")
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    inst.golden_run(_bank())
    assert len(_rows(os.path.join(str(tmp_path),
                                  "ask_audit.selftest.jsonl"))) == 3
    assert _read(os.path.join(str(tmp_path), "ask_audit.jsonl")) is None


def test_trap_replays_are_stamped_trap(tmp_path, monkeypatch):
    """The vocabulary is user|watchdog|golden|trap|serve — traps are traps,
    even when they arrive inside the same golden run."""
    monkeypatch.delenv("NEURALOSD_AUDIT_FILE", raising=False)
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    inst.golden_run(_bank())
    rows = _rows(os.path.join(str(tmp_path), "ask_audit.selftest.jsonl"))
    traps = [r for r in rows if r["source"] == "trap"]
    assert len(traps) == 2
    assert all(r["mode"] in ("refused",) or r["results"] is None
               for r in traps)


def test_explicit_audit_path_creates_missing_parent_dirs(tmp_path):
    """`--audit ./audits/run.jsonl` with no audits/ dir must NOT silently lose
    the replay's rows: router._audit swallows write failures (an answer must
    never die because its ledger is unreachable), so the redirect layer does
    the mkdir -p at the one place every self-test redirect passes."""
    inst = Instance("i", _probes(), state_dir=str(tmp_path))
    target = os.path.join(str(tmp_path), "audits", "run-42.jsonl")
    inst.golden_run(_bank(), audit_file=target)
    assert len(_rows(target)) == 3
    assert _read(os.path.join(str(tmp_path), "ask_audit.jsonl")) is None
