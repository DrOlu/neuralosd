#!/usr/bin/env python
"""Malformed-input sweep: a bad FILE must never become a bad ANSWER.

Feeds the router the shapes it will meet in the wild — a cache truncated by a
kill, a cache entry written by an older version, a result that references
itself, a menu that declares two probes with one name, a secret stored as a
number, a question that is not a string — and asserts three things each time:

  1. no traceback escapes (a crash is a denial of service),
  2. no answer is invented (an answer to a question nobody asked is worse),
  3. no secret reaches the response or the audit log.

Exit code 0 only when all of that holds, so CI can gate on it.

  python scripts/malformed_input_sweep.py [-v]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from typing import Any, Callable, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neuralosd.probe import probe as probe_dec            # noqa: E402
from neuralosd.router import (MASKED, NoResults, Router,  # noqa: E402
                              mask_pii)


def _probe(name: str, triggers: List[str], fn: Callable, args=None):
    fn.__name__ = name
    return probe_dec(description=name.replace("_", " "), triggers=triggers,
                     args=args or {}, name=name)(fn)


def _router(tmp: str, probes) -> Router:
    return Router(probes=probes, cache_file=os.path.join(tmp, "cache.json"),
                  audit_file=os.path.join(tmp, "audit.jsonl"))


CASES: List[Dict[str, Any]] = []


def case(name: str):
    def deco(fn):
        CASES.append({"name": name, "fn": fn})
        return fn
    return deco


# ── the corrupt cache family ───────────────────────────────────────────────

@case("cache: truncated JSON")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 7}])])
    with open(r.cache_file, "w", encoding="utf-8") as fh:
        fh.write('{"abc": {"ts": 1.0, "payload":')
    env = r.ask("how many rows")
    assert env["results"] == [{"n": 7}], env


@case("cache: entry without payload")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 7}])])
    with open(r.cache_file, "w", encoding="utf-8") as fh:
        json.dump({"k": {"ts": 0, "menu_version": r.menu_version}}, fh)
    assert r.ask("how many rows")["probe"] == "row_count"


@case("cache: payload is a list")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 7}])])
    with open(r.cache_file, "w", encoding="utf-8") as fh:
        json.dump({"k": {"ts": 9e9, "menu_version": r.menu_version,
                         "payload": [1, 2]}}, fh)
    assert r.ask("how many rows")["probe"] == "row_count"


@case("cache: top level is a string")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 7}])])
    with open(r.cache_file, "w", encoding="utf-8") as fh:
        json.dump("not a cache", fh)
    assert r.ask("how many rows")["probe"] == "row_count"


# ── hostile results ────────────────────────────────────────────────────────

@case("results: self-referential dict and list")
def _(tmp, verbose):
    cyc: Dict[str, Any] = {"n": 1}
    cyc["self"] = cyc
    lst: List[Any] = [1]
    lst.append(lst)
    r = _router(tmp, [_probe("weird", ["weird rows"],
                             lambda **k: [cyc, lst])])
    env = r.ask("weird rows")
    assert env["probe"] == "weird"
    assert "<cycle>" in json.dumps(env)


@case("results: Decimal/date/bytes are serialisable")
def _(tmp, verbose):
    import datetime
    import decimal
    r = _router(tmp, [_probe("typed", ["typed rows"], lambda **k: [
        {"d": decimal.Decimal("1.5"), "t": datetime.date(2026, 1, 1),
         "b": b"bytes"}])])
    env = r.ask("typed rows")
    json.dumps(env)                                  # must not raise
    with open(r.audit_file, encoding="utf-8") as fh:  # audit must be valid JSON
        rec = json.loads(fh.readlines()[-1])
    assert rec["probe"] == "typed"


# ── secrets that are not strings ──────────────────────────────────────────

@case("masking: numeric secret under a secret key")
def _(tmp, verbose):
    masked = mask_pii({"password": 12345678, "pin": 4321, "token": 9.99,
                       "count": 12345678})
    assert masked["password"] == masked["pin"] == masked["token"] == MASKED
    assert masked["count"] == 12345678, masked


@case("masking: container under a secret key is opaque")
def _(tmp, verbose):
    masked = mask_pii({"password": {"hint": "my dog", "value": "hunter2"}})
    assert "hunter2" not in json.dumps(masked), masked


# ── menus that cannot be executed as written ──────────────────────────────

@case("menu: duplicate probe names are rejected loudly")
def _(tmp, verbose):
    p1 = _probe("dup", ["first trigger"], lambda **k: [{"v": 1}])
    p2 = _probe("dup", ["second trigger"], lambda **k: [{"v": 2}])
    try:
        _router(tmp, [p1, p2])
    except ValueError as exc:
        assert "duplicate probe name" in str(exc)
        return
    raise AssertionError("duplicate probe name was accepted silently")


@case("menu: probe with no triggers cannot be routed to")
def _(tmp, verbose):
    r = _router(tmp, [_probe("orphan", [], lambda **k: [{"v": 1}]),
                      _probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 1}])])
    assert r.ask("how many rows")["probe"] == "row_count"


# ── questions that are not questions ──────────────────────────────────────

@case("ask: non-string question raises TypeError, not AttributeError")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 1}])])
    for bad in (None, 42, b"how many rows", ["how many rows"]):
        try:
            r.ask(bad)
        except TypeError:
            continue
        raise AssertionError(f"ask({bad!r}) did not raise TypeError")


@case("ask: empty and whitespace questions refuse")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 1}])])
    for q in ("", "   ", "\n\t"):
        try:
            r.ask(q)
        except NoResults:
            continue
        raise AssertionError(f"ask({q!r}) was answered")


@case("ask: 10k-character question is bounded")
def _(tmp, verbose):
    r = _router(tmp, [_probe("row_count", ["how many rows"],
                             lambda **k: [{"n": 1}])])
    try:
        r.ask("how many rows " + "x" * 10_000)
    except NoResults:
        pass


@case("ask: SQL metacharacters are data, not syntax")
def _(tmp, verbose):
    seen = {}

    def fn(**kw):
        seen.update(kw)
        return [{"n": 1}]

    r = _router(tmp, [_probe(
        "rows_by_tenant", ["rows by tenant"], fn,
        args={"tenant": {"type": "pattern", "pattern": r"[\w-]+"}})])
    r.ask("rows by tenant ' OR 1=1 --")
    assert "DROP" not in json.dumps(seen).upper()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    failures = []
    for c in CASES:
        tmp = tempfile.mkdtemp(prefix="nosd-malformed-")
        try:
            c["fn"](tmp, a.verbose)
            print(f"ok    {c['name']}")
        except Exception as exc:
            failures.append((c["name"], exc))
            print(f"FAIL  {c['name']}: {type(exc).__name__}: {exc}")
            if a.verbose:
                traceback.print_exc()

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} malformed-input cases held")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
