"""The @probe decorator — the "route decorator" of intelligence."""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ProbeMeta:
    """Everything the framework needs to route, cage, verify, and audit."""
    name: str
    description: str
    triggers: List[str]
    args: Dict[str, Dict[str, Any]]      # argname -> spec
    pii: List[str]
    conf_gate: Optional[float]
    tier: str
    confirm: bool
    function: Callable
    min_coverage: Optional[float] = None

    def _vocab_tokens(self):
        """Every token this probe can be said to know: triggers, name,
        and enum values. Used by the router's question-coverage
        check - a question token absent from here is a domain noun this probe
        has never heard of."""
        from .router import tokens
        v = set()
        for t in self.triggers or []:
            v |= tokens(t)
        v |= tokens(self.name.replace("_", " "))
        for spec in (self.args or {}).values():
            for val in spec.get("values") or []:
                v |= tokens(str(val))
        return frozenset(v)
    """Fraction of the probe's best trigger that the question must contain.

    Token-overlap scoring is happy with a 2-token question matching 2 of an
    8-token trigger, which is how an installed derived metric captured the
    unrelated question "total revenue". Coverage asks a stricter question:
    is this probe's trigger SUBSTANTIALLY present, or merely touching?

    None keeps the original scoring, so every existing probe is unaffected.
    """


def arg(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Raw arg spec (escape hatch)."""
    return dict(spec)


def enum_arg(values: List[str], required: bool = True) -> Dict[str, Any]:
    s = {"type": "enum", "values": list(values)}
    if not required:
        s["required"] = False
    return s


def pattern_arg(pattern: str, required: bool = True) -> Dict[str, Any]:
    """Regex with ONE capture group = the extracted value."""
    s = {"type": "pattern", "pattern": pattern}
    if not required:
        s["required"] = False
    return s


def int_arg(minimum: int = 1, maximum: int = 100,
            default: Optional[int] = None) -> Dict[str, Any]:
    s = {"type": "integer", "min": minimum, "max": maximum}
    if default is not None:
        s["default"] = default
        s["required"] = False
    return s


def probe(description: str, triggers: List[str],
          args: Optional[Dict[str, Dict[str, Any]]] = None,
          pii: Optional[List[str]] = None,
          conf_gate: Optional[float] = None,
          tier: str = "in-process",
          name: Optional[str] = None,
          confirm: bool = False,
          min_coverage: Optional[float] = None) -> Callable:
    """Declare a typed capability.

    The decorated function must be a PURE function of its args -> the data
    layer. No model calls inside a probe. Results must be small (row cap).
    """
    def deco(fn: Callable) -> Callable:
        fn._probe = ProbeMeta(
            name=name or fn.__name__,
            description=description,
            triggers=list(triggers),
            args=dict(args or {}),
            pii=list(pii or []),
            conf_gate=conf_gate,
            tier=tier,
            confirm=confirm,
            function=fn,
            min_coverage=min_coverage,
        )
        return fn
    return deco
