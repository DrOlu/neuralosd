import os

"""neuralosd — deterministic-first agentic runtime for neuralOS.

One pip install. Two sandbox backends (BoxLite, Microsandbox). Any data
source. Offline. On CPU. With verification gates, audit trail, and
deterministic routing.
"""
__version__ = "1.0.3"

_DOCS = {
    "usage": "neuralOS usage and operations guide",
    "architecture": "architectural and design manual",
    "cookbook": "agentic cookbook — complete recipes",
    "reference": "API reference — every class, function, parameter",
}


def skills() -> dict:
    """List bundled agent skill documents (neuralOS operational manuals)."""
    skills_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    result = {}
    if os.path.isdir(skills_dir):
        for f in sorted(os.listdir(skills_dir)):
            if f.endswith(".md"):
                result[f.replace(".md", "")] = os.path.join(skills_dir, f)
    return result


def docs(topic: str = None) -> str:
    """Read bundled neuralOS documentation. Topics: usage, architecture, cookbook, reference."""
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    if topic is None:
        return "\n".join(f"  {k:15s} {v}" for k, v in _DOCS.items())
    if topic not in _DOCS:
        return f"unknown topic: {topic!r}. Available: {', '.join(_DOCS)}"
    path = os.path.join(docs_dir, f"{topic}.md")
    with open(path, encoding="utf-8") as f:
        return f.read()

from .probe import probe, enum_arg, pattern_arg, int_arg, ProbeMeta
from .router import Router, NoResults
from .instance import Instance
from .chains import ChainRunner, chain, ChainError
from .meta import MetaSelector
from .hitl import ConfirmStore, ConfirmRequired
from .lint import TriggerLinter

__all__ = [
    "probe", "enum_arg", "pattern_arg", "int_arg", "ProbeMeta",
    "Router", "NoResults", "Instance",
    "ChainRunner", "chain", "ChainError",
    "MetaSelector",
    "ConfirmStore", "ConfirmRequired",
    "TriggerLinter",
]
