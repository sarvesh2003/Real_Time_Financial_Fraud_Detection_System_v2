"""
One module per failure class in the taxonomy.

Importing this package imports every check module, which is what populates the
registry. A new check module must be added to _MODULES below or it will never
run, and that is the only wiring a new check needs.
"""

from __future__ import annotations

import importlib

_MODULES = (
    "stimulus_validity",
    "context_integrity",
    "behavioral_evidence",
    "scoring_integrity",
    "comparability",
    "metric_soundness",
)


def load_all() -> None:
    """Import every check module so its decorators register their checks."""
    for name in _MODULES:
        importlib.import_module(f"{__name__}.{name}")
