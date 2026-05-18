"""Taxonomaid: hybrid auto-sorter for shared folders.

Combines deterministic rules with an LLM fallback that learns its decisions
back into rules over time. The package is organised as a hexagonal
ports-and-adapters architecture.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.3"
