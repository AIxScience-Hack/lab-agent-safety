"""LabWatcher enrichment: Amass literature / drug / patent context (cached)."""
from .amass import (
    AmassCache,
    AmassClient,
    AmassError,
    CORES,
    DEFAULT_CACHE_PATH,
    MAX_LIMIT,
    QUERIES_PATH,
    cache_key,
    enrich_session,
    load_queries,
    populate_cache,
    precedent,
    precedent_items,
    summarise_record,
)

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
    "load_queries",
    "populate_cache",
    "precedent",
    "precedent_items",
    "summarise_record",
]
