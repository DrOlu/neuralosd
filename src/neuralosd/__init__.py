"""neuralosd — deterministic-first agentic runtime for neuralOS.

One pip install. Two sandbox backends (BoxLite, Microsandbox). Any data
source. Offline. On CPU. With verification gates, audit trail, and
deterministic routing.
"""
__version__ = "1.0.0"

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
