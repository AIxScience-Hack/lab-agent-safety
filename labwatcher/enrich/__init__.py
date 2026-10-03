"""LabWatcher enrichment: Amass literature / drug / patent context (cached).

Names are resolved lazily so ``python -m labwatcher.enrich.amass`` does not
import the module twice (runpy warning) and importing the package stays cheap.
"""
from __future__ import annotations

import importlib

__all__ = [
    "AmassCache",
    "AmassClient",
    "AmassError",
    "CORES",
    "DEFAULT_CACHE_PATH",
    "MAX_LIMIT",
    "QUERIES_PATH",
    "cache_key",
    "enrich_session",
    "fetch_records",
    "load_queries",
    "populate_cache",
    "precedent",
    "precedent_items",
    "summarise_record",
]


def __getattr__(name: str):
    if name in __all__ or name == "amass":
        mod = importlib.import_module(".amass", __name__)
        if name == "amass":
            return mod
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | {"amass"})
