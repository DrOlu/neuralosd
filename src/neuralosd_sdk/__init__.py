"""neuralosd_sdk — thin client for a neuralOS instance server."""
from .client import (  # noqa: F401
    AskResult,
    AuthError,
    NeuralOSDClient,
    RefusalError,
    TransportError,
)
__version__ = "2.0.6"
