"""Module-level Metrics singleton.

Sub-modules import `metrics` and emit counters / gauges / histograms.
The HTTP `/metrics` endpoint in `api.py` renders the same instance.

Why a global? The alternative is threading a `Metrics` instance through
every component constructor, which adds a lot of boilerplate for a logger-
shaped concern. The singleton pattern matches Prometheus client libraries
in every other language (Java, Go, Rust).
"""
from __future__ import annotations

from .middleware import Metrics

# Module-global instance — populated at coordinator startup.
metrics = Metrics()


def get() -> Metrics:
    """Convenience accessor — returns the shared singleton."""
    return metrics
