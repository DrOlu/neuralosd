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
    for raw in gaps:
        # Accept both the audit-dict shape and a bare question string —
        # callers mixing refusals_from_audit output with raw strings must
        # not crash the gossip loop.
        g = raw if isinstance(raw, dict) else {"question": raw}
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


def explain_fingerprints(fingerprints, gaps, threshold=0.5):
    """Explain OUR OWN fingerprints back to their tokens — the trusted-peer
    half of the gossip loop.

    A fingerprint is a hash: the RECEIVING edge can never invert it. Only
    the edge that BUILT the fingerprint holds the gap log it came from, so
    the loop closes when that edge — and only that edge, for peers it
    trusts — answers "yes, this hash is my (incident, attachment) gap".
    Tokens are returned, questions never are: the same privacy line the
    export draws, honoured in the other direction.

    Unknown hashes come back with ``known: False`` and an empty token list
    — an honest "not mine" rather than a guess.
    """
    index = {}
    norm = [{"question": g} if isinstance(g, str) else g for g in gaps or []]
    for c in cluster_gaps(norm, threshold):
        core = _core_tokens(c.get("questions") or [])
        if len(core) < 2:
            continue
        for fp in _pair_fps(core):
            index.setdefault(fp, set()).update(core)
    out = []
    for fp in fingerprints or []:
        fp = fp.get("fp") if isinstance(fp, dict) else fp
        toks = sorted(index.get(fp, ()))
        out.append({"fp": fp, "tokens": toks, "known": bool(toks)})
    return out


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
    from .instance import selftest_ledger
    from .router import audit_source
    inst = load_instance(instance_dir)
    try:
        # birth-trap proof is SELF-TEST traffic (v2.9.6): it replays a
        # synthesized trap through the live router and must not be counted
        # as a user refusal in the SLO ledger
        with selftest_ledger(inst), audit_source("trap"):
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


def _write_mothballed(instance_dir, blob):
    """Atomic write of the mothballed marker (tmp + fsync + replace).

    A crash mid-write used to truncate mothballed.json; with the tolerant
    reader that would silently REINSTATE every demoted probe. The replace
    makes the reader see either the old file or the new one, never a torn
    one.
    """
    path = _mothball_path(instance_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def mothballed_names(instance_dir):
    """Demoted probe names, tolerant of a corrupt/absent marker.

    Same contract as `_cmd._common._mothballed_names`: fleet/SLO reporting
    must not crash because one instance's hygiene file is damaged. Writes
    go through `_write_mothballed`, so corruption here is external.
    """
    path = _mothball_path(instance_dir)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (ValueError, OSError):
        return []
    names = blob.get("names", []) if isinstance(blob, dict) else []
    return [n for n in names if isinstance(n, str)]


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
        _write_mothballed(instance_dir,
                          {"names": sorted(current | set(mothballed)),
                           "ts": time.time()})
    return report


def reinstate(instance_dir, name):
    path = _mothball_path(instance_dir)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (ValueError, OSError):
        return False
    if not isinstance(blob, dict) or name not in blob.get("names", []):
        return False
    blob["names"] = [n for n in blob["names"] if n != name]
    _write_mothballed(instance_dir, blob)
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


# ── v2.4.0: pinning, SLO, fleet, badge, synonyms ───────────────────────────

def _snapshot_label(label):
    """A label becomes a FILENAME. Reject anything that could leave the
    snapshots directory or smuggle control characters into it."""
    if (not isinstance(label, str) or not label or len(label) > 80
            or label in (".", "..")
            or any(c in label for c in "/\\")
            or any(ord(c) < 32 for c in label)):
        raise SystemExit(f"error: invalid snapshot label {label!r}")
    return label


def pin_snapshot(instance_dir, label):
    """Freeze every probe's current observation under a label, with the
    content hash. `ask --as-of <label>` answers from the pin — reproducible
    evidence, even after the source changes."""
    label = _snapshot_label(label)
    from .menu_adapter import observe
    obs = observe(instance_dir)
    blob = {"label": label, "ts": time.time(), "observations": obs}
    blob["hash"] = hashlib.sha256(
        json.dumps(obs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    d = os.path.join(instance_dir, "snapshots")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{label}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2, default=str)
        fh.write("\n")
    return {"label": label, "hash": blob["hash"], "path": path}


def ask_as_of(instance_dir, question, label):
    """Answer from a PINNED snapshot: derived metrics evaluate against the
    frozen observations. The envelope carries the pin's label + hash — the
    answer is reproducible evidence, not a vibe."""
    label = _snapshot_label(label)
    from ._cmd._common import load_instance
    path = os.path.join(instance_dir, "snapshots", f"{label}.json")
    if not os.path.isfile(path):
        raise SystemExit(f"error: no snapshot labeled {label!r} in "
                         f"{instance_dir} (neuralosd snapshot --label {label})")
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    inst = load_instance(instance_dir,
                         observations_override=blob["observations"])
    try:
        env = inst.ask(question, use_cache=False)
    except Exception as e:
        env = getattr(e, "envelope", None) or {}
    env["snapshot"] = {"label": label, "hash": blob.get("hash"),
                       "ts": blob.get("ts")}
    env["pinned"] = True
    return env


def _tail_lines(path, cap):
    """Read a file's last `cap` lines without loading it whole (a multi-GB
    audit log must not be parsed into memory to compute a rate)."""
    import collections
    with open(path, encoding="utf-8", errors="replace") as fh:
        return list(collections.deque(fh, maxlen=cap))


def _envi(name, default):
    """Integer from the environment, or the default (the _envf idiom)."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Below this many asks in the window the rate is not a signal: one refusal is
# 100 %. Undersized samples still report their rate, but cannot breach.
MIN_ASKS_FOR_SLO = 10


def refusal_rate(instance_dir, window_hours=24, max_refusal_rate=0.4,
                 tail_lines=200000, sources=None, min_asks=None):
    """The refusal rate over the window IS the menu-gap signal. A breach is
    not a failure of the router — it is a capability gap, quantified.

    A breach needs a SAMPLE. `min_asks` (default ``MIN_ASKS_FOR_SLO`` = 10,
    overridable with ``--min-asks`` or ``NEURALOSD_MIN_ASKS``) is the
    smallest window that can be judged: below it ``insufficient_data`` is
    True and ``breach`` stays False, because one refusal in a one-ask
    window is a 100 % rate and a statistical artefact, not a menu failure.
    The rate itself is never hidden — undersized samples still report
    ``refusal_rate`` and their ask count, so an operator can see the
    traffic that is too thin to judge.

    Reads at most `tail_lines` lines (bounded, newest-first by position):
    a rate computed from a bounded tail is honest as long as the cap is
    larger than the window's ask volume — and multi-GB audits stay fast.

    Row contract (v2.9.6): every producer MUST emit a numeric `ts` — the
    fleet SLO is computed from this file, and a row without one cannot be
    dated, so it is counted in NEITHER asks nor refusals. Rows without a
    numeric ts are not silently dropped: they are reported back as
    `rows_missing_ts` so a producer bug shows up in monitoring instead of
    quietly shrinking the denominator. Legacy timestamps are never invented
    for such rows. `mode` is emitted by every producer too; rows missing it
    are schema defects but do not affect the math (refusal detection keys on
    empty results, which predates the schema pin and is kept for
    compatibility).

    `sources` filters by row provenance (the v2.9.6 `source` stamp): pass
    ("user",) to measure HUMAN traffic only — self-test replays stamp
    themselves golden/trap, health ticks watchdog, HTTP serves serve. Rows
    with no stamp (all pre-2.9.6 ledgers) count as "user". The DEFAULT is
    None: unfiltered, every row counts — exactly the pre-2.9.6 denominator,
    so dashboards do not move under a patch release.
    """
    if min_asks is None:
        min_asks = _envi("NEURALOSD_MIN_ASKS", MIN_ASKS_FOR_SLO)
    min_asks = int(min_asks)
    audit_path = os.path.join(instance_dir, "ask_audit.jsonl")
    cutoff = time.time() - window_hours * 3600.0
    asks = refused = missing_ts = 0
    reasons = {}
    if os.path.isfile(audit_path):
        for line in _tail_lines(audit_path, tail_lines):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts")
            if not isinstance(ts, (int, float)):
                # cannot be dated → cannot enter the window. COUNTED, not
                # hidden: a producer emitting ts-less rows is a defect the
                # dashboard must see.
                missing_ts += 1
                continue
            if ts < cutoff:
                continue
            if sources is not None:
                # legacy rows predate the stamp; they were all human traffic
                if r.get("source", "user") not in sources:
                    continue
            asks += 1
            if r.get("results") in (None, [], {}):
                refused += 1
                why = r.get("refusal_reason") or "refused"
                reasons[why] = reasons.get(why, 0) + 1
    rate = (refused / asks) if asks else 0.0
    undersized = asks < min_asks
    return {"asks": asks, "refused": refused,
            "refusal_rate": round(rate, 4),
            "slo": max_refusal_rate,
            # A breach needs a sample. breach is what the dashboard and the
            # exit codes read, so a 1-ask window must not set it.
            "breach": (not undersized) and rate > max_refusal_rate,
            "insufficient_data": undersized,
            "min_asks": min_asks,
            "rows_missing_ts": missing_ts,
            "top_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:5]}


def fleet_report(dirs, window_hours=24, max_refusal_rate=0.4, min_asks=None):
    """Per-instance health across a fleet: menu size, activity, refusal
    rate vs SLO. The dashboard (and the exit code) read this.

    Instances whose window is smaller than `min_asks` are neither healthy
    nor breaching: they are counted in their own `undersized` bucket and
    rendered n/a, because calling a 1-ask window "ok" is as wrong as
    paging on it.
    """
    instances = []
    breached = healthy = undersized = 0
    for d in dirs:
        d = os.path.abspath(d)
        entry = {"instance": os.path.basename(d.rstrip("/")), "path": d}
        try:
            from ._cmd._common import load_instance
            entry["probes"] = len(load_instance(d).router.metas)
        except (Exception, SystemExit) as e:
            # load_instance signals a missing instance with SystemExit (the
            # CLI-error idiom); a report over a mixed fleet must skip such a
            # dir, not die on it. Either way the refusal-rate row still lands.
            entry["probes"] = None
            entry["error"] = str(e)[:120]
        stats = refusal_rate(d, window_hours=window_hours,
                             max_refusal_rate=max_refusal_rate,
                             min_asks=min_asks)
        entry.update({k: stats[k] for k in
                      ("asks", "refused", "refusal_rate", "breach",
                       "insufficient_data", "rows_missing_ts")})
        if stats["insufficient_data"]:
            undersized += 1
        elif stats["breach"]:
            breached += 1
        else:
            healthy += 1
        entry["mothballed"] = len(mothballed_names(d))
        instances.append(entry)
    return {"instances": instances, "breached": breached, "healthy": healthy,
            "undersized": undersized, "slo": max_refusal_rate}


def write_badge(instance_dir, out_path, actions_out=None):
    """Run the golden bank and emit a shields.io endpoint JSON — trust
    signaling that lives OUTSIDE the box, in the README."""
    from ._cmd._common import load_instance
    inst = load_instance(instance_dir)
    golden_path = os.path.join(instance_dir, "golden.json")
    if os.path.isfile(golden_path):
        with open(golden_path, encoding="utf-8") as fh:
            bank = json.load(fh)
    else:
        bank = {"items": []}
    summary = inst.golden_run(bank)
    total = summary["total"]
    passed = summary["correct"] + summary["refused_ok"]
    ok = summary["wrong"] == 0 and summary["answered_traps"] == 0
    badge = {"schemaVersion": 1,
             "label": "golden bank",
             "message": f"{passed}/{total} passing" if total else "empty",
             "color": "brightgreen" if ok and total else
                      ("yellow" if total == 0 else "red")}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(badge, fh, indent=2)
        fh.write("\n")
    if actions_out:
        with open(actions_out, "w", encoding="utf-8") as fh:
            fh.write(
                "name: golden bank\n"
                "on: [push, workflow_dispatch]\n"
                "jobs:\n"
                "  golden:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - uses: actions/setup-python@v5\n"
                "        with: {python-version: \"3.12\"}\n"
                "      - run: pip install 'neuralosd[data]'\n"
                "      - run: neuralosd golden --dir .\n")
    return badge


def propose_synonyms(instance_dir, audit_path=None, min_cluster=2):
    """Gap clusters -> PROPOSED triggers for the probe that most plausibly
    owns the pattern. Human-approved by default (approved: False); applying
    is a separate, explicit step. Refusal rate drops WITHOUT loosening the
    gate — the gate gets better vocabulary instead."""
    from ._cmd._common import load_instance
    audit_path = audit_path or os.path.join(instance_dir,
                                            "ask_audit.jsonl")
    gaps = []
    if os.path.isfile(audit_path):
        with open(audit_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("results") in (None, [], {}):
                    gaps.append({"question": r.get("question")})
    clusters = [c for c in cluster_gaps(gaps) if c["size"] >= min_cluster]
    inst = load_instance(instance_dir)

    def _tokens(text):
        from .router import tokens as _t
        return _t(text or "")

    proposals = []
    for c in clusters:
        common = [t for t in c.get("common_terms", []) if t]
        if len(common) < 2:
            continue
        cs = set(common)
        # the audit's OWN score vectors are the strongest ownership evidence:
        # a refusal whose scores nearly crowned a probe names it.
        votes = {}
        for g in gaps:
            if g.get("question") in c.get("questions", []):
                for probe, _s in (g.get("scores") or {}).items():
                    votes[probe] = votes.get(probe, 0) + 3
        best, best_score = None, 0
        for meta in inst.router.metas:
            score = votes.get(meta.name, 0)
            score += 5 * len(cs & set(
                v for spec in (meta.args or {}).values()
                for v in spec.get("values") or []))
            score += 2 * len(cs & _tokens((meta.name or "").replace("_", " ")))
            score += len(cs & _tokens(meta.description or ""))
            score += len(cs & _tokens(" ".join(meta.triggers or [])))
            if score > best_score:
                best, best_score = meta, score
        if best is None or best_score <= 0:
            continue                       # no plausible owner: a capability
            # gap, not a synonym gap — stays in `gaps`, not here
        trigger = " ".join(sorted(common)[:2])
        if trigger in (best.triggers or []):
            continue                       # already covered — not a gap
        proposals.append({"probe": best.name, "trigger": trigger,
                          "cluster_size": c["size"],
                          "questions": c["questions"][:5],
                          "approved": False})
    blob = {"instance": instance_dir, "ts": time.time(),
            "proposals": proposals}
    path = os.path.join(instance_dir, "synonym_proposals.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return blob


def apply_synonyms(instance_dir, proposals_path):
    """Apply APPROVED synonym proposals. Derived metrics (the loop's own
    probes) are extended in derived.json — the menu's code is not edited by
    a machine. probes.py probes get an exact EDIT SUGGESTION instead: a
    human makes that change, because generated code is never silently
    rewritten."""
    from .derived import (load as load_derived, save as save_derived)
    with open(proposals_path, encoding="utf-8") as fh:
        blob = json.load(fh)
    metrics = load_derived(os.path.join(instance_dir, "derived.json"))
    by_name = {m.name: m for m in metrics}
    applied = suggested = 0
    for p in blob.get("proposals", []):
        if not p.get("approved"):
            continue
        m = by_name.get(p["probe"])
        if m is not None:
            if p["trigger"] not in (m.triggers or []):
                m.triggers = list(m.triggers or []) + [p["trigger"]]
                applied += 1
        else:
            p["suggestion"] = (
                f"human action: add {p['trigger']!r} to the triggers of "
                f"@{p['probe']} in probes.py")
            suggested += 1
    save_derived(os.path.join(instance_dir, "derived.json"), metrics)
    blob["applied"] = applied
    blob["suggested"] = suggested
    with open(proposals_path, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return {"applied": applied, "suggested": suggested}


# ── v2.8.1: federation gossip — the protocol wiring ────────────────────────

_MAX_FP_COUNT = 1000000
_MAX_BLOB_FPS = 200
_GOSSIP_SKILL = "neuralosd.gossip"


def _validate_fingerprint_blob(blob):
    """A gossip blob must be well-formed BEFORE it is trusted or sent:
    version 1, each fingerprint 16 hex chars, each count a sane integer.
    A misbehaving peer cannot poison the gaps report."""
    import re
    if not isinstance(blob, dict) or blob.get("version") != 1:
        raise SystemExit("error: gossip blob must be {\"version\": 1, ...}")
    fps = blob.get("fingerprints")
    if not isinstance(fps, list) or not fps:
        raise SystemExit("error: gossip blob has no fingerprints")
    if len(fps) > _MAX_BLOB_FPS:
        raise SystemExit(f"error: gossip blob exceeds "
                         f"{_MAX_BLOB_FPS} fingerprints")
    for f in fps:
        fp, count = f.get("fp", ""), f.get("count")
        if not re.fullmatch(r"[0-9a-f]{16}", str(fp)):
            raise SystemExit(f"error: malformed fingerprint {fp!r} "
                             f"(expected 16 hex chars)")
        if not isinstance(count, int) or not (1 <= count <= _MAX_FP_COUNT):
            raise SystemExit(f"error: fingerprint {fp!r} count out of "
                             f"range: {count!r}")
    return blob


def gossip_push(transport, blob, peers, origin=""):
    """Publish the local fingerprint blob to trusted peers via the gateway
    mailbox (fire-and-forget, at-least-once). The blob carries hashes and
    counts ONLY — the questions never leave the edge."""
    _validate_fingerprint_blob(blob)
    sent, failed = [], []
    blob = dict(blob, origin=origin)
    for peer in peers:
        try:
            transport.mailbox(target=peer, skill=_GOSSIP_SKILL,
                              input_data=blob)
            sent.append(peer)
        except Exception as e:                             # noqa: BLE001
            failed.append({"peer": peer, "error": str(e)[:120]})
    return {"pushed": sent, "failed": failed}


def gossip_collect(inbox_dir):
    """Read peer blobs from the inbox directory (the gateway drops them
    there), validate each, and build a per-peer gossip state. Malformed
    files are skipped; counts are capped at arrival — a poisoned peer is
    bounded, never trusted."""
    import uuid
    state = {"peers": {}, "ts": time.time()}
    if not os.path.isdir(inbox_dir):
        return state
    # also purge a stale self-ingested entry from a previous version
    state["peers"].pop("gossip_state", None)
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".json") or name == "gossip_state.json":
            continue                       # never self-ingest our own state
        path = os.path.join(inbox_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (ValueError, OSError):
            continue
        peer = name[:-5]
        peer_state = state["peers"].setdefault(peer, {"fingerprints": [],
                                                      "ts": time.time()})
        seen = {f["fp"]: f["count"] for f in peer_state["fingerprints"]}
        for f in blob.get("fingerprints", []):
            fp = str(f.get("fp", ""))
            count = f.get("count")
            if not re.fullmatch(r"[0-9a-f]{16}", fp):
                continue
            try:
                count = max(1, min(int(count), _MAX_FP_COUNT))
            except (TypeError, ValueError):
                continue
            seen[fp] = max(seen.get(fp, 0), count)
        peer_state["fingerprints"] = [{"fp": fp, "count": n}
                                      for fp, n in sorted(seen.items())]
    with open(os.path.join(inbox_dir, "gossip_state.json"), "w",
              encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    return state


def gossip_peer_counts(inbox_dir):
    """fp -> best peer count across all collected peers (for mesh_count)."""
    state = gossip_collect(inbox_dir)
    best = {}
    for peer in state["peers"].values():
        for f in peer["fingerprints"]:
            best[f["fp"]] = max(best.get(f["fp"], 0), f["count"])
    return best
