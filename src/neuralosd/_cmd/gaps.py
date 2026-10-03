"""`neuralosd gaps` — mine gated/fuzzy questions from an ask audit log."""
import json
import os


def run(a):
    log_path = a.log
    if not os.path.isfile(log_path):
        raise SystemExit(f"error: no audit log: {log_path}")

    rows = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    menu = []
    if os.path.isfile(a.menu):
        with open(a.menu, encoding="utf-8") as f:
            data = json.load(f)
        menu = data.get("menu", data) if isinstance(data, dict) else data
    known = {m["name"] for m in menu} if menu else set()

    # Mine EVERY shape of "the menu struggled here", not only hard refusals:
    #   refusals with a calibrated reason (below_floor, ambiguous, ...)
    #   a confident answer to a question the probe only partly understood
    #     (discarded terms/filters - previously logged as a plain success, so
    #      the worst failure class was invisible to this miner)
    #   the legacy gated/fuzzy markers from older audit shapes
    gaps = []
    for r in rows:
        probe = r.get("probe")
        reason = r.get("refusal_reason")
        refused = r.get("results") is None and (reason or r.get("error"))
        discarded = r.get("discarded") or {}
        fuzzy = bool(discarded.get("terms") or discarded.get("filters"))
        gated = r.get("gated") or r.get("fuzzy") or r.get("no_results")
        unmatched = bool(menu) and probe not in known
        if refused:
            gaps.append({"question": r.get("question") or r.get("q"),
                         "probe": probe,
                         "reason": reason or "refused",
                         "scores": r.get("scores")})
        elif fuzzy:
            gaps.append({"question": r.get("question") or r.get("q"),
                         "probe": probe,
                         "reason": "partial_answer",
                         "discarded": discarded})
        elif gated or unmatched:
            gaps.append({"question": r.get("question") or r.get("q"),
                         "probe": probe,
                         "reason": "gated" if gated else "unmatched",
                         "score": r.get("score")})

    seen, uniq = set(), []
    for g in gaps:
        key = (g["question"] or "").lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(g)

    out = {"source": log_path, "count": len(uniq), "gaps": uniq}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n{len(uniq)} gaps written to {a.out}", file=__import__("sys").stderr)