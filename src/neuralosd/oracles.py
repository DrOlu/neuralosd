"""Typed answer oracles (v2.4.0) — the golden bank verifies the ANSWER,
not just the routing.

A golden case with `oracle: {value: V, shape: S}` asserts that the probe's
answer EQUALS V under the rules of shape S — with the shape's invariants
enforced (a count cannot be negative, a percent cannot exceed 100, a date
must parse). Wrong value ≠ pass, even when routing is perfect: a probe that
routes confidently to the wrong number is the worst failure this system has.
"""
import datetime

_TOL = 1e-6


def check_answer(shape, expected, actual):
    """True iff `actual` satisfies the typed oracle (expected, shape)."""
    if expected is None:
        return False
    # A non-str shape (a schema dict forwarded by mistake) used to raise
    # AttributeError inside the oracle, taking down the verify loop that
    # depends on a plain True/False. An unusable oracle is a failed check.
    if shape is not None and not isinstance(shape, str):
        return False
    shape = (shape or "count").strip().lower()
    if shape in ("count", "integer", "int"):
        try:
            e, a = float(expected), float(actual)
        except (TypeError, ValueError):
            return False
        return float(e).is_integer() and a == e and a >= 0
    if shape in ("percent", "ratio", "money", "number", "float"):
        try:
            e, a = float(expected), float(actual)
        except (TypeError, ValueError):
            return False
        if a != a or e != e:                       # NaN
            return False
        if shape == "percent" and not (0.0 <= a <= 100.0):
            return False                            # invariant
        if shape == "ratio" and a < 0:
            return False
        return abs(a - e) <= _TOL * max(1.0, abs(e))
    if shape == "date":
        return (_parse_date(actual) is not None
                and str(_parse_date(actual)) == str(_parse_date(expected)))
    if shape == "text":
        if actual is None:
            return False
        return str(actual).strip().casefold() == \
            str(expected).strip().casefold()
    return False


def _parse_date(v):
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v
    text = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%d/%m/%Y",
                "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(text[:len(fmt) + 6], fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def extract_answer(results, path=None):
    """Pull the answer a value-oracle should check out of a probe envelope's
    results.

    With `path` ("count", "rows[0].region", "total"): a precise dotted walk.
    Without: a scan of every leaf — numeric leaves preferred for numeric
    shapes — and the match must still satisfy the shape's rules, so a wrong
    number anywhere in the tree cannot masquerade as the oracle value
    (the caller compares, this only finds candidates).
    Returns a list of candidate leaves; empty when nothing is found.
    """
    leaves = []

    def walk(node, dotted):
        if len(leaves) > 500:                      # bounded
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{dotted}.{k}" if dotted else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node[:100]):
                walk(v, f"{dotted}[{i}]")
        else:
            leaves.append((dotted, node))

    walk(results, "")
    if path:
        want = [p for p in path.split(".") if p]
        for dotted, v in leaves:
            parts = [p for p in (seg.split("[")[0]
                                 for seg in dotted.split(".")) if p]
            if parts == want:
                return v
        return None
    # candidate scan: numeric leaves first, then strings
    numerics = [v for _, v in leaves
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if numerics:
        return numerics
    strings = [v for _, v in leaves if isinstance(v, str) and v.strip()]
    return strings
