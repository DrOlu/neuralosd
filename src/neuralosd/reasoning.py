"""The reasoning loop — escalate deterministically, map with a model, compute here.

The division of labour is the whole design, and it came out of a run that failed
three times before it worked:

    deterministic code decides WHEN to escalate
    the reasoning model decides WHAT to map
    our code computes the number and verifies it

Why the model is not allowed to judge: in that run a 7B model said "gap" and
then "ok" on *identical input*. A judge that flips is not a gate. Escalation is
therefore decided by `needs_escalation()` below, which contains no model call.

Why the model does not compute: it once proposed dividing two probe NAMES. Given
a closed list of observed quantities and no arithmetic to do, it cannot produce a
number at all — and anything it emits outside the list is a rejection.

What is NOT here: code generation. The model's output is two ids. The install is
a JSON spec interpreted by `derived.py`. Nothing a model emits is executed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .derived import (DerivedError, DerivedMetric, Quantity,
                      RATIO_QUALIFIERS, SHARE_PHRASES, check_expectation,
                      evaluate, expectation_for, inventory_from_observations,
                      resolve)

OLLAMA_DEFAULT = os.environ.get("NEURALOSD_OLLAMA", "http://127.0.0.1:11434")
MODEL_DEFAULT = os.environ.get("NEURALOSD_REASON_MODEL", "deepseek-r1:8b")


# ── 1. the deterministic gate ──────────────────────────────────────────────

def needs_escalation(question: str, served_probe: Optional[str],
                     probe_text: Optional[str] = None) -> Optional[str]:
    """Should this question go to a reasoning model? Deterministic. No model.

    True when the question asks for a SHAPE of answer — a ratio — that the probe
    which actually served it cannot produce. "what share of total revenue comes
    from the top 10 customers" was served by a probe that returns a list of
    customers: real data, wrong question.

    Coverage is tested on the qualifier's CONTENT WORDS, not on the literal
    phrase: a description reading "revenue share percentages" covers "share of",
    because the concept is present even though those two words never sit
    together.
    """
    q = (question or "").lower()
    asked = [t for t in RATIO_QUALIFIERS if t in q]
    if not asked:
        return None
    if not served_probe:
        return asked[0]
    from .router import tokens
    own = tokens(probe_text or served_probe)
    for t in asked:
        words = tokens(t)
        if words and not (words & own):
            return t
    return None


# ── 2. the mapper protocol ─────────────────────────────────────────────────

PROMPT_HEAD = """You map a question onto a closed list of available quantities.

RULES
- Choose EXACTLY two ids: one numerator, one denominator.
- Both ids MUST appear verbatim in the list below. Never invent an id.
- Do NOT compute, estimate or explain. Do NOT output any number.
- If no pair of listed quantities answers the question, reply with null ids.

Reply with JSON only, in exactly this shape:
{"numerator": "<id>", "denominator": "<id>", "why": "<8 words max>"}

QUESTION: """


class MapperError(Exception):
    pass


class OllamaMapper:
    """The reasoning model, used ONLY as a closed-list classifier.

    temperature=0 and a fixed seed make it as stable as an LLM gets. Stability
    is still *checked* rather than assumed: `run_loop` asks twice and requires
    agreement before anything is installed.
    """

    def __init__(self, model: Optional[str] = None,
                 url: Optional[str] = None, timeout: float = 240.0):
        # Default in here rather than at the call site: a CLI that passes
        # --model's None through would otherwise override the default with
        # nothing, which is a confusing way to fail.
        self.model = model or MODEL_DEFAULT
        self.url = (url or OLLAMA_DEFAULT).rstrip("/")
        self.timeout = timeout
        self.last_latency = None

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags",
                                        timeout=5) as r:
                tags = json.loads(r.read().decode())
            return any(m.get("name") == self.model
                       for m in tags.get("models", []))
        except Exception:
            return False

    def map_ids(self, question: str, served: str,
                inventory: List[Quantity]) -> Dict[str, Any]:
        listing = "\n".join(
            f"- {q.id}  ({q.kind})" + (f"  \u2014 {q.about}" if q.about else "")
            for q in inventory)
        prompt = (PROMPT_HEAD + question + "\n\nQUESTION WAS ANSWERED WITH: "
                  + str(served) + "\n\nAVAILABLE QUANTITIES:\n" + listing + "\n")
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "seed": 42},
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read().decode())
        except urllib.error.URLError as e:
            raise MapperError(f"cannot reach Ollama at {self.url}: {e}") from e
        self.last_latency = time.time() - t0
        return parse_pick(out.get("message", {}).get("content", ""))


def parse_pick(raw: str) -> Dict[str, Any]:
    """Extract the JSON object. deepseek-r1 emits  thinking blocks; drop them."""
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw or "",
                  flags=re.S | re.I).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


# ── 3. the anti-hallucination gate ─────────────────────────────────────────

def validate_pick(pick: Dict[str, Any],
                  inventory: List[Quantity]) -> tuple:
    """(clean_pick, reason). Anything outside the closed list is rejected.

    Note the "null" STRING: deepseek-r1 replied {"numerator": "null"} rather
    than JSON null. Treated as a decline, which is what it means — not as an
    invalid id, which would read as a model failure.
    """
    if not pick:
        return None, "no parseable JSON in the reply"
    n, d = pick.get("numerator"), pick.get("denominator")

    def is_null(v):
        return v is None or (isinstance(v, str)
                             and v.strip().lower() in ("null", "none", "", "n/a"))

    if is_null(n) and is_null(d):
        return None, "the model declined: no pair of listed quantities fits"
    ids = {q.id for q in inventory}
    for label, v in (("numerator", n), ("denominator", d)):
        if is_null(v):
            return None, f"{label} was null"
        if v not in ids:
            return None, f"{label} is not in the closed list: {v!r}"
    if n == d:
        return None, "numerator and denominator are the same quantity"
    return {"numerator": n, "denominator": d,
            "why": str(pick.get("why") or "")[:120]}, None


# ── 4-6. compute, check invariants, verify ─────────────────────────────────

def _normalize_question(q: str) -> str:
    s = re.sub(r"[^\w\s%]", " ", (q or "").lower())
    return re.sub(r"\s+", " ", s).strip()


LEAD = re.compile(r"^(what|which|how|who|where|when|show|list|give|tell|"
                  r"me|is|are|the|a|an)\s+", re.I)


def triggers_from_question(question: str) -> List[str]:
    """Triggers derived DETERMINISTICALLY from the question, never from a model.

    A model-generated trigger list is how a new probe hijacks its siblings: the
    earlier run's new probe carried the original's three tokens plus extras, and
    trigger breadth won the tie. The question itself is narrow by construction,
    and the backtest still has to prove it.
    """
    norm = _normalize_question(question)
    if not norm:
        return []
    out = [norm]
    head = norm
    for _ in range(4):
        nxt = LEAD.sub("", head)
        if nxt == head:
            break
        head = nxt
    if head and head != norm and len(head.split()) >= 2:
        out.append(head)
    return out[:2]


@dataclass
class Proposal:
    question: str
    served: str
    pick: Optional[Dict[str, Any]] = None
    value: Optional[Dict[str, Any]] = None
    metric: Optional[DerivedMetric] = None
    checks: List[Dict[str, Any]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    stable: Optional[bool] = None
    latency_s: Optional[float] = None

    @property
    def ok(self) -> bool:
        return bool(self.pick and self.value and not self.reasons)

    def to_json(self) -> Dict[str, Any]:
        return {"question": self.question, "served": self.served,
                "pick": self.pick, "value": self.value,
                "metric": self.metric.to_json() if self.metric else None,
                "checks": self.checks, "reasons": self.reasons,
                "stable": self.stable, "ok": self.ok,
                "latency_s": (round(self.latency_s, 2)
                              if self.latency_s is not None else None)}


def propose(question: str, served: str, observations: Dict[str, Dict],
            mapper, oracle: Optional[float] = None,
            tolerance: float = 1e-6, check_stability: bool = True,
            scale: str = "percent") -> Proposal:
    """Full loop, no side effects. Returns a Proposal; installing is separate."""
    p = Proposal(question=question, served=served)
    inventory = inventory_from_observations(observations)
    if not inventory:
        p.reasons.append("the instance exposes no measurable quantities")
        return p

    t0 = time.time()
    try:
        first = mapper.map_ids(question, served, inventory)
    except MapperError as e:
        p.reasons.append(str(e))
        return p
    p.latency_s = time.time() - t0
    p.checks.append({"name": "closed_inventory", "ok": True,
                     "detail": f"{len(inventory)} quantities observed"})

    if check_stability:
        try:
            second = mapper.map_ids(question, served, inventory)
            p.stable = (validate_pick(first, inventory)[0]
                        == validate_pick(second, inventory)[0])
        except MapperError:
            p.stable = None
    else:
        p.stable = None
    p.checks.append({"name": "stable_across_two_calls",
                     "ok": p.stable is not False,
                     "detail": {True: "identical picks", False:
                                "DIFFERENT picks — refusing to install",
                                None: "not checked"}[p.stable]})
    if p.stable is False:
        p.reasons.append("the model gave different answers for the same "
                         "question; a mapping that is not reproducible cannot "
                         "be installed")

    clean, why = validate_pick(first, inventory)
    p.checks.append({"name": "ids_in_closed_list", "ok": bool(clean),
                     "detail": why or "both ids exist"})
    if not clean:
        p.reasons.append(why)
        return p
    p.pick = clean

    try:
        value = evaluate(DerivedMetric(
            name="probe", kind="ratio", numerator=clean["numerator"],
            denominator=clean["denominator"], scale=scale), observations)
    except DerivedError as e:
        p.checks.append({"name": "computes", "ok": False, "detail": str(e)})
        p.reasons.append(str(e))
        return p
    p.value = value
    p.checks.append({"name": "computes", "ok": True,
                     "detail": f"{value['ratio']:.6f}"})

    exp = expectation_for(question)
    violation = check_expectation(value["ratio"], exp)
    p.checks.append({"name": f"expectation_{exp}", "ok": not violation,
                     "detail": violation or f"ratio {value['ratio']:.6f} is "
                                            f"consistent with the question"})
    if violation:
        p.reasons.append(violation)

    if oracle is not None:
        delta = abs(value["ratio"] - float(oracle))
        good = delta <= tolerance
        p.checks.append({"name": "matches_outside_oracle", "ok": good,
                         "detail": f"delta {delta:.2e} vs oracle {oracle}"})
        if not good:
            p.reasons.append(f"computed {value['ratio']:.6f} but the oracle says "
                             f"{oracle} (delta {delta:.2e})")
    else:
        p.checks.append({"name": "matches_outside_oracle", "ok": None,
                         "detail": "no oracle supplied — pass --oracle for an "
                                   "independent check"})

    name = _metric_name(question)
    p.metric = DerivedMetric(
        name=name, kind="ratio", numerator=clean["numerator"],
        denominator=clean["denominator"], scale=scale,
        description=_describe(question, value),
        triggers=triggers_from_question(question), question=question,
        provenance={"model": getattr(mapper, "model", "unknown"),
                    "why": clean["why"],
                    "verified_against": oracle,
                    "ts": time.time(),
                    "checks": [c["name"] for c in p.checks if c["ok"]]})
    return p


def _metric_name(question: str) -> str:
    words = [w for w in _normalize_question(question).split()
             if w not in ("what", "is", "the", "of", "a", "an", "how", "many",
                          "does", "do", "are", "and", "in", "for", "to")]
    return ("derived_" + "_".join(words[:6]))[:64] or "derived_metric"


def _describe(question: str, value: Dict[str, Any]) -> str:
    return (f"Derived metric answering: {question!r} — "
            f"{value['numerator']['id']} / {value['denominator']['id']} "
            f"({value['unit']})")


# ── 7. backtest: prove it did not hijack its siblings ──────────────────────

def snapshot_routes(questions: List[str], ask: Callable[[str], Dict]
                    ) -> Dict[str, Optional[str]]:
    """Route every question NOW. This is the baseline.

    The baseline must be THIS router's own behaviour, captured before the
    change. Comparing against another engine's answers reports that engine's
    pre-existing disagreements as damage done by the new metric — the first run
    of this loop claimed 16 regressions, and all 16 were differences between
    neuralosd's scorer and the menu's original engine.
    """
    out: Dict[str, Optional[str]] = {}
    for q in questions:
        try:
            out[q] = (ask(q) or {}).get("probe")
        except Exception:                              # NoResults is a refusal
            out[q] = None
    return out


def compare_routes(before: Dict[str, Optional[str]],
                   after: Dict[str, Optional[str]],
                   exempt: Optional[set] = None) -> Dict[str, Any]:
    """Regression = a question whose route CHANGED.

    Only the target question is allowed to change; that is the whole point of
    installing. Everything else must be exactly as it was, including questions
    that were already refusing or already routing somewhere odd.
    """
    exempt = exempt or set()
    changed = []
    for q, was in before.items():
        if q in exempt:
            continue
        now = after.get(q)
        if now != was:
            changed.append({"question": q, "before": was, "after": now})
    same = sum(1 for q, was in before.items()
               if q not in exempt and after.get(q) == was)
    return {"total": len([q for q in before if q not in exempt]),
            "unchanged": same, "changed": len(changed),
            "regressions": changed}


def backtest(cases: List[Dict[str, str]], ask: Callable[[str], Dict]
             ) -> Dict[str, Any]:
    """Replay questions against EXPLICIT expectations (a golden bank).

    Use this when you know what each question should route to. For "did my
    change break anything?" use snapshot_routes + compare_routes instead.
    """
    results, passed = [], 0
    for case in cases:
        try:
            got, err = (ask(case["question"]) or {}).get("probe"), None
        except Exception as e:                         # noqa: BLE001
            got, err = None, type(e).__name__
        ok = (got == case["expect"])
        passed += bool(ok)
        results.append({"question": case["question"],
                        "expect": case["expect"], "got": got,
                        "ok": ok, "error": err})
    return {"total": len(cases), "passed": passed,
            "failed": len(cases) - passed, "results": results}


# ── 8. install ─────────────────────────────────────────────────────────────

def install(metric: DerivedMetric, instance_dir: str) -> str:
    """Add or replace a metric in derived.json. Never touches probes.py."""
    from .derived import load as load_derived, save as save_derived
    path = os.path.join(instance_dir, "derived.json")
    metrics = [m for m in load_derived(path) if m.name != metric.name]
    metrics.append(metric)
    save_derived(path, metrics)
    return path


def observations_from(probes: List[Callable],
                      kwargs: Optional[Dict[str, Dict]] = None
                      ) -> Dict[str, Dict]:
    """Call every probe once and record what it returned.

    Observation, so the closed inventory is true by construction. Probe
    failures are recorded as absent rather than aborting the sweep.
    """
    kwargs = kwargs or {}
    out: Dict[str, Dict] = {}
    for fn in probes:
        meta = getattr(fn, "_probe", None)
        name = getattr(meta, "name", None) or getattr(fn, "__name__", "?")
        try:
            r = fn(**kwargs.get(name, {}))
        except Exception:
            continue
        if isinstance(r, dict):
            out[name] = r
    return out
