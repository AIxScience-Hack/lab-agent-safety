"""Amass literature / drug / patent enrichment for LabWatcher.

Public surface (see labwatcher/SPEC.md, "Amass"):

    AmassClient(api_key=None, ...)            httpx client, Bearer auth, rate-limit aware
        .search(core, query, **filters)       -> list[dict]   (limit clamped to <= 5)
        .get(core, amass_id)                  -> dict
        .credits_spent / .requests_made       observed X-Amass-Credit-Cost totals

    enrich_session(context, env, card_title, keywords, cache=True)
        -> list[{source_core, title, id, url, snippet, relevance_note}]

    precedent(context, category_id, cache=True) -> str   (short bullet text for Stage 3)

    load_queries()                            parsed queries.yaml
    AmassCache                                JSON file cache keyed by core+query+filters

Everything is cache-first: with ``cache=True`` (default) a query whose key is in
``labwatcher/data/amass_cache.json`` never touches the network. A cache miss is
fetched only when an ``AMASS_API_KEY`` is available; otherwise it yields no results
(and the caller still gets whatever else the cache holds). Tests run fully offline.

Populate / refresh the cache (uses the real key, ~25 requests):

    python -m labwatcher.enrich.amass --populate
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
import yaml

__all__ = [
    "AmassClient",
    "AmassCache",
    "AmassError",
    "CORES",
    "DEFAULT_CACHE_PATH",
    "QUERIES_PATH",
    "MAX_LIMIT",
    "cache_key",
    "enrich_session",
    "load_queries",
    "precedent",
    "precedent_items",
    "populate_cache",
    "summarise_record",
]

BASE_URL = "https://api.amass.tech/api/v1"
MAX_LIMIT = 5  # SPEC: limit <= 5 per search

HERE = Path(__file__).resolve().parent
QUERIES_PATH = HERE / "queries.yaml"
DEFAULT_CACHE_PATH = HERE.parent / "data" / "amass_cache.json"

# Canonical core names -> URL path segment.
CORES: dict[str, str] = {
    "BiomedCore": "biomedcore",
    "TrialCore": "trialcore",
    "DrugCore": "drugcore",
    "RegulatoryCore": "regulatorycore",
    "GeneCore": "genecore",
    "PatentCore": "patentcore",
}
_CORE_BY_LOWER = {v: k for k, v in CORES.items()}


class AmassError(RuntimeError):
    """Raised for non-retryable API failures (4xx other than 429, exhausted retries)."""

    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"Amass {status}: {message}")
        self.status = status
        self.payload = payload


def normalize_core(core: str) -> str:
    """Accept 'BiomedCore', 'biomedcore', 'BIOMEDCORE' -> 'BiomedCore'."""
    if core in CORES:
        return core
    key = core.strip().lower().replace("_", "").replace("-", "")
    if key in _CORE_BY_LOWER:
        return _CORE_BY_LOWER[key]
    raise ValueError(f"Unknown Amass core: {core!r} (expected one of {sorted(CORES)})")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- client


class AmassClient:
    """Thin httpx wrapper around the Amass REST API.

    Rate limiting: every response's ``X-RateLimit-Remaining`` is recorded; when it
    drops to ``min_remaining`` or below the client sleeps until ``X-RateLimit-Reset``
    before the next request. A 429 honours ``Retry-After`` (exponential fallback),
    5xx retries once with backoff. ``sleep`` is injectable so tests run instantly.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        min_remaining: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("AMASS_API_KEY", "")
        if not self.api_key:
            raise AmassError(401, "AMASS_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.min_remaining = min_remaining
        self._sleep = sleep
        self._lock = threading.Lock()
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "labwatcher-enrich/1.0",
            },
            transport=transport,
        )
        # observability
        self.requests_made = 0
        self.credits_spent = 0.0
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: str | None = None
        self.last_headers: dict[str, str] = {}

    # -- context manager -----------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AmassClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public API ----------------------------------------------------------
    def search(self, core: str, query: str, **filters: Any) -> list[dict]:
        """GET /cores/{core}/records?query=...  Returns the ``data`` list.

        ``limit`` is clamped to ``MAX_LIMIT`` (5). List-valued filters are sent as
        repeated params (OR within a filter) except for PatentCore, whose multi-value
        filters are comma-separated in a single param.
        """
        core = normalize_core(core)
        if not query or not str(query).strip():
            raise ValueError("query must be a non-empty string")
        params = self._build_params(core, query, filters)
        data = self._request("GET", f"/cores/{CORES[core]}/records", params=params)
        if not isinstance(data, list):
            raise AmassError(502, "search response 'data' was not a list", data)
        return data

    def get(self, core: str, amass_id: str, include: Iterable[str] | None = None) -> dict:
        """GET /cores/{core}/records/{amassId}. Returns the ``data`` object."""
        core = normalize_core(core)
        if not amass_id:
            raise ValueError("amass_id is required")
        params: list[tuple[str, str]] = [("include", inc) for inc in (include or [])]
        data = self._request("GET", f"/cores/{CORES[core]}/records/{amass_id}", params=params)
        if not isinstance(data, dict):
            raise AmassError(502, "get response 'data' was not an object", data)
        return data

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _build_params(core: str, query: str, filters: dict[str, Any]) -> list[tuple[str, str]]:
        limit = filters.pop("limit", MAX_LIMIT)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = MAX_LIMIT
        limit = max(1, min(MAX_LIMIT, limit))
        params: list[tuple[str, str]] = [("query", str(query)), ("limit", str(limit))]
        comma_joined = core == "PatentCore"
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, bool):
                params.append((key, "true" if value else "false"))
            elif isinstance(value, (list, tuple, set)):
                vals = [str(v) for v in value if v is not None]
                if not vals:
                    continue
                if comma_joined and key != "include":
                    params.append((key, ",".join(vals)))
                else:
                    params.extend((key, v) for v in vals)
            else:
                params.append((key, str(value)))
        return params

    def _record_headers(self, headers: httpx.Headers) -> None:
        self.last_headers = {k: v for k, v in headers.items() if k.lower().startswith(("x-ratelimit", "x-amass", "retry-after"))}
        rem = headers.get("X-RateLimit-Remaining")
        if rem is not None:
            try:
                self.rate_limit_remaining = int(rem)
            except ValueError:
                pass
        reset = headers.get("X-RateLimit-Reset")
        if reset:
            self.rate_limit_reset = reset
        cost = headers.get("X-Amass-Credit-Cost")
        if cost:
            try:
                self.credits_spent += float(cost)
            except ValueError:
                pass

    def _seconds_until_reset(self) -> float:
        if not self.rate_limit_reset:
            return 1.0
        try:
            reset = datetime.fromisoformat(self.rate_limit_reset.replace("Z", "+00:00"))
        except ValueError:
            try:
                return max(0.0, float(self.rate_limit_reset))
            except ValueError:
                return 1.0
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        return max(0.0, (reset - datetime.now(timezone.utc)).total_seconds())

    def _pace(self) -> None:
        """Proactively wait when the window is nearly exhausted."""
        if self.rate_limit_remaining is not None and self.rate_limit_remaining <= self.min_remaining:
            wait = min(65.0, self._seconds_until_reset() + 0.5)
            if wait > 0:
                self._sleep(wait)
            self.rate_limit_remaining = None  # re-learn from the next response

    def _request(self, method: str, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        attempt = 0
        backoff = 1.0
        while True:
            with self._lock:
                self._pace()
                resp = self._http.request(method, path, params=params)
                self.requests_made += 1
                self._record_headers(resp.headers)
            if resp.status_code == 429:
                attempt += 1
                if attempt > self.max_retries:
                    raise AmassError(429, "rate limited; retries exhausted", _safe_json(resp))
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except ValueError:
                    wait = backoff
                self._sleep(max(0.5, min(65.0, wait)))
                self.rate_limit_remaining = None  # already waited; don't pace again
                backoff = min(32.0, backoff * 2)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise AmassError(resp.status_code, "server error; retries exhausted", _safe_json(resp))
                self._sleep(backoff)
                backoff = min(32.0, backoff * 2)
                continue
            payload = _safe_json(resp)
            if resp.status_code >= 400:
                err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
                msg = err.get("message") or resp.text[:200]
                if err.get("fields"):
                    msg = f"{msg} fields={err['fields']}"
                raise AmassError(resp.status_code, msg, payload)
            if not isinstance(payload, dict) or "data" not in payload:
                raise AmassError(502, "malformed response (no 'data' envelope)", payload)
            return payload["data"]


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# --------------------------------------------------------------------------- cache


def cache_key(core: str, query: str, filters: dict[str, Any] | None = None) -> str:
    """Stable key: core|query|sorted-json(filters). ``limit`` is normalised."""
    core = normalize_core(core)
    f = dict(filters or {})
    f["limit"] = max(1, min(MAX_LIMIT, int(f.get("limit", MAX_LIMIT))))
    return f"{core}|{query.strip()}|{json.dumps(f, sort_keys=True, separators=(',', ':'))}"


class AmassCache:
    """JSON file cache of search results. Thread-safe within a process.

    File shape::

        {"version": 1, "updated_at": "...", "entries": {
            "<key>": {"core", "query", "filters", "fetched_at", "credit_cost", "records": [...]}}}
    """

    VERSION = 1

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_CACHE_PATH
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"version": self.VERSION, "updated_at": None, "entries": {}}
        self._loaded = False

    def load(self) -> "AmassCache":
        if self._loaded:
            return self
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = {}
                if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                    self._data = raw
            self._loaded = True
        return self

    def save(self) -> None:
        with self._lock:
            self._data["version"] = self.VERSION
            self._data["updated_at"] = _utcnow_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)

    def __contains__(self, key: str) -> bool:
        return key in self.load()._data["entries"]

    def __len__(self) -> int:
        return len(self.load()._data["entries"])

    def keys(self) -> list[str]:
        return list(self.load()._data["entries"].keys())

    def get(self, core: str, query: str, filters: dict[str, Any] | None = None) -> list[dict] | None:
        entry = self.load()._data["entries"].get(cache_key(core, query, filters))
        if entry is None:
            return None
        return list(entry.get("records", []))

    def put(self, core: str, query: str, filters: dict[str, Any] | None, records: list[dict], credit_cost: float | None = None) -> None:
        self.load()
        with self._lock:
            self._data["entries"][cache_key(core, query, filters)] = {
                "core": normalize_core(core),
                "query": query.strip(),
                "filters": dict(filters or {}),
                "fetched_at": _utcnow_iso(),
                "credit_cost": credit_cost,
                "records": records,
            }


# --------------------------------------------------------------------------- records


def _trim(text: Any, n: int = 400) -> str:
    if not text:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def summarise_record(core: str, rec: dict) -> dict:
    """Reduce a raw Amass record to the compact shape stored in the cache / returned
    by enrich_session: id, title, url, snippet, date, source + a few core-specific IDs.
    """
    core = normalize_core(core)
    amass_id = rec.get("amassId") or ""
    out: dict[str, Any] = {"id": amass_id, "source_core": core}
    if core == "BiomedCore":
        out.update(
            title=rec.get("title") or "",
            url=rec.get("url") or (f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/" if rec.get("pmid") else ""),
            snippet=_trim(rec.get("abstract")),
            date=rec.get("publicationDate"),
            source=rec.get("journal"),
            pmid=rec.get("pmid"),
            doi=rec.get("doi"),
            citation_count=rec.get("citationCount"),
            is_retracted=rec.get("isRetracted"),
        )
    elif core == "DrugCore":
        out.update(
            title=rec.get("name") or "",
            url=rec.get("url") or (f"https://www.ebi.ac.uk/chembl/explore/compound/{rec['chemblId']}" if rec.get("chemblId") else ""),
            snippet=_trim(rec.get("description")),
            date=None,
            source="ChEMBL",
            chembl_id=rec.get("chemblId"),
            drug_type=rec.get("drugType"),
            max_clinical_stage=rec.get("maxClinicalStage"),
            synonyms=list(rec.get("synonyms") or [])[:8],
            trade_names=list(rec.get("tradeNames") or [])[:8],
        )
    elif core == "RegulatoryCore":
        secs = rec.get("documentSections") or []
        matched = next((s.get("matchedText") for s in secs if s.get("matchedText")), None)
        out.update(
            title=rec.get("name") or "",
            url=rec.get("url") or rec.get("sourceUrl") or "",
            snippet=_trim(matched or rec.get("therapeuticIndication")),
            date=rec.get("authorizationDate"),
            source=rec.get("agency"),
            active_substance=rec.get("activeSubstance"),
            authorization_status=rec.get("authorizationStatus"),
        )
    elif core == "PatentCore":
        out.update(
            title=rec.get("title") or "",
            url=rec.get("url") or (f"https://patents.google.com/patent/{rec['publicationNumber'].replace('-', '')}" if rec.get("publicationNumber") else ""),
            snippet=_trim(rec.get("abstract")),
            date=rec.get("priorityDate") or rec.get("publicationDate"),
            source=", ".join(rec.get("assignees") or [])[:120] or None,
            publication_number=rec.get("publicationNumber"),
            cpc_codes=list(rec.get("cpcCodes") or [])[:6],
            cited_by_count=rec.get("citedByCount"),
        )
    elif core == "TrialCore":
        out.update(
            title=rec.get("briefTitle") or rec.get("officialTitle") or "",
            url=rec.get("url") or rec.get("sourceUrl") or "",
            snippet=_trim(rec.get("briefSummary")),
            date=rec.get("startDate"),
            source=rec.get("sponsorName"),
            nct_id=rec.get("nctId"),
            phase=rec.get("phase"),
            overall_status=rec.get("overallStatus"),
        )
    else:  # GeneCore
        out.update(
            title=" — ".join(x for x in [rec.get("symbol"), rec.get("name")] if x),
            url=rec.get("url") or "",
            snippet=_trim(rec.get("summary")),
            date=None,
            source="HGNC/Open Targets",
            ensembl_gene_id=rec.get("ensemblGeneId"),
        )
    return out


# --------------------------------------------------------------------------- queries


_QUERIES_CACHE: dict[str, Any] | None = None
_QUERIES_MTIME: float | None = None


def load_queries(path: Path | str | None = None) -> dict[str, Any]:
    """Parse queries.yaml (cached by mtime). Returns {defaults, envs, precedents}."""
    global _QUERIES_CACHE, _QUERIES_MTIME
    p = Path(path) if path else QUERIES_PATH
    mtime = p.stat().st_mtime
    if path is None and _QUERIES_CACHE is not None and _QUERIES_MTIME == mtime:
        return _QUERIES_CACHE
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    envs = raw.get("envs") or {}
    precedents = raw.get("precedents") or {}
    # validate
    for ctx, env_map in envs.items():
        if not isinstance(env_map, dict):
            raise ValueError(f"queries.yaml envs.{ctx} must be a mapping of env -> list")
        for env, items in env_map.items():
            if not isinstance(items, list):
                raise ValueError(f"queries.yaml envs.{ctx}.{env} must be a list")
            for it in items:
                _validate_query_item(it, f"envs.{ctx}.{env}")
    for cat, ctx_map in precedents.items():
        if not isinstance(ctx_map, dict):
            raise ValueError(f"queries.yaml precedents.{cat} must map context -> query")
        for ctx, it in ctx_map.items():
            _validate_query_item(it, f"precedents.{cat}.{ctx}")
    parsed = {"defaults": defaults, "envs": envs, "precedents": precedents}
    if path is None:
        _QUERIES_CACHE, _QUERIES_MTIME = parsed, mtime
    return parsed


def _validate_query_item(it: Any, where: str) -> None:
    if not isinstance(it, dict) or not it.get("core") or not it.get("query"):
        raise ValueError(f"queries.yaml {where}: each item needs 'core' and 'query'")
    normalize_core(it["core"])
    if "filters" in it and it["filters"] is not None and not isinstance(it["filters"], dict):
        raise ValueError(f"queries.yaml {where}: 'filters' must be a mapping")


def _item_filters(item: dict, defaults: dict) -> dict:
    f = dict(item.get("filters") or {})
    f.setdefault("limit", defaults.get("limit", MAX_LIMIT))
    return f


def env_query_items(context: str, env: str, queries: dict | None = None) -> list[dict]:
    q = queries or load_queries()
    return list((q["envs"].get(context) or {}).get(env) or [])


def precedent_query_item(context: str, category_id: str, queries: dict | None = None) -> dict | None:
    q = queries or load_queries()
    ctx_map = q["precedents"].get(category_id) or {}
    return ctx_map.get(context) or ctx_map.get("default")


def all_query_items(queries: dict | None = None) -> list[dict]:
    """Every distinct (core, query, filters) item across envs and precedents."""
    q = queries or load_queries()
    seen: dict[str, dict] = {}
    for ctx, env_map in q["envs"].items():
        for env, items in env_map.items():
            for it in items:
                seen.setdefault(cache_key(it["core"], it["query"], _item_filters(it, q["defaults"])), it)
    for cat, ctx_map in q["precedents"].items():
        for ctx, it in ctx_map.items():
            seen.setdefault(cache_key(it["core"], it["query"], _item_filters(it, q["defaults"])), it)
    return list(seen.values())


# --------------------------------------------------------------------------- fetch-through-cache


def _client_or_none(client: AmassClient | None) -> AmassClient | None:
    if client is not None:
        return client
    if os.environ.get("AMASS_API_KEY"):
        try:
            return AmassClient()
        except AmassError:
            return None
    return None


def fetch_records(
    core: str,
    query: str,
    filters: dict[str, Any] | None = None,
    *,
    cache: bool | AmassCache = True,
    client: AmassClient | None = None,
    cache_path: Path | str | None = None,
) -> list[dict]:
    """Return compact records for one search, cache-first.

    ``cache`` may be ``True`` (default file cache), ``False`` (always live, no write),
    or an ``AmassCache`` instance. On a miss with no usable client -> ``[]``.
    """
    filters = dict(filters or {})
    store: AmassCache | None
    if isinstance(cache, AmassCache):
        store = cache
    elif cache:
        store = AmassCache(cache_path)
    else:
        store = None

    if store is not None:
        hit = store.get(core, query, filters)
        if hit is not None:
            return hit

    cl = _client_or_none(client)
    if cl is None:
        return []
    owns = client is None
    try:
        raw = cl.search(core, query, **dict(filters))
        cost = None
        try:
            cost = float(cl.last_headers.get("x-amass-credit-cost", cl.last_headers.get("X-Amass-Credit-Cost", "")))
        except ValueError:
            cost = None
        records = [summarise_record(core, r) for r in raw]
    finally:
        if owns:
            cl.close()
    if store is not None:
        store.put(core, query, filters, records, credit_cost=cost)
        store.save()
    return records


# --------------------------------------------------------------------------- public enrichment


def _keyword_hits(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t and t.lower() in low]


def enrich_session(
    context: str,
    env: str,
    card_title: str = "",
    keywords: Iterable[str] | None = None,
    cache: bool | AmassCache = True,
    *,
    client: AmassClient | None = None,
    cache_path: Path | str | None = None,
    max_per_query: int = MAX_LIMIT,
) -> list[dict]:
    """Domain precedent for a session: run every queries.yaml entry for (context, env)
    and return flattened results, each ``{source_core, title, id, url, snippet,
    relevance_note, query, date, source}``.

    Ordering: results whose title/snippet mention ``card_title`` words or ``keywords``
    first (and their ``relevance_note`` says which terms matched), then query order.
    Unknown context/env -> ``[]``.
    """
    q = load_queries()
    items = env_query_items(context, env, q)
    if not items:
        return []
    terms = [t for t in (keywords or []) if isinstance(t, str) and len(t.strip()) >= 3]
    title_terms = [w for w in (card_title or "").replace("/", " ").split() if len(w) >= 4]
    terms = list(dict.fromkeys(t.strip() for t in terms + title_terms))

    results: list[tuple[int, int, dict]] = []
    seen_ids: set[str] = set()
    for qi, item in enumerate(items):
        filters = _item_filters(item, q["defaults"])
        try:
            recs = fetch_records(item["core"], item["query"], filters, cache=cache, client=client, cache_path=cache_path)
        except AmassError:
            recs = []
        note = item.get("relevance_note") or ""
        for rec in recs[:max_per_query]:
            rid = rec.get("id") or f"{item['core']}:{rec.get('title')}"
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            hits = _keyword_hits(f"{rec.get('title', '')} {rec.get('snippet', '')}", terms)
            rnote = note
            if hits:
                rnote = f"{note} Matches: {', '.join(hits[:4])}." if note else f"Matches: {', '.join(hits[:4])}."
            results.append(
                (
                    -len(hits),
                    qi,
                    {
                        "source_core": normalize_core(item["core"]),
                        "title": rec.get("title") or "",
                        "id": rec.get("id") or "",
                        "url": rec.get("url") or "",
                        "snippet": rec.get("snippet") or "",
                        "relevance_note": rnote.strip(),
                        "query": item["query"],
                        "date": rec.get("date"),
                        "source": rec.get("source"),
                    },
                )
            )
    results.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in results]


def precedent_items(
    context: str,
    category_id: str,
    cache: bool | AmassCache = True,
    *,
    client: AmassClient | None = None,
    cache_path: Path | str | None = None,
    n: int = 3,
) -> list[dict]:
    """Compact records backing ``precedent()``; ``[]`` if the category is unmapped."""
    q = load_queries()
    item = precedent_query_item(context, category_id, q)
    if item is None:
        return []
    try:
        recs = fetch_records(item["core"], item["query"], _item_filters(item, q["defaults"]), cache=cache, client=client, cache_path=cache_path)
    except AmassError:
        recs = []
    out = []
    for rec in recs[:n]:
        r = dict(rec)
        r["relevance_note"] = item.get("relevance_note") or ""
        r["query"] = item["query"]
        out.append(r)
    return out


def precedent(
    context: str,
    category_id: str,
    cache: bool | AmassCache = True,
    *,
    client: AmassClient | None = None,
    cache_path: Path | str | None = None,
    n: int = 3,
) -> str:
    """Short bullet text for the Stage 3 evaluator prompt, e.g.::

        Domain precedent (interlock_bypass): Operating a BSC through an airflow alarm ...
        - [BiomedCore] Title of paper (2021, J Occup Hyg) https://pubmed...
        - ...

    Returns ``""`` when the category is unmapped or nothing is cached/fetchable.
    """
    q = load_queries()
    item = precedent_query_item(context, category_id, q)
    if item is None:
        return ""
    recs = precedent_items(context, category_id, cache, client=client, cache_path=cache_path, n=n)
    if not recs:
        return ""
    head = f"Domain precedent ({category_id}): {item.get('relevance_note', '').strip()}".rstrip(": ")
    lines = [head]
    for r in recs:
        year = (r.get("date") or "")[:4]
        meta = ", ".join(x for x in [year, (r.get("source") or "")[:60]] if x)
        title = _trim(r.get("title"), 140) or r.get("id", "")
        line = f"- [{r.get('source_core')}] {title}"
        if meta:
            line += f" ({meta})"
        if r.get("url"):
            line += f" {r['url']}"
        lines.append(line)
    return "\n".join(lines)


# --------------------------------------------------------------------------- populate


def populate_cache(
    *,
    client: AmassClient | None = None,
    cache_path: Path | str | None = None,
    refresh: bool = False,
    max_requests: int = 40,
    log: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Fetch every query in queries.yaml into the cache (skipping cached keys unless
    ``refresh``). Stops at ``max_requests``. Returns a stats dict incl. credit totals.
    """
    q = load_queries()
    store = AmassCache(cache_path).load()
    items = all_query_items(q)
    cl = _client_or_none(client)
    if cl is None:
        raise AmassError(401, "AMASS_API_KEY is not set; cannot populate")
    owns = client is None
    fetched = skipped = failed = 0
    errors: list[str] = []
    try:
        for it in items:
            filters = _item_filters(it, q["defaults"])
            key = cache_key(it["core"], it["query"], filters)
            if not refresh and key in store:
                skipped += 1
                continue
            if cl.requests_made >= max_requests:
                log(f"stopping: reached max_requests={max_requests}")
                break
            try:
                recs = fetch_records(it["core"], it["query"], filters, cache=store, client=cl)
                fetched += 1
                log(f"[{cl.requests_made:>2}] {it['core']:<14} {it['query']!r:<70} -> {len(recs)} recs  "
                    f"cost={cl.last_headers.get('x-amass-credit-cost', '?')} remaining={cl.rate_limit_remaining}")
            except AmassError as e:
                failed += 1
                errors.append(f"{it['core']} {it['query']!r}: {e}")
                log(f"FAILED {it['core']} {it['query']!r}: {e}")
    finally:
        if owns:
            cl.close()
    store.save()
    return {
        "items_total": len(items),
        "fetched": fetched,
        "skipped_cached": skipped,
        "failed": failed,
        "errors": errors,
        "requests_made": cl.requests_made,
        "credits_spent": cl.credits_spent,
        "usd_spent": round(cl.credits_spent / 100.0, 4),
        "cache_entries": len(store),
        "cache_path": str(store.path),
    }


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LabWatcher Amass enrichment")
    ap.add_argument("--populate", action="store_true", help="fetch all queries.yaml entries into the cache")
    ap.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    ap.add_argument("--max-requests", type=int, default=40)
    ap.add_argument("--cache", default=None, help="cache path (default labwatcher/data/amass_cache.json)")
    ap.add_argument("--precedent", nargs=2, metavar=("CONTEXT", "CATEGORY"), help="print precedent text")
    ap.add_argument("--enrich", nargs=2, metavar=("CONTEXT", "ENV"), help="print enrich_session JSON")
    args = ap.parse_args(argv)
    if args.populate:
        stats = populate_cache(cache_path=args.cache, refresh=args.refresh, max_requests=args.max_requests, log=print)
        print(json.dumps(stats, indent=2))
        return 1 if stats["failed"] else 0
    if args.precedent:
        print(precedent(args.precedent[0], args.precedent[1], cache_path=args.cache))
        return 0
    if args.enrich:
        print(json.dumps(enrich_session(args.enrich[0], args.enrich[1], cache_path=args.cache), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
