"""The loop machinery — v2.3.0's ten improvements, one module.

Every function here is honest by construction:

  clustering     — refusal PATTERNS, not anecdotes (F2)
  fingerprints   — token-PAIR hashes only; a question can never be
                   reconstructed from an exported fingerprint (F10)
  feedback       — a thumbs-down becomes a CANDIDATE with provenance;
                   promotion requires proof (routing or refusal) (F3)
  birth traps    — every reason-installed probe proves its refusal before
                   the loop closes (F1)
  provenance     — golden receipts: who, when, from what source (F5)
  transfer       — schema-similar instances RECONFIRM shared probes and
                   PROPOSE missing ones; proposals never auto-install (F4)
  shadow         — generated probes do not serve traffic until they agree
                   with their oracle (F6)
  mothball       — stale probes are demoted, kept, and reinstatable (F8)
  drift          — a schema change flags the probes it breaks BEFORE
                   runtime (F9)
"""
import hashlib
import json
import math
import os
import re
import time

from .banks import (apply_feedback, ingest_feedback,
                    record_golden, receipts)  # noqa: F401
from .router import tokens

# ── F2: refusal clustering ─────────────────────────────────────────────────


def _q_tokens(question):
    return tokens(question or "")


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_gaps(gaps, threshold=0.5):
    """Greedy single-link clustering over domain-token Jaccard similarity.

    A cluster reports its size, member questions, and the tokens common to
    EVERY member — the pattern, not any one phrasing. Singleton clusters are
    kept as singletons: a gap asked once is not inflated to look like a
    trend.
    """
    clusters = []          # each: {"tokens": set, "questions": [str]}
    for g in gaps:
        qt = _q_tokens(g.get("question"))
        placed = False
        for c in clusters:
            if _jaccard(qt, c["tokens"]) >= threshold:
                c["questions"].append(g.get("question"))
                c["tokens"] |= qt
                placed = True
                break
        if not placed:
            clusters.append({"tokens": set(qt), "questions": [g.get("question")]})

    out = []
    for c in clusters:
        qs = [q for q in c["questions"] if q]
        n = len(qs)
        if n == 0:
            continue
        common = set.intersection(*[_q_tokens(q) for q in qs]) if qs else set()
        noun = "query" if n == 1 else "queries"
        phrasing = "phrasing" if n == 1 else "phrasings"
        out.append({
            "size": n,
            "questions": qs[:10],
            "common_terms": sorted(common),
            "cost": f"this gap cost you {n} refused {noun} "
                    f"across {n} {phrasing}",
        })
    out.sort(key=lambda c: -c["size"])
    return out


# ── F10: federated gap fingerprints ────────────────────────────────────────

def _core_tokens(questions):
    """Tokens appearing in at least half the cluster's questions."""
    n = len(questions)
    if n == 0:
        return set()
    counts = {}
    for q in questions:
        for t in _q_tokens(q):
            counts[t] = counts.get(t, 0) + 1
    return {t for t, c in counts.items() if c >= math.ceil(n / 2)}


def _pair_fps(core, cap=6):
    """Skip-bigram fingerprints: every pair of core tokens, hashed.

    Pair-level matching is deliberate — two edges phrase the same pattern
    differently ("shipping cost per unit" vs "cost of shipping per unit
    shipped"), but both contain the pair (cost, shipping). One shared pair
    links the pattern; the raw tokens and the questions never leave the edge.
    """
    toks = sorted(core)
    fps = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            fps.append(hashlib.sha256(f"{toks[i]}|{toks[j]}".encode()
                                      ).hexdigest()[:16])
            if len(fps) >= cap:
                return fps
    return fps


def export_fingerprints(clusters):
    """The federatable payload: pattern hashes + counts. NO questions."""
    fingerprints = {}
    for c in clusters:
        core = _core_tokens(c.get("questions") or [])
        if len(core) < 2:
            continue                       # a one-token pattern matches noise
        for fp in _pair_fps(core):
            fingerprints[fp] = fingerprints.get(fp, 0) + c["size"]
    return {"version": 1,
            "ts": time.time(),
            "fingerprints": [{"fp": fp, "count": n}
                             for fp, n in sorted(fingerprints.items())]}


def ingest_fingerprints(clusters, peer_blob):
    """Merge a peer's fingerprints into local clusters as `mesh_count`:
    the LOCAL size plus the strongest matching peer count. Nothing about
    the peer's questions becomes visible — only that the pattern exists
    elsewhere, and how often."""
    peer = {f["fp"]: f["count"] for f in peer_blob.get("fingerprints", [])}
    out = []
    for c in clusters:
        c = dict(c)
        core = _core_tokens(c.get("questions") or [])
        best = 0
        for fp in _pair_fps(core):
            best = max(best, peer.get(fp, 0))
        if best:
            c["mesh_count"] = c["size"] + best
            c["mesh_note"] = (f"peers saw this pattern {best} more time(s) — "
                              f"{c['mesh_count']} asks total across the mesh")
        out.append(c)
    out.sort(key=lambda c: -(c.get("mesh_count") or c["size"]))
    return out


# ── F1: probe birth traps from the reason loop ─────────────────────────────

def close_probe_birth(metric, instance_dir):
    """Synthesize the new probe's misuse trap and PROVE the refusal with the
    live router. The receipt is written into the metric's provenance, so the
    install record shows the gap was closed at birth — not assumed."""
    from .banks import synthesize_trap_for
    from .derived import load as load_derived, save as save_derived

    summary = {"q": None, "synthesized": False, "refused": None,
               "answered_by": None}
    item = synthesize_trap_for(metric, os.path.join(instance_dir,
                                                    "traps.json"))
    if item is None:
        # the raw name cannot carry a valid auto-trap (it would be answered
        # by a conforming router) — record the skip honestly
        metric.provenance["birth_trap"] = dict(summary, skipped=True)
        _refresh_metric(metric, instance_dir)
        return summary
    summary["synthesized"] = True
    summary["q"] = item["q"]

    from ._cmd._common import load_instance
    inst = load_instance(instance_dir)
    try:
        env = inst.ask(item["q"], use_cache=False)
    except Exception as e:
        env = getattr(e, "envelope", None) or {}
    answered = env.get("results") not in (None, [], {})
    summary["refused"] = not answered
    summary["answered_by"] = env.get("probe") if answered else None

    metric.provenance["birth_trap"] = summary
    _refresh_metric(metric, instance_dir)
    return summary


def _refresh_metric(metric, instance_dir):
    from .derived import load as load_derived, save as save_derived
    path = os.path.join(instance_dir, "derived.json")
    metrics = load_derived(path)
    for i, m in enumerate(metrics):
        if m.name == metric.name:
            metrics[i] = metric
            break
    save_derived(path, metrics)


# ── F6: shadow mode ────────────────────────────────────────────────────────

def shadow_install(metric, instance_dir, days=14):
    """Register the metric as SHADOWED: present in derived.json, evaluated
    against its oracle, but excluded from routing until it earns promotion."""
    metric.shadow = {"since": time.time(), "days": days, "agreement": []}
    _refresh_metric(metric, instance_dir)


def shadow_report(instance_dir):
    from .derived import load as load_derived
    return {"shadowed": [m.name for m in load_derived(
        os.path.join(instance_dir, "derived.json")) if m.shadow]}


def shadow_evaluate(instance_dir, name):
    """Run the shadowed metric against FRESH observations and compare with
    its recorded oracle. Every run appends to the agreement log — the
    promotion decision reads the log, never a vibe."""
    from .derived import (evaluate, load as load_derived, save as save_derived)
    from .menu_adapter import observe
    metrics = load_derived(os.path.join(instance_dir, "derived.json"))
    m = next((m for m in metrics if m.name == name), None)
    if m is None or not m.shadow:
        raise SystemExit(f"error: {name!r} is not a shadowed probe")
    obs = observe(instance_dir)
    value = evaluate(m, obs)["value"]
    oracle = m.provenance.get("verified_against")
    agreed = (oracle is not None
              and abs(float(value) - float(oracle)) <= 1e-6)
    m.shadow["agreement"].append({"ts": time.time(), "agreed": agreed,
                                  "value": value, "oracle": oracle})
    save_derived(os.path.join(instance_dir, "derived.json"), metrics)
    return {"agreed": agreed, "value": value, "oracle": oracle}


def shadow_promote(instance_dir, name, min_agreement=0.9, min_runs=3,
                   max_age_days=14):
    """Promote a shadowed probe to routable — only when it has been shadowed
    long enough, evaluated often enough, and agrees with its oracle."""
    from .derived import (load as load_derived, save as save_derived)
    metrics = load_derived(os.path.join(instance_dir, "derived.json"))
    m = next((m for m in metrics if m.name == name), None)
    if m is None or not m.shadow:
        return False
    age_days = (time.time() - m.shadow.get("since", time.time())) / 86400.0
    if age_days < max_age_days:
        return False
    runs = m.shadow.get("agreement", [])
    if len(runs) < min_runs:
        return False
    if sum(1 for r in runs if r.get("agreed")) / len(runs) < min_agreement:
        return False
    m.shadow = None
    save_derived(os.path.join(instance_dir, "derived.json"), metrics)
    return True


# ── F4: schema-similarity probe transfer ───────────────────────────────────

def _schema_fingerprint(inst):
    """The instance's schema shape: every caged enum value on the menu."""
    vals = set()
    for meta in inst.router.metas:
        for spec in (meta.args or {}).values():
            for v in spec.get("values") or []:
                vals.add(str(v))
    return vals


def propose_transfer(from_dir, to_dir, min_sim=0.6, out_path=None):
    """Propose A's probes for schema-similar B.

    RECONFIRMED probes (both menus) get A's verified golden items translated
    with re-oracle flags. PROPOSED probes (A only) carry their definition.
    NOTHING is written into B except the proposal document — a proposal is
    a proposal.
    """
    from ._cmd._common import load_instance
    a = load_instance(from_dir)
    b = load_instance(to_dir)

    fa, fb = _schema_fingerprint(a), _schema_fingerprint(b)
    union = fa | fb
    sim = (len(fa & fb) / len(union)) if union else 0.0

    a_by = {m.name: m for m in a.router.metas}
    b_names = {m.name for m in b.router.metas}

    a_golden = {}
    gp = os.path.join(os.path.abspath(from_dir), "golden.json")
    if os.path.isfile(gp):
        with open(gp, encoding="utf-8") as fh:
            for it in json.load(fh).get("items", []):
                if it.get("expect_probe") and not it.get("expect_refusal"):
                    a_golden.setdefault(it["expect_probe"], []).append(it)

    from_name = os.path.basename(os.path.abspath(from_dir).rstrip("/"))
    proposed, reconfirmed = [], {}
    for name in sorted(a_by):
        meta = a_by[name]
        if name in b_names:
            golden = [_translate_golden(g, from_name)
                      for g in a_golden.get(name, [])]
            if golden:
                reconfirmed[name] = {"golden": golden}
            continue
        proposed.append({
            "probe": name,
            "description": meta.description,
            "triggers": list(meta.triggers or []),
            "args": {k: dict(v) for k, v in (meta.args or {}).items()},
            "golden": [_translate_golden(g, from_name)
                       for g in a_golden.get(name, [])]})

    proposal = {"from": from_name,
                "to": os.path.basename(os.path.abspath(to_dir).rstrip("/")),
                "similarity": round(sim, 4),
                "threshold": min_sim,
                "proposed": sim >= min_sim,
                "probes": proposed if sim >= min_sim else [],
                "reconfirmed": reconfirmed if sim >= min_sim else {}}
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(proposal, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    return proposal


def _translate_golden(item, from_name):
    g = dict(item)
    g["reoracle"] = True          # the value was proven on ANOTHER estate
    g["provenance"] = {"source": f"transfer:{from_name}",
                       "original": {k: item.get(k) for k in
                                    ("q", "expect_probe", "oracle")}}
    g.pop("oracle", None)
    return g


# ── F8: auto-deprecation / menu hygiene ────────────────────────────────────

def _mothball_path(instance_dir):
    return os.path.join(instance_dir, "mothballed.json")


def mothballed_names(instance_dir):
    path = _mothball_path(instance_dir)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return list(json.load(fh).get("names", []))


def mothball(instance_dir, age_days=180, apply=False):
    """Probes nobody has queried in `age_days` (or ever) are mothball
    candidates: kept on disk, demoted from the menu. At least ONE probe
    always stays routable — a mothballed-everything instance is a mute
    instance, and hygiene must never become muting."""
    from ._cmd._common import load_instance
    audit_path = os.path.join(instance_dir, "ask_audit.jsonl")
    last = {}
    if os.path.isfile(audit_path):
        with open(audit_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                p = r.get("probe")
                ts = r.get("ts")
                if p and isinstance(ts, (int, float)):
                    last[p] = max(last.get(p, 0), ts)
    # the CURRENT routable menu (probes.py instances have no menu json —
    # the router's metas are the truth)
    names = [m.name for m in load_instance(instance_dir).router.metas]
    cutoff = time.time() - age_days * 86400.0
    candidates = [n for n in names
                  if n not in last or last[n] < cutoff]
    # keep the most-recently-used candidate as the anchor
    ordered = sorted(candidates,
                     key=lambda n: -last.get(n, 0))
    mothballed = ordered[1:] if len(candidates) == len(names) else ordered
    report = {"candidates": candidates, "mothballed": mothballed,
              "kept": [n for n in names if n not in mothballed]}
    if apply and mothballed:
        current = set(mothballed_names(instance_dir))
        with open(_mothball_path(instance_dir), "w",
                  encoding="utf-8") as fh:
            json.dump({"names": sorted(current | set(mothballed)),
                       "ts": time.time()}, fh, indent=2)
            fh.write("\n")
    return report


def reinstate(instance_dir, name):
    path = _mothball_path(instance_dir)
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    if name not in blob.get("names", []):
        return False
    blob["names"] = [n for n in blob["names"] if n != name]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2)
        fh.write("\n")
    return True


# ── F9: schema-drift re-audit ──────────────────────────────────────────────

_NAME_CAGED = re.compile(r"^(distinct|total|average)_(.+)$")


def audit_drift(instance_dir, src):
    """Re-profile the source and flag every probe the NEW schema breaks:
    name-caged probes (total_<col> / distinct_<col> / average_<col>) whose
    column is gone, and enum-caged probes whose values no longer exist.
    This runs BEFORE the probes fail at runtime — drift is an audit
    trigger, not a 3am page."""
    from ._cmd.init import profile_any
    from ._cmd._common import load_instance

    profile = profile_any(src)
    cols = {c["name"]: c for c in profile.get("columns", [])}
    date_col = next((c.get("date_column") for c in cols.values()
                     if c.get("date_column")), None)

    inst = load_instance(instance_dir)
    stale, missing_cols = [], set()
    for meta in inst.router.metas:
        m = _NAME_CAGED.match(meta.name or "")
        if m and m.group(2) not in cols:
            stale.append({"probe": meta.name,
                          "missing_column": m.group(2)})
            missing_cols.add(m.group(2))
            continue
        caged = []
        for spec in (meta.args or {}).values():
            caged += [str(v) for v in spec.get("values") or []]
        if not caged:
            continue
        date_parts = {"year", "quarter", "month"} if date_col else set()
        gone = [v for v in caged
                if v not in cols and v not in date_parts]
        if gone:
            stale.append({"probe": meta.name, "missing_values": gone})
            missing_cols.update(gone)

    return {"source": src,
            "source_kind": profile.get("kind"),
            "columns_now": {n: c.get("type") for n, c in cols.items()},
            "stale_probes": stale,
            "missing_columns": sorted(missing_cols),
            "action": ("re-audit required — regenerate or retrigger the "
                       "flagged probes before they fail at runtime")
                       if stale else "clean"}
