"""Tests for labwatcher.enrich.amass.

Everything here runs offline from labwatcher/data/amass_cache.json plus httpx
MockTransport. Live tests run only with AMASS_LIVE=1 (and AMASS_API_KEY set).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from labwatcher.enrich import amass
from labwatcher.enrich.amass import (
    AmassCache,
    AmassClient,
    AmassError,
    MAX_LIMIT,
    cache_key,
    enrich_session,
    load_queries,
    precedent,
    precedent_items,
    summarise_record,
)

CATEGORIES = [
    "interlock_bypass",
    "record_tampering",
    "data_fabrication",
    "unapproved_substitution",
    "hazard_release",
    "infrastructure_disruption",
    "sample_integrity",
    "scope_overreach",
    "prompt_injection",
]
CONTEXTS = ["drug_discovery", "materials_discovery"]
ENVS = [
    ("drug_discovery", "aspirin"),
    ("drug_discovery", "cell_culture"),
    ("drug_discovery", "cytotox"),
    ("materials_discovery", "coin_cell"),
]

RESULT_KEYS = {"source_core", "title", "id", "url", "snippet", "relevance_note"}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Guarantee no network: strip the key and make any real client construction fail."""
    if os.environ.get("AMASS_LIVE") != "1":
        monkeypatch.delenv("AMASS_API_KEY", raising=False)
        def _boom(self, request):  # pragma: no cover - only hit on a bug
            raise AssertionError(f"network call attempted offline: {request.url}")
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _boom)
    yield


def _json_response(status: int, body, headers: dict | None = None) -> httpx.Response:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return httpx.Response(status, json=body, headers=h)


def _ok_headers(remaining=50, cost="5", reset="2030-01-01T00:00:00Z"):
    return {
        "X-RateLimit-Limit": "60",
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": reset,
        "X-Amass-Credit-Cost": cost,
    }


class Recorder:
    """Collects requests and hands back a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        r = self.responses.pop(0)
        return r(request) if callable(r) else r


def make_client(responses, **kw) -> tuple[AmassClient, Recorder, list[float]]:
    rec = Recorder(responses)
    sleeps: list[float] = []
    cl = AmassClient("amass_test_key", transport=httpx.MockTransport(rec), sleep=sleeps.append, **kw)
    return cl, rec, sleeps


BIOMED_REC = {
    "amassId": "AMBC_x1",
    "pmid": "123",
    "doi": "10.1/x",
    "url": None,
    "title": "A paper",
    "abstract": "word " * 200,
    "journal": "J Test",
    "publicationDate": "2020-05-01",
    "citationCount": 3,
    "isRetracted": False,
}


# --------------------------------------------------------------------------- queries.yaml


def test_queries_yaml_covers_spec_envs_and_categories():
    q = load_queries()
    envs = q["envs"]
    # exact queries from the SPEC / task brief
    asp = [(i["core"], i["query"]) for i in envs["drug_discovery"]["aspirin"]]
    assert ("DrugCore", "aspirin") in asp and ("DrugCore", "acetylsalicylic acid") in asp
    for bq in ["acetic anhydride laboratory safety", "analytical balance calibration GLP", "melting point purity aspirin"]:
        assert ("BiomedCore", bq) in asp
    cc = {i["query"] for i in envs["drug_discovery"]["cell_culture"]}
    assert cc == {"HepG2 passage number phenotype drift", "mycoplasma contamination cell culture detection", "biosafety cabinet airflow failure"}
    assert all(i["core"] == "BiomedCore" for i in envs["drug_discovery"]["cell_culture"])
    ct = {i["query"] for i in envs["drug_discovery"]["cytotox"]}
    assert ct == {"CellTiter-Glo Z-factor assay quality", "staurosporine reference compound cytotoxicity", "edge effect evaporation 384-well"}
    assert not any(i["core"] == "RegulatoryCore" for i in envs["drug_discovery"]["cytotox"])
    coin = [(i["core"], i["query"]) for i in envs["materials_discovery"]["coin_cell"]]
    assert ("PatentCore", "lithium iron phosphate coin cell formation protocol") in coin
    assert ("PatentCore", "glovebox oxygen sensor interlock") in coin
    for bq in ["lithium-ion thermal runaway overcharge", "LiPF6 electrolyte decomposition moisture", "coin cell coulombic efficiency reproducibility"]:
        assert ("BiomedCore", bq) in coin
    # precedent map: every category x context
    for cat in CATEGORIES:
        assert cat in q["precedents"], cat
        for ctx in CONTEXTS:
            item = q["precedents"][cat][ctx]
            assert item["core"] and item["query"] and item["relevance_note"]
    # every item has a relevance note
    for ctx, env_map in envs.items():
        for env, items in env_map.items():
            for it in items:
                assert it.get("relevance_note"), (ctx, env, it)


def test_all_query_items_is_small_budget():
    items = amass.all_query_items()
    assert 15 <= len(items) <= 40


def test_load_queries_rejects_bad_item(tmp_path):
    bad = tmp_path / "q.yaml"
    bad.write_text("envs:\n  drug_discovery:\n    aspirin:\n      - core: NopeCore\n        query: x\n")
    with pytest.raises(ValueError):
        load_queries(bad)
    bad.write_text("envs:\n  drug_discovery:\n    aspirin:\n      - core: BiomedCore\n")
    with pytest.raises(ValueError):
        load_queries(bad)


# --------------------------------------------------------------------------- cache key / params


def test_cache_key_normalises_core_and_limit():
    k1 = cache_key("biomedcore", " aspirin ", {"limit": 50})
    k2 = cache_key("BiomedCore", "aspirin", {"limit": 5})
    k3 = cache_key("BiomedCore", "aspirin")
    assert k1 == k2 == k3
    assert cache_key("BiomedCore", "aspirin", {"minCitationCount": 10}) != k3
    assert cache_key("DrugCore", "aspirin") != k3
    with pytest.raises(ValueError):
        cache_key("FooCore", "x")


def test_build_params_clamps_limit_and_encodes_filters():
    p = AmassClient._build_params("BiomedCore", "q", {"limit": 300, "isRetracted": False, "authorNames": ["A", "B"], "skip": None})
    assert ("limit", str(MAX_LIMIT)) in p
    assert ("isRetracted", "false") in p
    assert ("authorNames", "A") in p and ("authorNames", "B") in p
    assert not any(k == "skip" for k, _ in p)
    # PatentCore joins multi-values with commas (except include)
    p = AmassClient._build_params("PatentCore", "q", {"countryCode": ["US", "EP"], "include": ["claims", "description"], "limit": 0})
    assert ("countryCode", "US,EP") in p
    assert ("include", "claims") in p and ("include", "description") in p
    assert ("limit", "1") in p


# --------------------------------------------------------------------------- client


def test_client_requires_key(monkeypatch):
    monkeypatch.delenv("AMASS_API_KEY", raising=False)
    with pytest.raises(AmassError) as ei:
        AmassClient()
    assert ei.value.status == 401


def test_client_search_sends_bearer_and_unwraps_data():
    cl, rec, sleeps = make_client([_json_response(200, {"data": [BIOMED_REC]}, _ok_headers(remaining=57, cost="5"))])
    out = cl.search("biomedcore", "aspirin", limit=99, minCitationCount=10)
    assert out == [BIOMED_REC]
    req = rec.requests[0]
    assert req.headers["Authorization"] == "Bearer amass_test_key"
    assert req.url.path == "/api/v1/cores/biomedcore/records"
    assert req.url.params["query"] == "aspirin"
    assert req.url.params["limit"] == "5"  # clamped
    assert req.url.params["minCitationCount"] == "10"
    assert cl.requests_made == 1
    assert cl.credits_spent == 5.0
    assert cl.rate_limit_remaining == 57
    assert sleeps == []


def test_client_get_by_id():
    cl, rec, _ = make_client([_json_response(200, {"data": {"amassId": "AMDC_1", "name": "ASPIRIN"}}, _ok_headers(cost="1"))])
    out = cl.get("DrugCore", "AMDC_1", include=["parent"])
    assert out["name"] == "ASPIRIN"
    assert rec.requests[0].url.path == "/api/v1/cores/drugcore/records/AMDC_1"
    assert rec.requests[0].url.params["include"] == "parent"
    assert cl.credits_spent == 1.0


def test_client_backs_off_on_429_then_succeeds():
    cl, rec, sleeps = make_client([
        _json_response(429, {"error": {"status": 429, "code": "TOO_MANY_REQUESTS", "message": "slow"}}, {"Retry-After": "3", "X-RateLimit-Remaining": "0"}),
        _json_response(200, {"data": []}, _ok_headers()),
    ])
    assert cl.search("BiomedCore", "x") == []
    assert len(rec.requests) == 2
    assert sleeps == [3.0]


def test_client_429_retries_exhausted_raises():
    r429 = _json_response(429, {"error": {"message": "slow"}}, {"Retry-After": "1"})
    cl, rec, sleeps = make_client([r429, r429, r429], max_retries=2)
    with pytest.raises(AmassError) as ei:
        cl.search("BiomedCore", "x")
    assert ei.value.status == 429
    assert len(sleeps) == 2


def test_client_paces_when_remaining_is_low():
    cl, rec, sleeps = make_client([
        _json_response(200, {"data": []}, _ok_headers(remaining=1, reset="2000-01-01T00:00:00Z")),
        _json_response(200, {"data": []}, _ok_headers(remaining=59)),
    ], min_remaining=2)
    cl.search("BiomedCore", "a")
    assert sleeps == []
    cl.search("BiomedCore", "b")  # remaining (1) <= min_remaining (2) -> proactive wait
    assert len(sleeps) == 1 and sleeps[0] > 0
    assert cl.rate_limit_remaining == 59


def test_client_5xx_retries_once_then_ok():
    cl, rec, sleeps = make_client([
        _json_response(500, {"error": {"message": "boom"}}),
        _json_response(200, {"data": []}, _ok_headers()),
    ])
    assert cl.search("BiomedCore", "x") == []
    assert len(sleeps) == 1


def test_client_400_raises_with_fields():
    cl, rec, _ = make_client([
        _json_response(400, {"error": {"status": 400, "code": "BAD_REQUEST", "message": "Validation failed", "fields": {"limit": "bad"}}}),
    ])
    with pytest.raises(AmassError) as ei:
        cl.search("BiomedCore", "x")
    assert ei.value.status == 400
    assert "limit" in str(ei.value)


def test_client_rejects_bad_core_and_empty_query():
    cl, _, _ = make_client([])
    with pytest.raises(ValueError):
        cl.search("FooCore", "x")
    with pytest.raises(ValueError):
        cl.search("BiomedCore", "   ")


# --------------------------------------------------------------------------- summarise_record


def test_summarise_record_shapes():
    b = summarise_record("BiomedCore", BIOMED_REC)
    assert b["id"] == "AMBC_x1" and b["url"] == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert len(b["snippet"]) <= 400 and b["snippet"].endswith("…")
    d = summarise_record("DrugCore", {"amassId": "AMDC_1", "name": "ASPIRIN", "chemblId": "CHEMBL25", "description": "d", "synonyms": ["a"]})
    assert d["title"] == "ASPIRIN" and "CHEMBL25" in d["url"] and d["chembl_id"] == "CHEMBL25"
    p = summarise_record("PatentCore", {"amassId": "AMPC_1", "title": "T", "publicationNumber": "US-1-B2", "assignees": ["ACME"], "priorityDate": "2019-01-01"})
    assert p["url"] == "https://patents.google.com/patent/US1B2" and p["date"] == "2019-01-01" and p["source"] == "ACME"
    r = summarise_record("RegulatoryCore", {"amassId": "AMRC_1", "name": "X", "agency": "FDA", "documentSections": [{"matchedText": "hit"}]})
    assert r["snippet"] == "hit" and r["source"] == "FDA"


# --------------------------------------------------------------------------- cache file


def test_cache_file_has_every_query_with_records():
    store = AmassCache().load()
    assert store.path.exists(), "run: python -m labwatcher.enrich.amass --populate"
    q = load_queries()
    for it in amass.all_query_items(q):
        filters = amass._item_filters(it, q["defaults"])
        recs = store.get(it["core"], it["query"], filters)
        assert recs is not None, f"missing cache entry for {it['core']} {it['query']!r}"
        assert 1 <= len(recs) <= MAX_LIMIT
        for r in recs:
            assert r["id"] and r["title"] and r["source_core"] == amass.normalize_core(it["core"])


def test_cache_roundtrip_and_miss(tmp_path):
    path = tmp_path / "c.json"
    store = AmassCache(path)
    assert store.get("BiomedCore", "x") is None
    store.put("BiomedCore", "x", {"limit": 5}, [{"id": "AMBC_1", "title": "t"}], credit_cost=5)
    store.save()
    again = AmassCache(path)
    assert again.get("biomedcore", "x", {"limit": 50}) == [{"id": "AMBC_1", "title": "t"}]
    raw = json.loads(path.read_text())
    assert raw["version"] == 1 and len(raw["entries"]) == 1


def test_fetch_records_miss_without_key_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("AMASS_API_KEY", raising=False)
    assert amass.fetch_records("BiomedCore", "never cached", cache_path=tmp_path / "c.json") == []


def test_fetch_records_writes_through_cache_with_mock_client(tmp_path):
    path = tmp_path / "c.json"
    cl, rec, _ = make_client([_json_response(200, {"data": [BIOMED_REC]}, _ok_headers(cost="5"))])
    out = amass.fetch_records("BiomedCore", "aspirin", {"limit": 5}, client=cl, cache_path=path)
    assert out[0]["id"] == "AMBC_x1"
    # second call: served from cache, no new request (Recorder would raise)
    out2 = amass.fetch_records("BiomedCore", "aspirin", {"limit": 5}, client=cl, cache_path=path)
    assert out2 == out
    assert len(rec.requests) == 1
    entry = json.loads(path.read_text())["entries"][cache_key("BiomedCore", "aspirin")]
    assert entry["credit_cost"] == 5.0


# --------------------------------------------------------------------------- enrich_session (offline)


@pytest.mark.parametrize("context,env", ENVS)
def test_enrich_session_offline_every_env(context, env):
    out = enrich_session(context, env, card_title="", keywords=[])
    assert out, f"no enrichment for {context}/{env}"
    for r in out:
        assert RESULT_KEYS <= set(r)
        assert r["id"] and r["title"] and r["url"].startswith("http")
        assert r["relevance_note"]
    cores = {r["source_core"] for r in out}
    if env == "aspirin":
        assert cores == {"DrugCore", "BiomedCore"}
    elif env == "coin_cell":
        assert cores == {"PatentCore", "BiomedCore"}
    else:
        assert cores == {"BiomedCore"}
    assert len({r["id"] for r in out}) == len(out)  # deduped


def test_enrich_session_keyword_ranking():
    out = enrich_session("drug_discovery", "aspirin", card_title="Aspirin synthesis", keywords=["acetic anhydride"])
    assert out
    top = out[0]
    assert "Matches:" in top["relevance_note"]
    hay = (top["title"] + " " + top["snippet"]).lower()
    assert "acetic anhydride" in hay or "aspirin" in hay
    # unmatched entries sort after matched ones
    matched = [("Matches:" in r["relevance_note"]) for r in out]
    assert matched == sorted(matched, reverse=True)


def test_enrich_session_unknown_env_is_empty():
    assert enrich_session("drug_discovery", "nope") == []
    assert enrich_session("nope", "aspirin") == []


# --------------------------------------------------------------------------- precedent (offline)


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("category", CATEGORIES)
def test_precedent_every_category_offline(context, category):
    text = precedent(context, category)
    assert text.startswith(f"Domain precedent ({category}):")
    lines = text.splitlines()
    assert 2 <= len(lines) <= 4
    for line in lines[1:]:
        assert line.startswith("- [") and "http" in line
    items = precedent_items(context, category)
    assert 1 <= len(items) <= 3 and all(i["relevance_note"] for i in items)


def test_precedent_unmapped_is_empty():
    assert precedent("drug_discovery", "not_a_category") == ""
    assert precedent_items("materials_discovery", "not_a_category") == []


# --------------------------------------------------------------------------- live (opt-in)


@pytest.mark.skipif(os.environ.get("AMASS_LIVE") != "1" or not os.environ.get("AMASS_API_KEY"), reason="set AMASS_LIVE=1 and AMASS_API_KEY")
def test_live_search_small():
    with AmassClient() as cl:
        out = cl.search("DrugCore", "aspirin", limit=2)
    assert out and out[0]["amassId"].startswith("AMDC_")
    assert cl.credits_spent > 0
    assert cl.rate_limit_remaining is not None
