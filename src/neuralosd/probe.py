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
          confirm: bool = False) -> Callable:
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
        )
        return fn
    return deco
