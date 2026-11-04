"""Tests-first: v2.8.0 — joins for every source kind + roles management.

  JOINS  `--extra-source` accepts ANY init source (csv/tsv, sqlite/db,
         xlsx, json/jsonl, api:) — not just delimited files. Extras get
         caged count_in probes; `--on` joins the main source with a named
         extra (`prices:region=region`); a join against a missing extra or
         an unknown source kind fails honestly.
  ROLES  `neuralosd roles` — manage acl.json roles from the CLI with
         validation and a journal: set (allow-list + row filter, probes
         validated against the live menu), list, show, remove. serve gains
         per-request roles via the X-NeuralOS-Role header.
"""
import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))

CSV_MAIN = "region,units\nNorth,100\nSouth,200\n"


def _build(tmp_path, name="inst"):
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / "s.csv"
    src.write_text(CSV_MAIN, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=None,
                                 join_on=None, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


def _init_extras(tmp_path, name, extras, join_on=None):
    """extras: list of (path_or_prefix, kind_hint ignored — init profiles)."""
    from types import SimpleNamespace
    from neuralosd._cmd import init as init_cmd
    src = tmp_path / f"{name}_main.csv"
    src.write_text(CSV_MAIN, encoding="utf-8")
    out = str(tmp_path / name)
    os.makedirs(out, exist_ok=True)
    init_cmd.run(SimpleNamespace(source=str(src), out=out, name=name,
                                 packages=None, extra_sources=extras,
                                 join_on=join_on, since=None, header=None,
                                 secret_ref=None, secret_header=None))
    return out


def _res(env):
    res = env.get("results")
    return res[0] if isinstance(res, list) else res


# ── joins across source kinds ─────────────────────────────────────────────

def test_join_across_sqlite_extra(tmp_path):
    import sqlite3
    db = tmp_path / "prices.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE prices (region TEXT, price REAL)")
    con.executemany("INSERT INTO prices VALUES (?, ?)",
                    [("North", 10.0), ("South", 20.0)])
    con.commit()
    con.close()
    out = _init_extras(tmp_path, "ms1", [str(db)], join_on="region=region")
    from neuralosd._cmd._common import load_instance
    inst = load_instance(out)
    env = inst.ask("joined count", use_cache=False)
    assert env["probe"] == "joined_count"
    res = _res(env)
    assert res["count"] == 2
    rows = res.get("rows") or []
    assert all(set(r) >= {"region", "units", "price"} for r in rows)
    env = inst.ask("how many rows in prices", use_cache=False)
    assert _res(env)["count"] == 2


def test_join_across_json_extra(tmp_path):
    import json as _json
    j = tmp_path / "prices.json"
    j.write_text(_json.dumps([
        {"region": "North", "price": 10},
        {"region": "South", "price": 20}]), encoding="utf-8")
    out = _init_extras(tmp_path, "ms2", [str(j)], join_on="region=region")
    from neuralosd._cmd._common import load_instance
    inst = load_instance(out)
    env = inst.ask("joined count", use_cache=False)
    res = _res(env)
    assert res["count"] == 2
    assert all("price" in r for r in res["rows"])


def test_join_across_xlsx_extra(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    x = tmp_path / "prices.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "prices"
    ws.append(["region", "price"])
    ws.append(["North", 10])
    ws.append(["South", 20])
    wb.save(x)
    out = _init_extras(tmp_path, "ms3", [str(x)], join_on="region=region")
    from neuralosd._cmd._common import load_instance
    inst = load_instance(out)
    env = inst.ask("joined count", use_cache=False)
    res = _res(env)
    assert res["count"] == 2
    assert all("price" in r for r in res["rows"])


def test_multiple_extras_with_named_join(tmp_path):
    a = tmp_path / "prices.csv"
    a.write_text("region,price\nNorth,10\nSouth,20\n", encoding="utf-8")
    b = tmp_path / "stock.csv"
    b.write_text("region,onhand\nNorth,7\n", encoding="utf-8")
    out = _init_extras(tmp_path, "ms4", [str(a), str(b)],
                       join_on="prices:region=region")
    from neuralosd._cmd._common import load_instance
    inst = load_instance(out)
    env = inst.ask("how many rows in stock", use_cache=False)
    assert _res(env)["count"] == 1
    env = inst.ask("joined count", use_cache=False)
    assert _res(env)["count"] == 2          # main(2) ⋈ prices(2) on region


def test_unsupported_extra_kind_is_an_honest_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        _init_extras(tmp_path, "ms5", ["browser:https://example.com"])
    assert "browser" in str(e.value).lower()


# ── roles management ──────────────────────────────────────────────────────

def _roles(a_instance_dir, **kw):
    from types import SimpleNamespace
    from neuralosd._cmd import roles as roles_cmd
    ns = SimpleNamespace(instance_dir=a_instance_dir,
                         op=kw.get("op", "list"), role=kw.get("role"),
                         allow=kw.get("allow"), row_filter=kw.get(
                             "row_filter"),
                         as_json=kw.get("as_json", False))
    return roles_cmd.run(ns)


def test_roles_set_list_show_remove_round_trip(tmp_path):
    out = _build(tmp_path)
    _roles(out, op="set", role="analyst",
           allow="row_count,breakdown", row_filter="region=in:North,South")
    acl = json.loads(open(os.path.join(out, "acl.json"),
                          encoding="utf-8").read())
    role = acl["roles"]["analyst"]
    assert role["allow"] == ["row_count", "breakdown"]
    assert role["row_filter"] == {"column": "region", "in": ["North",
                                                             "South"]}
    # journal: every change is recorded
    journal = open(os.path.join(out, "acl_journal.jsonl"),
                   encoding="utf-8").read()
    assert '"op": "set"' in journal and "analyst" in journal

    _roles(out, op="remove", role="analyst")
    acl = json.loads(open(os.path.join(out, "acl.json"),
                          encoding="utf-8").read())
    assert "analyst" not in acl.get("roles", {})


def test_roles_set_validates_probes_against_the_menu(tmp_path):
    out = _build(tmp_path)
    with pytest.raises(SystemExit) as e:
        _roles(out, op="set", role="analyst", allow="row_count,no_such_probe")
    assert "no_such_probe" in str(e.value)
    acl_path = os.path.join(out, "acl.json")
    acl = json.loads(open(acl_path, encoding="utf-8").read()) \
        if os.path.isfile(acl_path) else {"roles": {}}
    assert "analyst" not in acl.get("roles", {}), \
        "a failed validation must not leave a half-written role"


def test_roles_set_validates_the_row_filter_format(tmp_path):
    out = _build(tmp_path)
    with pytest.raises(SystemExit) as e:
        _roles(out, op="set", role="analyst", allow="row_count",
               row_filter="region=NonsenseFormat")
    assert "in:" in str(e.value)


def test_roles_journal_records_removals_too(tmp_path):
    out = _build(tmp_path)
    _roles(out, op="set", role="r1", allow="row_count")
    _roles(out, op="remove", role="r1")
    lines = [json.loads(l) for l in
             open(os.path.join(out, "acl_journal.jsonl"),
                  encoding="utf-8") if l.strip()]
    ops = [l["op"] for l in lines]
    assert "set" in ops and "remove" in ops


# ── serve: per-request roles via header ───────────────────────────────────

def test_serve_role_resolution_enforces_acl(tmp_path):
    """The serve role-header contract, at the resolution level: unknown
    role -> error; known role -> a role-scoped instance; no role -> the
    default instance. (The HTTP loop adds headers on top of this.)"""
    from neuralosd._cmd._common import load_instance
    from neuralosd.service import resolve_role_instance
    out = _build(tmp_path)
    acl = {"roles": {"analyst": {
        "allow": ["row_count", "breakdown"],
        "row_filter": {"column": "region", "in": ["North"]}}}}
    with open(os.path.join(out, "acl.json"), "w", encoding="utf-8") as fh:
        json.dump(acl, fh)

    inst_full, err = resolve_role_instance("analyst", load_instance(out),
                                           out, {})
    assert err is None
    from neuralosd.router import NoResults
    try:
        env = inst_full.ask("units by region", use_cache=False)
    except NoResults as e:
        env = getattr(e, "envelope", {}) or {}
    rows = _res(env)["rows"]
    assert {r["region"] for r in rows} == {"North"}

    no_role_inst, err = resolve_role_instance(None, load_instance(out),
                                              out, {})
    assert err is None and no_role_inst is not None

    unknown, err = resolve_role_instance("ghost", load_instance(out),
                                         out, {})
    assert unknown is None and "unknown role" in err


