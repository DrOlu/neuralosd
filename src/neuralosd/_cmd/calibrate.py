"""`neuralosd calibrate` — fit refusal thresholds from a golden bank and a trap bank.

Two banks, one directory:

  golden.json   positives:  {"items": [{"q": "...", "expect_probe": "..."}]}
  traps.json    negatives:  {"items": [{"q": "...", "expect_refusal": true}]}

The fit searches (floor x margin) and picks the operating point that answers
every positive correctly while answering NO trap confidently. The scoring
punishes a confident wrong answer 5x harder than a false refusal, because in a
regulated deployment they are not the same order of bad: a false refusal costs
one rephrasing; a confident wrong number can be quoted in a board pack.

Writes the chosen thresholds to router.json in the instance directory, which
load_instance reads on every ask — so the operating point survives restarts
without environment variables. The full trade-off curve is printed so the
operator can see what they gave up.

The fit is pure arithmetic over PRE-COMPUTED score vectors: no probe runs, no
model is called, and it works offline on a captured bank.
"""
import itertools
import json
import os
import sys

FLOORS = (0.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
MARGINS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0)

TRAP_SCORE = -5      # confident wrong answer
HIT_SCORE = 2        # positive answered correctly
REFUSED_OK = 1       # trap refused
FALSE_REFUSAL = -1   # positive refused


def run(a):
    from ._common import load_instance
    from ..router import (GENERIC_INTENT, tokens, score_probe, extract_args,
                          _args_seen)

    golden_path = a.golden
    if not os.path.isfile(golden_path):
        raise SystemExit(f"error: no golden file: {golden_path}")
    with open(golden_path, encoding="utf-8") as f:
        golden = json.load(f)
    inst_dir = os.path.dirname(os.path.abspath(golden_path)) or "."
    inst = load_instance(inst_dir)

    traps_path = a.traps or os.path.join(inst_dir, "traps.json")
    traps = {"items": []}
    if os.path.isfile(traps_path):
        with open(traps_path, encoding="utf-8") as f:
            traps = json.load(f)
    elif not a.allow_no_traps:
        raise SystemExit(f"error: no traps file: {traps_path}\n"
                         f"       calibrating refusals without traps teaches "
                         f"the router nothing about what to refuse\n"
                         f"       (pass --allow-no-traps to fit on positives "
                         f"only)")

    vocab = inst.router._vocab

    def args_bypass(item):
        """Mirror the router: an extracted entity (id, email, status) IS the
        confidence signal, and thresholds do not apply. Without this the fit
        would mispredict caged probes - it counted person_lookup as a false
        refusal when the real router routes it on the pattern match alone."""
        from ..router import NoResults  # noqa: F401
        return item

    def domain_of(q):
        qt = tokens(q)
        d = {t for t in qt - GENERIC_INTENT if not t.isdigit()}
        return d or (qt - GENERIC_INTENT) or qt

    # Pre-compute the score table once: the fit itself is then pure set
    # arithmetic over stored vectors - no probe or model calls in the loop.
    entries = []
    for bank, items in (("golden", golden), ("traps", traps)):
        for item in items.get("items", []):
            q = item.get("q")
            vec = sorted(((score_probe(m, tokens(q)), m.name, m)
                          for m in inst.router.metas), key=lambda x: -x[0])
            entries.append({"q": q, "bank": bank, "item": item, "vec": vec})

    def simulate(entry, floor, margin):
        """('answered'|'refused', probe_or_reason) under given thresholds.

        Mirrors Router._gate, including the entity-reference bypass: when the
        question names something the top probe EXTRACTED (an id, an email), the
        thresholds do not apply - the extraction is the confidence."""
        vec, item = entry["vec"], entry["item"]
        domain = domain_of(entry["q"])
        if not vec:
            return "refused", "no_probe_matches"
        top_score, top_name, top_meta = vec[0]
        try:
            kw = extract_args(top_meta, entry["q"])
        except Exception:
            kw = None
        if kw and _args_seen(kw, entry["q"]) > 0:
            return "answered", top_name
        second = vec[1][0] if len(vec) > 1 else 0.0
        if top_score < floor:
            return "refused", "below_floor"
        if (top_score - second) < margin:
            return "refused", "ambiguous"
        cov = (len(domain & vocab.get(top_name, frozenset()))
               / max(1, len(domain)))
        if cov < 0.5:
            return "refused", "low_coverage"
        expect = item.get("expect_probe")
        if expect in (None, top_name):
            return "answered", top_name
        # right subject, wrong probe: still a wrong answer, not a refusal
        return "answered", top_name

    curve = []
    for floor, margin in itertools.product(FLOORS, MARGINS):
        score = correct = refused_ok = confident_wrong = false_refusals = 0
        for entry in entries:
            item, bank = entry["item"], entry["bank"]
            is_trap = bank == "traps" or bool(item.get("expect_refusal"))
            kind, probe = simulate(entry, floor, margin)
            if kind == "refused":
                if is_trap:
                    refused_ok += 1
                    score += REFUSED_OK
                else:
                    false_refusals += 1
                    score += FALSE_REFUSAL
            else:
                if is_trap:
                    confident_wrong += 1
                    score += TRAP_SCORE
                elif item.get("expect_probe") in (None, probe):
                    correct += 1
                    score += HIT_SCORE
                else:
                    confident_wrong += 1        # routed to the wrong probe
                    score += TRAP_SCORE
        curve.append({"floor": floor, "margin": margin, "score": score,
                      "correct": correct, "traps_refused": refused_ok,
                      "confident_wrong": confident_wrong,
                      "false_refusals": false_refusals})

    # Best = zero confident wrongs first, then fewest false refusals, then score.
    chosen = min(curve, key=lambda r: (r["confident_wrong"],
                                       r["false_refusals"], -r["score"]))
    out_path = os.path.join(inst_dir, "router.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"floor": chosen["floor"], "margin": chosen["margin"],
                   "min_question_coverage": 0.5, "refuse_disjunction": True,
                   "fitted_from": {"golden": len(golden.get("items", [])),
                                   "traps": len(traps.get("items", []))}},
                  f, indent=2)

    print(f"fitted {len(entries)} questions "
          f"({len(golden.get('items', []))} golden / {len(traps.get('items', []))} traps)")
    print(f"  chosen: floor={chosen['floor']} margin={chosen['margin']} -> "
          f"{chosen['correct']} correct, {chosen['traps_refused']} traps "
          f"refused, {chosen['confident_wrong']} confident-wrong, "
          f"{chosen['false_refusals']} false refusals")
    print(f"  written: {out_path}")
    print("\ntrade-off curve (score-ordered, top 8):")
    for row in sorted(curve, key=lambda x: -x["score"])[:8]:
        print(f"  floor={row['floor']:<5} margin={row['margin']:<4} "
              f"score={row['score']:<5} correct={row['correct']:<4} "
              f"traps_refused={row['traps_refused']:<3} "
              f"conf_wrong={row['confident_wrong']:<2} "
              f"false_ref={row['false_refusals']}")
    return 0
