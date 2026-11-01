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
