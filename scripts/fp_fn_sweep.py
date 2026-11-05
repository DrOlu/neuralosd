#!/usr/bin/env python
"""False-positive / false-negative sweep for a neuralOS menu.

A router has two ways to be wrong, and they are not symmetric:

  * FALSE NEGATIVE (refusal on a question the instance can answer) — the user
    gets nothing, which is safe but useless;
  * FALSE POSITIVE (an answer to a question the instance cannot answer) — the
    user gets a confident number that means something else, which is worse
    than no answer at all.

This script measures both, plus the failure mode that hides between them: an
answer that is UNSTABLE under trivial rewording (case, punctuation, filler),
which means the gate is a coin flip rather than a rule.

Usage
-----
  python scripts/fp_fn_sweep.py                       # bundled fixture
  python scripts/fp_fn_sweep.py --instance DIR        # a real instance
  python scripts/fp_fn_sweep.py --fixture F.json --json out.json
  python scripts/fp_fn_sweep.py --stress 3            # add wording noise

Exit code is 0 only when every threshold holds, so it can gate CI.

Fixture shape (tests/fixtures/refusal_gate.json is the reference):
  {"menu": [{name, description, triggers, parameters}],
   "cases": [{question, kind: "answer"|"refuse", probe}],
   "contract": {"refusal_reasons": [...]}}
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "refusal_gate.json")


def _read_menu_file(menu_path: str) -> List[Dict[str, Any]]:
    with open(menu_path, encoding="utf-8") as fh:
        blob = json.load(fh)
    return blob["menu"] if isinstance(blob, dict) else blob


def _contract() -> Dict[str, Any]:
    """The shared contract, so an instance sweep checks the same reasons."""
    try:
        with open(DEFAULT_FIXTURE, encoding="utf-8") as fh:
            return json.load(fh).get("contract") or {}
    except (OSError, ValueError):
        return {}


def _load(path: Optional[str], instance_dir: Optional[str]) -> Dict[str, Any]:
    """Fixture mode (labelled cases) or instance mode (a real directory).

    Instance mode accepts BOTH instance shapes: a classic ``needle_menu.json``
    and a generated instance (``probes.py`` + ``bridge.py``). Using the real
    probes matters — a stub rebuilt from the menu can route perfectly while the
    real bridge raises, and a sweep that never executes the real code proves
    nothing about it.
    """
    if instance_dir:
        menu_path = os.path.join(instance_dir, "needle_menu.json")
        inst_probes = None
        if os.path.isfile(menu_path):
            menu = _read_menu_file(menu_path)
        else:
            from neuralosd._cmd._common import load_instance
            inst = load_instance(instance_dir)
            menu = inst.menu
            inst_probes = list(inst.probes)
        if not menu:
            raise SystemExit(f"error: {instance_dir} declares no probes to sweep")
        return {"menu": menu, "cases": _instance_cases(),
                "contract": _contract(), "probes": inst_probes}
    with open(path or DEFAULT_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _instance_cases() -> List[Dict[str, Any]]:
    """Refusal cases every instance must honour, whatever its domain.

    Without these, an instance sweep measures recall only — and a router that
    answers EVERYTHING scores 100% recall while being useless. These are the
    questions no instance owns; a data instance that answers "who won the world
    cup" is broken regardless of what its menu says.
    """
    return [
        {"question": "what is the weather in lagos tomorrow", "kind": "refuse"},
        {"question": "who won the world cup in 1998", "kind": "refuse"},
        {"question": "write me a poem about the sea", "kind": "refuse"},
        {"question": "delete every row in the table", "kind": "refuse"},
        {"question": "how many invoices does my accountant have", "kind": "refuse"},
        {"question": "translate this sentence into french", "kind": "refuse"},
    ]


def _arg(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror the shared conformance harness: args are OPTIONAL here.

    A trigger written as "search the report" cannot fill a required `q`, so
    with mandatory args every such trigger would score as a false negative
    when it is really a menu authoring problem. The sweep is about ROUTING.
    """
    if spec.get("enum"):
        return {"type": "enum", "values": list(spec["enum"]), "required": False}
    if spec.get("pattern"):
        return {"type": "pattern", "pattern": spec["pattern"], "required": False}
    if spec.get("type") == "integer":
        return {"type": "integer", "min": spec.get("minimum", 0),
                "max": spec.get("maximum", 10 ** 6), "required": False}
    return {"type": "pattern", "pattern": r".+", "required": False}


def _probes_from_menu(menu: List[Dict[str, Any]]):
    from neuralosd.probe import probe as probe_dec

    probes = []
    for entry in menu:
        name = entry["name"]
        props = ((entry.get("parameters") or {}).get("properties")) or {}
        args = {k: _arg(v) for k, v in props.items() if isinstance(v, dict)}

        def fn(_n=name, **kw):
            return [{"probe": _n, "args": kw}]

        fn.__name__ = name
        fn.__doc__ = entry.get("description") or name
        probes.append(probe_dec(
            description=entry.get("description") or name,
            triggers=list(entry.get("triggers") or [name]),
            args=args, name=name)(fn))
    return probes


def _router(probes, tmp: str, name: str = "sweep"):
    from neuralosd.router import Router
    return Router(probes=probes,
                  cache_file=os.path.join(tmp, "cache.json"),
                  audit_file=os.path.join(tmp, "audit.jsonl"),
                  name=name)


def _classify(router, question: str) -> Dict[str, Any]:
    """{'outcome': 'answered'|'refused'|'error', 'probe':…, 'reason':…, 'top':…}

    `top` is the highest-scoring candidate in a refusal's score vector. It lets
    the caller tell "the router did not find your probe" from "the router found
    it and the probe correctly demanded an argument it was not given" — two
    very different diagnoses.
    """
    from neuralosd.router import NoResults
    try:
        env = router.ask(question, use_cache=False)
        return {"outcome": "answered", "probe": env.get("probe"),
                "reason": None, "top": env.get("probe")}
    except NoResults as exc:
        env = exc.envelope or {}
        scores = env.get("scores") or []
        top = scores[0].get("probe") if scores and isinstance(scores[0], dict) \
            else None
        return {"outcome": "refused", "probe": None,
                "reason": env.get("refusal_reason"), "top": top}
    except Exception as exc:                       # a crash is never acceptable
        return {"outcome": "error", "probe": None,
                "reason": f"{type(exc).__name__}: {exc}", "top": None}


# Politeness only. "now"/"today" are deliberately NOT here: they are temporal
# qualifiers, and a probe that cannot honour the qualifier SHOULD refuse
# (dropped_filter) rather than silently answer an unqualified question.
_FILLER = ("please", "kindly", "for me", "thanks", "asap", "quickly")


def _perturbations(question: str, n: int, rng: random.Random) -> List[str]:
    """Wording noise that must NOT change the classification."""
    out = [question, question.upper(), question + "?", "  " + question + "  ",
           re.sub(r"\s+", " ", question.title())]
    for _ in range(n):
        filler = rng.choice(_FILLER)
        out.append(f"{filler} {question}".strip())
        out.append(question.rstrip("?") + " " + filler)
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def sweep(blob: Dict[str, Any], stress: int = 0, seed: int = 1234,
          thresholds: Optional[Dict[str, float]] = None,
          instance_mode: bool = False) -> Dict[str, Any]:
    rng = random.Random(seed)
    contract = blob.get("contract") or {}
    reasons = set(contract.get("refusal_reasons") or [])
    menu = blob.get("menu") or []
    # Real probes when we have them (instance mode); otherwise stubs faithful to
    # the menu's declared parameters.
    probes = blob.get("probes") or _probes_from_menu(menu)

    tmp = tempfile.mkdtemp(prefix="nosd-sweep-")
    router = _router(probes, tmp)

    findings: List[Dict[str, Any]] = []

    # ── 1. recall over the menu's own triggers (false negatives) ──────────
    triggers_total = triggers_hit = collisions = arg_gated = 0
    for entry in menu:
        name = entry["name"]
        for trig in entry.get("triggers") or []:
            triggers_total += 1
            got = _classify(router, trig)
            ok = got["outcome"] == "answered" and got["probe"] == name
            if not ok and got["reason"] == "args_not_extractable" \
                    and got.get("top") == name:
                # ROUTING held: this probe was identified as the best match and
                # then refused because a required argument was not in the
                # question. That is the probe being honest about a "which one?"
                # question, not a router miss — counting it as a false negative
                # would send the reader to the router when the truth is that the
                # trigger is (correctly) under-specified.
                arg_gated += 1
                ok = True
            triggers_hit += ok
            if ok:
                continue
            # A trigger that is answered by a DIFFERENT probe is a menu
            # collision, not a router miss: two entries claim the same words
            # and only one of them executes. Naming it separately is the whole
            # point — "false negative" would send someone to the wrong file.
            kind = "trigger_collision" if (
                got["outcome"] == "answered"      # another probe took it
                or got["reason"] == "ambiguous"   # two entries claim it
            ) else "false_negative"
            collisions += kind == "trigger_collision"
            findings.append({"type": kind, "question": trig,
                             "expected": name, "got": got})

    # ── 2. precision over the labelled cases (false positives) ───────────
    answered_ok = answered_total = 0
    refused_ok = refused_total = 0
    for case in blob.get("cases") or []:
        q = case["question"]
        kind = (case.get("kind") or "answer").lower()
        got = _classify(router, q)
        if kind == "answer":
            answered_total += 1
            ok = got["outcome"] == "answered" and \
                (case.get("probe") in (None, got["probe"]))
            answered_ok += ok
            if not ok:
                findings.append({"type": "missed_answer", "question": q,
                                 "expected": case.get("probe"), "got": got})
        else:
            refused_total += 1
            ok = got["outcome"] == "refused" and \
                (not reasons or got["reason"] in reasons)
            refused_ok += ok
            if not ok:
                findings.append({"type": "false_positive", "question": q,
                                 "expected": "refusal", "got": got})

    # ── 3. stability under rewording (coin-flip gate) ────────────────────
    unstable = 0
    if stress:
        probes_to_check = [(c["question"], (c.get("probe") or "refusal"))
                           for c in (blob.get("cases") or [])][:12]
        for q, expect in probes_to_check:
            base = _classify(router, q)
            for variant in _perturbations(q, stress, rng)[1:]:
                got = _classify(router, variant)
                if got["outcome"] != base["outcome"] or \
                        (base["outcome"] == "answered"
                         and got["probe"] != base["probe"]):
                    unstable += 1
                    findings.append({"type": "unstable", "question": variant,
                                     "expected": base, "got": got})
                    break

    metrics = {
        "triggers": triggers_total,
        "trigger_recall": round(triggers_hit / triggers_total, 4)
        if triggers_total else 1.0,
        "labelled_answers": answered_total,
        "labelled_answer_rate": round(answered_ok / answered_total, 4)
        if answered_total else 1.0,
        "labelled_refusals": refused_total,
        "refusal_precision": round(refused_ok / refused_total, 4)
        if refused_total else 1.0,
        "trigger_collisions": collisions,
        "arg_gated": arg_gated,
        "unstable": unstable,
        "errors": sum(1 for f in findings if f["got"]["outcome"] == "error"),
        "findings": findings,
    }
    th = {"min_trigger_recall": 0.95, "min_answer_rate": 0.95,
          "min_refusal_precision": 1.0, "max_unstable": 0, "max_errors": 0,
          # A generated instance's menu is written by `init`, not by a human
          # who read the other triggers, so collisions are REPORTED there but do
          # not fail the run — the fixture's curated menu is the one where a
          # collision is a regression.
          "max_collisions": None if instance_mode else 0}
    th.update(thresholds or {})
    metrics["thresholds"] = th
    metrics["passed"] = (
        metrics["trigger_recall"] >= th["min_trigger_recall"]
        and metrics["labelled_answer_rate"] >= th["min_answer_rate"]
        and metrics["refusal_precision"] >= th["min_refusal_precision"]
        and (th["max_collisions"] is None
             or collisions <= th["max_collisions"])
        and unstable <= th["max_unstable"]
        and metrics["errors"] <= th["max_errors"])
    return metrics


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", default=DEFAULT_FIXTURE)
    ap.add_argument("--instance", default=None,
                    help="run the trigger sweep against a real instance dir")
    ap.add_argument("--stress", type=int, default=0,
                    help="rewording variants per case (stability check)")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--json", default=None, help="write the report here")
    ap.add_argument("--min-recall", type=float, default=0.95)
    ap.add_argument("--max-unstable", type=int, default=0)
    a = ap.parse_args(argv)

    blob = _load(a.fixture, a.instance)
    report = sweep(blob, stress=a.stress, seed=a.seed,
                   thresholds={"min_trigger_recall": a.min_recall,
                               "max_unstable": a.max_unstable},
                   instance_mode=bool(a.instance))

    print(f"menu entries        : {len(blob.get('menu') or [])}")
    print(f"trigger recall      : {report['trigger_recall']:.2%} "
          f"({report['triggers']} triggers)")
    print(f"labelled answers    : {report['labelled_answer_rate']:.2%} "
          f"({report['labelled_answers']} cases)")
    print(f"refusal precision   : {report['refusal_precision']:.2%} "
          f"({report['labelled_refusals']} cases)")
    print(f"menu trigger collisions: {report['trigger_collisions']}")
    print(f"routed but arg-gated  : {report['arg_gated']} "
          f"(correct: the probe demanded an argument)")
    print(f"unstable under noise: {report['unstable']}")
    print(f"crashes             : {report['errors']}")
    for f in report["findings"][:20]:
        print(f"  [{f['type']}] {f['question']!r} -> {f['got']}")
    if len(report["findings"]) > 20:
        print(f"  … {len(report['findings']) - 20} more")
    print("PASS" if report["passed"] else "FAIL")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
