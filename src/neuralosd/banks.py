"""Behavioural banks — the golden (positive) and traps (refusal) files.

Auto-trap synthesis (v2.1.0): when a probe is registered, its MISUSE trap is
appended to traps.json at birth. The trap question is
"<first trigger> <probe name>" — the probe's RAW name ("refund_payment") is
a token no trigger contains, so a conforming router refuses it
(no_probe_matches) while a router that drifts answers it confidently. That
asymmetry is what makes the trap a regression guard.

One honest exception: the synthesized question is only a valid refusal guard
when the raw name carries a token the probe's own vocabulary cannot know. A
probe named `breakdown` (a real word its triggers already cover) would get a
trap the conforming router ANSWERS — a bank that fails out of the box, the
exact failure `test_release_150` forbids. For such probes synthesis is
skipped and None is returned; the probe is still banked the moment a human
writes its real misuse trap.

Golden items are NEVER auto-added: a golden case asserts a verified correct
answer and needs human verification.

Gated by NEURALOSD_AUTO_TRAPS (default on; "0"/"false"/"no"/"off" disables).
"""
import json
import os
import time

from .router import tokens

_TRAP_COMMENT = ("Refusal bank. A trap PASSES only when the router refuses. "
                 "A confident answer to a trap is the worst failure this "
                 "system has. Add one trap per capability you deliberately "
                 "did NOT build.")


def auto_traps_enabled() -> bool:
    raw = os.environ.get("NEURALOSD_AUTO_TRAPS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _trap_is_a_valid_refusal_guard(probe_meta) -> bool:
    """The trap question embeds the probe's raw name. It only guards if that
    raw token is FOREIGN to the probe's own vocabulary (triggers + the
    human-readable name). `refund_payment` is foreign to ["refund payment"];
    `breakdown` is not foreign to ["breakdown", ...]."""
    foreign = tokens(probe_meta.name) - tokens(probe_meta.name.replace(
        "_", " "))
    for t in probe_meta.triggers or []:
        foreign -= tokens(t)
    return bool(foreign)


def synthesize_trap_for(probe_meta, traps_path):
    """Append probe_meta's misuse trap to the traps.json bank at traps_path.

    Returns the synthesized item (whether newly written or already present —
    the call is idempotent, deduped by q), or None when the gate is off or
    the probe cannot carry a valid auto trap.
    """
    if not auto_traps_enabled():
        return None
    if not _trap_is_a_valid_refusal_guard(probe_meta):
        return None
    first = (probe_meta.triggers or [""])[0]
    q = f"{first} {probe_meta.name}".strip()
    item = {"q": q, "expect_refusal": True,
            "note": f"auto-synthesized misuse trap for {probe_meta.name}"}
    path = os.fspath(traps_path)
    bank = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            bank = json.load(fh) or {}
    items = bank.setdefault("items", [])
    if any(i.get("q") == q for i in items):
        return item
    items.append(item)
    bank.setdefault("_comment", _TRAP_COMMENT)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return item


# ── v2.3.0: negative-feedback ingestion + oracle provenance ──────────
# ── F3: negative-feedback ingestion ────────────────────────────────────────

def _candidates_path(instance_dir):
    return os.path.join(instance_dir, "candidates.json")


def _load_candidates(instance_dir):
    path = _candidates_path(instance_dir)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"golden_candidates": [], "trap_candidates": []}


def _save_candidates(instance_dir, cand):
    with open(_candidates_path(instance_dir), "w", encoding="utf-8") as fh:
        json.dump(cand, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def ingest_feedback(instance_dir, question, verdict, correction=None,
                    by=None, ts=None):
    """A rejected answer becomes a bank CANDIDATE with the user's correction
    attached. verdict 'bad' (wrong ANSWER) -> golden candidate awaiting a
    verified oracle — it is never a trap, because the question itself is
    legitimate. verdict 'invalid' (the question is out of scope) -> trap
    candidate, promoted only once the refusal is PROVEN at apply time.
    """
    cand = _load_candidates(instance_dir)
    ts = ts if ts is not None else time.time()
    prov = {"source": "user_feedback", "by": by, "ts": ts,
            "verdict": verdict, "original_question": question}
    if verdict == "invalid":
        entry = {"kind": "trap_candidate",
                 "q": question, "expect_refusal": True, "provenance": prov}
        cand["trap_candidates"].append({"q": question,
                                        "expect_refusal": True,
                                        "provenance": prov})
    else:
        q = (correction or question).strip()
        if correction:
            prov["correction"] = correction
        entry = {"kind": "golden_candidate", "q": q, "verified": False,
                 "provenance": prov}
        cand["golden_candidates"].append({
            "q": q, "verified": False, "provenance": prov})
    _save_candidates(instance_dir, cand)
    return entry


def apply_feedback(instance_dir):
    """Promote candidates into the banks — each with PROOF:
      trap candidate -> traps.json only if the router already refuses it
                       (a trap the instance answers would fail the bank)
      golden candidate -> golden.json with routing proven live; the ORACLE
                       stays flagged unverified until a human confirms it
    """
    from ._cmd._common import load_instance
    cand = _load_candidates(instance_dir)
    summary = {"promoted_traps": 0, "promoted_golden": 0, "held": 0}

    def _write_json(path, blob):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(blob, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    inst = load_instance(instance_dir)
    for t in cand.get("trap_candidates", []):
        if t.get("applied"):
            continue
        try:
            env = inst.ask(t["q"], use_cache=False)
        except Exception:
            env = {}
        answered = env.get("results") not in (None, [], {})
        if answered:
            t["note"] = "currently answered — fix the probe or keep holding"
            summary["held"] += 1
            continue
        traps_path = os.path.join(instance_dir, "traps.json")
        bank = json.loads(open(traps_path, encoding="utf-8").read()) \
            if os.path.isfile(traps_path) else {"items": []}
        item = {"q": t["q"], "expect_refusal": True,
                "note": "promoted from user feedback (refusal proven at "
                        "apply time)",
                "provenance": t.get("provenance")}
        bank["items"].append(item)
        _write_json(traps_path, bank)
        t["applied"] = True
        summary["promoted_traps"] += 1

    for g in cand.get("golden_candidates", []):
        if g.get("applied"):
            continue
        try:
            env = inst.ask(g["q"], use_cache=False)
        except Exception:
            env = {}
        probe = env.get("probe")
        answered = env.get("results") not in (None, [], {})
        if not (probe and answered):
            g["note"] = "does not route yet — build the capability first"
            summary["held"] += 1
            continue
        golden_path = os.path.join(instance_dir, "golden.json")
        bank = json.loads(open(golden_path, encoding="utf-8").read()) \
            if os.path.isfile(golden_path) else {"items": []}
        bank["items"].append({
            "q": g["q"], "expect_probe": probe,
            "status": "candidate",
            "oracle": {"value": None,
                       "by": g.get("provenance", {}).get("by"),
                       "when": g.get("provenance", {}).get("ts"),
                       "source": "user-feedback",
                       "method": "UNVERIFIED — confirm the expected value "
                                 "before trusting this case"},
            "provenance": g.get("provenance")})
        _write_json(golden_path, bank)
        g["applied"] = True
        summary["promoted_golden"] += 1

    _save_candidates(instance_dir, cand)
    return summary



def record_golden(instance_dir, q, expect_probe=None, expect_refusal=False,
                  value=None, by=None, source=None, method=None, ts=None):
    """Append a golden item WITH its receipt: who verified the expected
    value, when, from what source, and how. An oracle value without these
    four fields is a number nobody can defend."""
    golden_path = os.path.join(instance_dir, "golden.json")
    bank = json.loads(open(golden_path, encoding="utf-8").read()) \
        if os.path.isfile(golden_path) else {"items": []}
    item = {"q": q, "oracle": {"value": value, "by": by,
                               "when": ts if ts is not None else time.time(),
                               "source": source, "method": method}}
    if expect_refusal:
        item["expect_refusal"] = True
    else:
        item["expect_probe"] = expect_probe
    bank["items"].append(item)
    with open(golden_path, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return item


def receipts(instance_dir):
    """The evidence chain, printable for an auditor: every golden case and
    its receipt — or an honest 'no receipt' where none exists."""
    golden_path = os.path.join(instance_dir, "golden.json")
    bank = json.loads(open(golden_path, encoding="utf-8").read()) \
        if os.path.isfile(golden_path) else {"items": []}
    lines = ["oracle receipts — the evidence chain for every golden case", ""]
    for it in bank.get("items", []):
        o = it.get("oracle")
        if not o:
            lines.append(f"  {it.get('q')!r} -> no receipt recorded")
            continue
        who = o.get("by") or "unknown"
        when = o.get("when")
        when_s = (time.strftime("%Y-%m-%d", time.gmtime(when))
                  if isinstance(when, (int, float)) else str(when or "?"))
        lines.append(f"  {it.get('q')!r}")
        lines.append(f"      value : {o.get('value')}")
        lines.append(f"      by    : {who}  on {when_s}")
        lines.append(f"      source: {o.get('source') or 'unspecified'} "
                     f"({o.get('method') or 'unspecified method'})")
    return "\n".join(lines)


