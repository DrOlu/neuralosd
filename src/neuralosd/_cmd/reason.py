"""`neuralosd reason` — escalate a question to a reasoning model, then extend the menu.

    neuralosd reason --instance-dir ~/neuralos-instances/chinook \
                     --question "what share of total revenue comes from the top 10 customers" \
                     --oracle 0.193764 --install

What happens, in order:

  1. the deterministic gate decides whether to escalate AT ALL (no model)
  2. every probe is called once to build a CLOSED inventory of what exists
  3. the reasoning model picks two ids from that list — nothing else
  4. ids outside the list are rejected; the arithmetic is ours
  5. deterministic invariants check the result ("a share cannot exceed 1")
  6. an optional outside oracle checks it against a number the loop never saw
  7. the neighbouring questions are replayed — a new probe must not hijack them
  8. only then is a derived.json entry written

`--dry-run` stops after step 6 and prints the whole trail.
"""
import json
import os
import sys


def _neighbour_questions(inst, question):
    """Questions that must NOT change route when a new metric is installed.

    The neighbours matter more than the target. An auto-generated probe hijacked
    its sibling once already: its trigger list contained the original's tokens
    plus extras, and trigger breadth decided the tie.
    """
    from ..derived import load as load_derived

    words = set(question.lower().split())
    out = []
    for meta in inst.router.metas:
        entry = " ".join([meta.name, meta.description or "",
                          " ".join(meta.triggers or [])]).lower()
        if len(words & set(entry.split())) >= 2:
            # ask with the probe's OWN longest trigger: closest to how users
            # reach it, and the sharpest test of a hijack
            trig = max(meta.triggers or [meta.name], key=len)
            out.append(trig)
    for m in load_derived(os.path.join(inst.state_dir, "derived.json")):
        if m.triggers:
            out.append(m.triggers[0])
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


def run(a):
    from ._common import load_instance
    from ..derived import load as load_derived
    from ..menu_adapter import observe, read_menu
    from ..reasoning import (MapperError, OllamaMapper, compare_routes,
                             install, needs_escalation, propose,
                             snapshot_routes)

    inst = load_instance(a.instance_dir)
    question = " ".join(a.question)
    instance_dir = inst.state_dir

    # ── 1. the deterministic gate ──────────────────────────────────────────
    env = None
    try:
        env = inst.ask(question, use_cache=False)
    except Exception as e:                     # NoResults and friends
        env = getattr(e, "envelope", None) or {}
    served = env.get("probe")
    entry = next((e for e in read_menu(instance_dir) if e["name"] == served),
                 None)
    probe_text = None
    if entry:
        probe_text = " ".join([entry.get("name", ""),
                               entry.get("description") or "",
                               " ".join(entry.get("triggers") or [])])
    qualifier = needs_escalation(question, served, probe_text)
    print(f"question  : {question}")
    print(f"served by : {served or 'nothing (refused)'}")
    print(f"gate      : {'ESCALATE — the question asks for a ' + repr(qualifier) + ' and the probe cannot produce one' if qualifier else 'do not escalate'}")
    if qualifier is None and not a.force:
        print("\nnothing to do: the routing layer already answered this shape "
              "of question.\n(use --force to escalate anyway)")
        return 0

    # ── 2. the closed inventory ────────────────────────────────────────────
    print("\nobserving every probe to build the closed inventory ...")
    observations = observe(instance_dir)
    print(f"  {len(observations)} probes returned a result")
    if not observations:
        print("error: no probe returned anything; cannot build an inventory")
        return 2

    # ── 3-6. map, compute, check ───────────────────────────────────────────
    mapper = OllamaMapper(model=a.model, url=a.ollama, timeout=a.timeout)
    if not mapper.available():
        print(f"\nerror: Ollama is not reachable at {a.ollama}, or the model "
              f"{a.model!r} is not pulled.\n"
              f"       try:  ollama pull {a.model}\n"
              f"       or:   --model <another model>", file=sys.stderr)
        return 3
    print(f"\nmapping with {mapper.model} (this takes ~30s x2 for the "
          f"stability check) ...")

    try:
        p = propose(question, served, observations, mapper,
                    oracle=a.oracle, scale=a.scale,
                    check_stability=not a.no_stability_check)
    except MapperError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    print(f"\n── the model's pick (latency {p.latency_s:.1f}s per call) ──")
    if p.pick:
        print(f"  numerator   : {p.pick['numerator']}")
        print(f"  denominator : {p.pick['denominator']}")
        print(f"  why         : {p.pick['why']}")
    else:
        print("  (no usable pick)")

    print("\n── checks ──")
    for c in p.checks:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[c["ok"]]
        print(f"  [{mark}] {c['name']}: {c['detail']}")

    if p.value:
        v = p.value
        print(f"\n── computed HERE, not by the model ──")
        print(f"  numerator   = {v['numerator']['value']}  ({v['numerator']['id']})")
        print(f"  denominator = {v['denominator']['value']}  ({v['denominator']['id']})")
        print(f"  result      = {v['value']} {v['unit']}")

    if not p.ok:
        print("\n✗ NOT INSTALLED")
        for r in p.reasons:
            print(f"  - {r}")
        return 4

    metric = p.metric
    print(f"\n── proposed metric ──")
    print(f"  name     : {metric.name}")
    print(f"  triggers : {metric.triggers}")
    print(f"  formula  : {metric.numerator} / {metric.denominator}")

    if a.dry_run:
        print("\n(dry run — nothing written)")
        print(json.dumps(p.to_json(), indent=2, ensure_ascii=False))
        return 0

    # ── 7. snapshot BEFORE, install, snapshot AFTER, compare ───────────────
    # The baseline is this router's OWN behaviour one moment ago. Comparing
    # against another engine's answers would report its pre-existing
    # disagreements as damage done by the new metric — the first run of this
    # loop reported 16 "regressions", all of which predated the change.
    neighbours = _neighbour_questions(inst, question)
    print(f"\n── backtest: {len(neighbours)} neighbouring questions ──")
    before = snapshot_routes(neighbours, lambda q: inst.ask(q, use_cache=False))
    refusals = sum(1 for v in before.values() if v is None)
    print(f"  baseline: captured {len(before)} routes "
          f"({refusals} of them already refusing)")

    # ── 8. install ─────────────────────────────────────────────────────────
    path = install(metric, instance_dir)
    print(f"\n✓ INSTALLED -> {path}")

    # ── 9. diff the routes and prove closure ───────────────────────────────
    from ._common import load_instance as reload_instance
    fresh = reload_instance(instance_dir)
    after = snapshot_routes(neighbours, lambda q: fresh.ask(q, use_cache=False))
    diff = compare_routes(before, after)
    for r in diff["regressions"][:10]:
        print(f"  [CHANGED] {r['question']!r}: {r['before']} -> {r['after']}")
    print(f"  unchanged: {diff['unchanged']}/{diff['total']}")

    try:
        env_after = fresh.ask(question, use_cache=False)
    except Exception as e:
        env_after = getattr(e, "envelope", None) or {}
    got = env_after.get("probe")

    print(f"\n── closure ──")
    print(f"  the target question now routes to : {got}")
    print(f"  mode                             : {env_after.get('mode')}")

    if diff["changed"]:
        print(f"\n✗ {diff['changed']} neighbouring question(s) changed route. "
              f"The metric is installed but it hijacked a sibling — inspect the "
              f"triggers in {path}.")
        return 4
    if got == metric.name:
        print("\n✓ LOOP CLOSED — the question is deterministic now, the model "
              "is not involved, and nothing else moved.")
        return 0
    print(f"\n✗ the target question did not route to {metric.name!r}.")
    return 5
