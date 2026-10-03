"""Declarative derived metrics — ratios that a model may PROPOSE but never compute.

The dangerous class of question is the RATIO:

    "what is the average shipping cost per unit"      -> shipped per line item: 3.5x wrong
    "what share of revenue comes from the top 10"     -> shipped a list: no share at all
    "average number of tracks per album"              -> shipped the album list

The router serves real data that answers a *different* question, and it looks
fine. This module closes that class without generating any code.

A derived metric is DATA:

    {"name": "revenue_share_top10",
     "kind": "ratio",
     "numerator": "chinook_top_customers.rows[].total_spend",
     "denominator": "chinook_revenue_by_year.rows[].revenue",
     "scale": "percent"}

Three properties follow from that choice, and they are the whole point:

1. NO CODE GENERATION. Nothing a model emits is ever executed. The spec is
   interpreted by the code below, which is ours and is tested.
2. NO ARITHMETIC BY THE MODEL. The model names two existing quantities; every
   digit comes from summing the probes' own output.
3. REVIEWABLE AS DATA. A reviewer reads two ids and a scale, not Python.

An id that is not in the instance's closed inventory cannot be resolved, so a
fabricated quantity is a rejection rather than a wrong number.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

DERIVED_FILE = "derived.json"

KINDS = ("ratio",)
SCALES = ("ratio", "percent")

# A quantity that is an identifier or a calendar part is not a MEASURE.
# Summing customer ids is arithmetically valid and semantically meaningless, and
# a model asked to pick from a list will happily pick one.
NOT_A_MEASURE = re.compile(
    r"(^|\.)(id|year|month|day|quarter|index|rownum|row_id)$"
    r"|_id$|_ids$|^.*\.rows\[\]\.(id|year|month|day|quarter)$",
    re.I)

# Phrases that ask for a SHARE. Used two ways: as the deterministic escalation
# gate, and as the deterministic expectation the answer must satisfy.
SHARE_PHRASES = ("share of", "proportion of", "percentage of", "percent of",
                 "fraction of", "part of", "as a share")
RATIO_QUALIFIERS = SHARE_PHRASES + (
    "per unit", "per customer", "per album", "per track", "per playlist",
    "per artist", "per invoice", "per order", "per country", "per genre",
    "average per", "on average per", "ratio", "relative to", "versus", "vs")


class DerivedError(Exception):
    """A spec is malformed, unresolvable, or fails an invariant."""


# ── the spec ───────────────────────────────────────────────────────────────

@dataclass
class DerivedMetric:
    name: str
    kind: str
    numerator: str
    denominator: str
    scale: str = "ratio"
    description: str = ""
    triggers: List[str] = field(default_factory=list)
    question: str = ""
    min_coverage: float = 0.6
    """Fraction of the trigger the question must contain. See ProbeMeta.

    Defaults to 0.6: high enough that an 8-token trigger cannot capture a
    2-token question, low enough to survive paraphrasing.
    """
    provenance: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name or ""):
            raise DerivedError(f"bad metric name: {self.name!r}")
        if self.kind not in KINDS:
            raise DerivedError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.scale not in SCALES:
            raise DerivedError(f"scale must be one of {SCALES}, got {self.scale!r}")
        if not self.numerator or not self.denominator:
            raise DerivedError("numerator and denominator are both required")
        if not (0.0 <= float(self.min_coverage) <= 1.0):
            raise DerivedError(f"min_coverage must be within 0..1, got "
                               f"{self.min_coverage!r}")
        if self.numerator == self.denominator:
            raise DerivedError("numerator and denominator are the same quantity")

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def load(path: str) -> List[DerivedMetric]:
    """Read derived.json. A missing file is an empty registry, not an error."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    entries = blob.get("metrics", blob) if isinstance(blob, dict) else blob
    out = []
    for e in entries:
        m = DerivedMetric(**{k: v for k, v in e.items()
                             if k in DerivedMetric.__dataclass_fields__})
        m.validate()
        out.append(m)
    return out


def save(path: str, metrics: List[DerivedMetric]) -> None:
    for m in metrics:
        m.validate()
    blob = {"version": 1, "metrics": [m.to_json() for m in metrics]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ── the closed inventory ───────────────────────────────────────────────────

@dataclass
class Quantity:
    """One measurable quantity, OBSERVED from a probe's actual output."""
    id: str
    probe: str
    path: str
    kind: str                 # "scalar" | "sum_rows"
    sample: Any = None
    about: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def inventory_from_observations(observations: Dict[str, Dict]) -> List[Quantity]:
    """Build the closed list from {probe_name: returned_dict}.

    Observation rather than declaration, so the list is true by construction:
    every id in it is a field some probe really returned. A model choosing from
    it cannot name a quantity that does not exist.
    """
    out: List[Quantity] = []
    for probe_name, result in observations.items():
        if not isinstance(result, dict):
            continue
        for key, value in result.items():
            if _numeric(value):
                out.append(Quantity(f"{probe_name}.{key}", probe_name, key,
                                    "scalar", value))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for rk, rv in value[0].items():
                    if _numeric(rv):
                        out.append(Quantity(f"{probe_name}.rows[].{rk}",
                                            probe_name, f"rows[].{rk}",
                                            "sum_rows", rv))
    return [q for q in out if not NOT_A_MEASURE.search(q.id)]


def _numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve(quantity: Quantity, observations: Dict[str, Dict]) -> Optional[float]:
    """Turn a quantity into a NUMBER. The only place a number is produced."""
    result = observations.get(quantity.probe)
    if not isinstance(result, dict):
        return None
    if quantity.path.startswith("rows[]."):
        key = quantity.path.split(".", 1)[1]
        vals = [r.get(key) for r in (result.get("rows") or [])
                if isinstance(r, dict) and _numeric(r.get(key))]
        return float(sum(vals)) if vals else None
    v = result.get(quantity.path)
    return float(v) if _numeric(v) else None


# ── deterministic expectations ─────────────────────────────────────────────

def expectation_for(question: str) -> str:
    """What the answer MUST look like, read off the question alone.

    A "share of" question cannot legitimately produce more than 1. This is
    derived from the wording, never from the model, so the model cannot relax
    its own acceptance test by claiming the constraint does not apply.
    """
    q = (question or "").lower()
    if any(p in q for p in SHARE_PHRASES):
        return "lte_1"          # 0 < value <= 1
    return "any"


def check_expectation(value: float, expectation: str) -> Optional[str]:
    """Return a reason string when the value violates the expectation."""
    if expectation == "lte_1":
        if value <= 0:
            return f"a share must be positive, got {value!r}"
        if value > 1.0 + 1e-9:
            return (f"a share of a whole cannot exceed 1, got {value:.6f} — "
                    f"numerator and denominator are not in a part/whole relation")
    return None


# ── evaluation ─────────────────────────────────────────────────────────────

def evaluate(metric: DerivedMetric,
             observations: Dict[str, Dict],
             by_id: Optional[Dict[str, Quantity]] = None) -> Dict[str, Any]:
    """Evaluate a spec. Deterministic; no model, no I/O of its own."""
    metric.validate()
    by_id = by_id or {q.id: q for q in
                      inventory_from_observations(observations)}
    num_q = by_id.get(metric.numerator)
    den_q = by_id.get(metric.denominator)
    for label, q in (("numerator", num_q), ("denominator", den_q)):
        if q is None:
            raise DerivedError(f"{label} is not a known quantity: "
                               f"{metric.numerator if label == 'numerator' else metric.denominator!r}")
    num = resolve(num_q, observations)
    den = resolve(den_q, observations)
    if num is None or den is None:
        raise DerivedError(f"could not resolve a value (numerator={num!r}, "
                           f"denominator={den!r})")
    if den == 0:
        raise DerivedError("denominator is zero")
    ratio = num / den
    enough = sum(1 for e in metric.triggers if e) >= 1
    return {
        "metric": metric.name,
        "numerator": {"id": num_q.id, "value": round(num, 2)},
        "denominator": {"id": den_q.id, "value": round(den, 2)},
        "ratio": round(ratio, 6),
        "percent": round(ratio * 100.0, 4),
        "value": round(ratio * 100.0, 4) if metric.scale == "percent"
                 else round(ratio, 6),
        "unit": "%" if metric.scale == "percent" else "ratio",
        "formula": f"{num_q.path.split('.')[-1]} / "
                   f"{den_q.path.split('.')[-1]}",
        "_tool": metric.name,
        "_derived": True,
        "_routable": bool(enough),
    }


# ── turning a spec into a routable probe ───────────────────────────────────

def is_derived(probe) -> bool:
    """True for a probe that came from derived.json rather than the data.

    Derived probes are kept OUT of the closed inventory: a ratio of a ratio is
    algebraically fine and analytically suspect, and it lets an install feed on
    its own output.
    """
    return bool(getattr(probe, "_derived", False))


def as_probe(metric: DerivedMetric, observations: Dict[str, Dict]
             ) -> Optional[Callable]:
    """Wrap a spec as a probe the Router can route to.

    The wrapper closes over a SNAPSHOT of the observations, so a derived probe
    is exactly as deterministic as the probes it is built from, and costs no
    extra database round-trips at ask time.
    """
    from .probe import probe as probe_deco

    metric.validate()
    by_id = {q.id: q for q in inventory_from_observations(observations)}

    def derived_fn():
        return evaluate(metric, observations, by_id)

    derived_fn.__name__ = metric.name
    derived_fn._derived = True          # excluded from future inventories
    return probe_deco(
        description=metric.description or
                    f"Derived metric {metric.name}: {formula_hint(metric)}",
        triggers=list(metric.triggers),
        name=metric.name,
        min_coverage=metric.min_coverage)(derived_fn)


def formula_hint(metric: DerivedMetric) -> str:
    return f"{metric.numerator} / {metric.denominator}"
