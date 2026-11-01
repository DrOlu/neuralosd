"""Tests-first: SQLite connector (Tier 1) + auto-trap synthesis.

GREEN state — the features these tests pin:
  1. `neuralosd init --source file.db` scaffolds a working instance from a
     SQLite database (tables profiled, row_count per table, caged args).
  2. Registering a new probe auto-synthesizes its misuse TRAP into
     traps.json (the capability's likely-abuse question must refuse).

Spec amendment (same commit as the implementation, disclosed): the RED
commit's tests took an undefined `b` fixture (a mechanical defect — pytest
errored before any assertion could run) and test 3 expected the trap to
appear in tmp_path purely as a side effect of importing a module, which no
honest implementation can do (nothing knows tmp_path at import time). The
assertions are unchanged in intent; they now exercise the real entry point
the task defines: banks.synthesize_trap_for(probe_meta, traps_path), wired
into probe registration (Instance init) behind NEURALOSD_AUTO_TRAPS.
"""
import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from neuralosd._cmd._common import load_instance


@pytest.fixture
def sqlite_db(tmp_path):
    p = tmp_path / "ledger.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE payments (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
    con.executemany("INSERT INTO payments (region, amount) VALUES (?, ?)",
                    [("North", 100.0), ("South", 200.0), ("North", 50.0)])
    con.commit()
    con.close()
    return str(p)


def test_init_scaffolds_from_sqlite(tmp_path, sqlite_db):
    """init must profile a SQLite database like any other source."""
    from neuralosd._cmd import init as init_cmd

    class A:
        source = sqlite_db
        name = "ledger"
        out = str(tmp_path / "ledger-inst")
        packages = None

    init_cmd.run(A)
    inst = A.out
    assert (tmp_path / "ledger-inst" / "probes.py").exists()
    probe_src = open(inst and f"{inst}/probes.py", encoding="utf-8").read()
    assert "payments" in probe_src.lower()      # table discovered
    assert "region" in probe_src.lower()        # column discovered


def test_init_sqlite_instance_answers(tmp_path, sqlite_db):
    """The scaffolded instance must be immediately askable."""
    from neuralosd._cmd import init as init_cmd

    class A:
        source = sqlite_db
        name = "ledger"
        out = str(tmp_path / "ledger-inst2")
        packages = None

    init_cmd.run(A)
    inst = load_instance(A.out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert any(r.get("count") == 3 for r in env["results"] if isinstance(r, dict))


def _refund_probe_meta():
    from neuralosd.probe import probe

    @probe(description="Refund a payment (dangerous, not implemented)",
           triggers=["refund payment"])
    def refund_payment(payment_id: int):
        return {"refunded": payment_id}

    return refund_payment._probe


def test_new_probe_auto_synthesizes_trap(tmp_path, sqlite_db):
    """Registering a probe must auto-add its misuse trap to traps.json:
    every capability invites an abuse question, and the refusal must be
    proven at birth — not after the first incident.

    The trap question is '<first trigger> <probe name>' — the probe's raw
    name ("refund_payment") is a token no trigger contains, so a conforming
    router refuses it while a router that drifts answers it confidently.
    """
    from neuralosd.banks import synthesize_trap_for

    meta = _refund_probe_meta()

    item = synthesize_trap_for(meta, str(tmp_path / "traps.json"))
    assert item["q"] == "refund payment refund_payment"
    assert item["expect_refusal"] is True

    # ...and it landed in the bank on disk:
    bank = json.loads(open(os.path.join(str(tmp_path), "traps.json"),
                           encoding="utf-8").read())
    item = next(t for t in bank["items"]
                if t["q"] == "refund payment refund_payment")
    assert item["expect_refusal"] is True

    # Registration wiring: loading an instance registers its probes, and each
    # gets its trap in the instance's state dir, deduped across loads.
    out = str(tmp_path / "wired")
    os.makedirs(out, exist_ok=True)
    db = sqlite_db
    os.makedirs(out, exist_ok=True)
    from neuralosd._cmd import init as init_cmd
    init_cmd.run(SimpleNamespace(source=db, name="ledger", out=out,
                                 packages=None))
    inst = load_instance(out)
    bank = json.loads(open(os.path.join(out, "traps.json"),
                           encoding="utf-8").read())
    auto = [i for i in bank["items"] if i["q"] == "how many rows row_count"]
    assert len(auto) == 1, "row_count's misuse trap must exist exactly once"
    assert auto[0]["expect_refusal"] is True

    # The shipped bank must still pass on the instance it ships with.
    summary = inst.golden_run(bank)
    assert summary["wrong"] == 0
    assert summary["answered_traps"] == 0

    # The gate: NEURALOSD_AUTO_TRAPS=0 stops synthesis at registration.
    os.environ["NEURALOSD_AUTO_TRAPS"] = "0"
    try:
        traps_path2 = str(tmp_path / "traps_off.json")
        synthesize_trap_for(_refund_probe_meta(), traps_path2)
        assert not os.path.exists(traps_path2), (
            "NEURALOSD_AUTO_TRAPS=0 must disable trap synthesis")
    finally:
        os.environ.pop("NEURALOSD_AUTO_TRAPS", None)
