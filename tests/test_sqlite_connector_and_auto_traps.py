"""Tests-first: SQLite connector (Tier 1) + auto-trap synthesis.

RED state by design — these tests fail until the features land:
  1. `neuralosd init --source file.db` scaffolds a working instance from a
     SQLite database (tables profiled, row_count per table, caged args).
  2. Registering a new probe auto-synthesizes its misuse TRAP into
     traps.json (the capability's likely-abuse question must refuse).
"""
import sqlite3

import pytest


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


def test_init_scaffolds_from_sqlite(b, tmp_path, sqlite_db):
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
    from neuralosd._cmd._common import load_instance
    inst = load_instance(A.out)
    env = inst.ask("how many rows", use_cache=False)
    assert env["probe"] == "row_count"
    assert any(r.get("count") == 3 for r in env["results"] if isinstance(r, dict))


def test_new_probe_auto_synthesizes_trap(b, tmp_path):
    """Registering a probe must auto-add its misuse trap to traps.json:
    every capability invites an abuse question, and the refusal must be
    proven at birth — not after the first incident."""
    from neuralosd.probe import probe

    @probe(description="Refund a payment (dangerous, not implemented)",
           triggers=["refund payment"])
    def refund_payment(payment_id: int):
        return {"refunded": payment_id}

    meta = refund_payment._probe
    from neuralosd.instance_audit import instance_local_modules  # noqa: F401
    # the auto-trap synthesis entry point (to be implemented):
    from neuralosd.banks import synthesize_trap_for  # noqa: F401

    import json, os
    traps = json.load(open(os.path.join(str(tmp_path), "traps.json"), encoding="utf-8"))
    item = next(t for t in traps["items"] if t["q"] == f"refund payment {meta.name}")
    assert item["expect_refusal"] is True
