"""TestClient coverage for the LabWatcher UI: every page and every /api route.

Runs against the in-memory fixture store so it never touches labwatcher/data; policy and rules
editors are pointed at a tmp dir so the other engineers' YAML files are never modified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from labwatcher.ui import fixtures
from labwatcher.ui.app import create_app
from labwatcher.ui.fixtures import CONTEXTS, TAXONOMY_IDS, MemoryStore


def _store_kinds():
    kinds = ["memory"]
    try:
        import labwatcher.store  # noqa: F401
        kinds.append("sqlite")
    except Exception:
        pass
    return kinds


@pytest.fixture(params=_store_kinds())
def client(tmp_path: Path, request):
    """App over a seeded store: the in-memory fixture store and (when importable) the real SQLite Store."""
    if request.param == "sqlite":
        from labwatcher.store import Store
        store = Store(":memory:")
    else:
        store = MemoryStore()
    app = create_app(store=store, policy_dir=tmp_path / "policies", rules_dir=tmp_path / "rules", seed=True)
    with TestClient(app) as c:
        c.store = store
        c.tmp = tmp_path
        c.kind = request.param
        yield c


# -- pages ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,marker", [
    ("/", 'data-page="analyzer"'), ("/live", 'data-page="live"'), ("/policy", 'data-page="policy"'),
    ("/rules", 'data-page="rules"'), ("/settings", 'data-page="settings"'),
])
def test_pages_render(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert marker in r.text
    for nav in ("/live", "/policy", "/rules", "/settings", 'id="ctx-switch"'):
        assert nav in r.text


def test_session_page_and_404(client):
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]
    assert client.get(f"/session/{sid}").status_code == 200
    assert 'data-page="session"' in client.get(f"/session/{sid}").text
    assert client.get("/session/does-not-exist").status_code == 404


def test_static_assets(client):
    assert client.get("/static/app.css").status_code == 200
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "initAnalyzer" in js.text


# -- analyzer API ---------------------------------------------------------------------------------

def test_health_and_taxonomy(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["backend"] in ("memory", "custom")
    tax = client.get("/api/taxonomy").json()
    assert [t["id"] for t in tax] == TAXONOMY_IDS


def test_summary_overall_and_per_context(client):
    s = client.get("/api/summary").json()
    assert s["total_sessions"] == len(fixtures.SESSION_PLAN)
    assert s["blocked_actions"] > 0 and s["flagged_sessions"] > 0
    assert 0 < s["failure_rate"] <= 1
    assert [b["id"] for b in s["by_category"]] == TAXONOMY_IDS
    assert sum(b["count"] for b in s["by_category"]) > 0
    assert len(s["trend"]) == 14 and sum(d["sessions"] for d in s["trend"]) == s["total_sessions"]
    per = {c: client.get(f"/api/summary?context={c}").json() for c in CONTEXTS}
    assert sum(p["total_sessions"] for p in per.values()) == s["total_sessions"]
    assert all(p["total_sessions"] > 0 for p in per.values())
    assert client.get("/api/summary?context=nope").status_code == 404


def test_sessions_filters_and_sorting(client):
    all_ = client.get("/api/sessions").json()
    assert all_["count"] == len(fixtures.SESSION_PLAN)
    dd = client.get("/api/sessions?context=drug_discovery").json()["sessions"]
    assert dd and all(s["context"] == "drug_discovery" for s in dd)
    assert all(s["card_title"] for s in dd)
    sev = client.get("/api/sessions?sort=severity").json()["sessions"]
    scores = [s["max_score"] or 0 for s in sev]
    assert scores == sorted(scores, reverse=True)
    asc = client.get("/api/sessions?sort=date&order=asc").json()["sessions"]
    assert [s["started_at"] for s in asc] == sorted(s["started_at"] for s in asc)
    running = client.get("/api/sessions?status=running").json()["sessions"]
    assert len(running) == 2 and all(s["status"] == "running" for s in running)
    flagged = client.get("/api/sessions?flagged=true").json()["sessions"]
    assert flagged and all(s["flagged"] for s in flagged)
    hi = client.get("/api/sessions?min_score=8").json()["sessions"]
    assert hi and all(s["max_score"] >= 8 for s in hi)
    assert client.get("/api/sessions?env=coin_cell").json()["count"] == 5


def test_session_detail_shape(client):
    exploit = next(s for s in client.get("/api/sessions").json()["sessions"] if s["outcome"] == "exploit_blocked")
    d = client.get(f"/api/sessions/{exploit['id']}").json()
    assert set(d) >= {"session", "actions", "transcript", "trailing", "human_decisions", "enrichment"}
    acts = d["actions"]
    assert [a["seq"] for a in acts] == sorted(a["seq"] for a in acts)
    assert all(isinstance(a["args"], dict) and isinstance(a["categories"], list) for a in acts)
    denied = [a for a in acts if a["decision"] == "deny"]
    assert denied and all(a["categories"] for a in denied)
    assert any(a["stage"] == "rules" and a["rule_id"] for a in acts)
    assert all(a["latency_ms"] is not None and a["ts"] for a in acts)
    assert any(a["evaluator"] and a["triage"] for a in acts)
    assert d["trailing"] and all(set(t["scores"]) == set(TAXONOMY_IDS) for t in d["trailing"])
    assert any(t["suggestion"] for t in d["trailing"])
    assert d["human_decisions"] and d["enrichment"]
    assert all(isinstance(e["result"], list) and e["result"][0]["title"] for e in d["enrichment"])
    assert any(m["role"] == "system" and "<system-reminder>" in m["content"] for m in d["transcript"])
    assert client.get("/api/sessions/missing").status_code == 404


def test_catalog(client):
    c = client.get("/api/catalog").json()
    assert c["contexts"] == CONTEXTS
    envs = c["catalog"]["drug_discovery"]["envs"]
    assert set(envs) >= {"aspirin", "cell_culture", "cytotox"}
    assert any(card["id"] == "a02" for card in envs["aspirin"])
    assert "coin_cell" in c["catalog"]["materials_discovery"]["envs"]
    assert c["catalog"]["materials_discovery"]["envs"]["coin_cell"]


# -- live API ------------------------------------------------------------------------------------------

def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        ev = {"event": "message", "data": ""}
        for line in block.splitlines():
            if line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                ev["data"] += line[5:].strip()
            elif line.startswith("id:"):
                ev["id"] = int(line[3:].strip())
        if ev["data"]:
            ev["data"] = json.loads(ev["data"])
            events.append(ev)
    return events


def test_live_events_sse(client):
    r = client.get("/api/live/events?context=drug_discovery&once=1&backlog=10")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    kinds = [e["event"] for e in events]
    assert kinds.count("action") == 10 and "escalations" in kinds and "hello" in kinds
    acts = [e for e in events if e["event"] == "action"]
    assert all(e["data"]["context"] == "drug_discovery" for e in acts)
    assert [e["id"] for e in acts] == sorted(e["id"] for e in acts)
    pend = next(e for e in events if e["event"] == "escalations")["data"]
    assert len(pend) == 1 and pend[0]["decision"] == "escalate"
    # resume from the last id gives nothing new
    last = acts[-1]["id"]
    tail = _parse_sse(client.get(f"/api/live/events?context=drug_discovery&once=1&after={last}").text)
    assert not [e for e in tail if e["event"] == "action"]


def test_escalation_approve_and_deny(client):
    pend = client.get("/api/escalations").json()["pending"]
    assert len(pend) == 2 and {p["context"] for p in pend} == set(CONTEXTS)
    a, b = pend
    r = client.post(f"/api/escalations/{a['id']}", json={"decision": "approve", "note": "ok under supervision"})
    assert r.status_code == 200 and r.json()["decision"] == "approve" and r.json()["verdict"] == "allow"
    assert all(p["id"] != a["id"] for p in r.json()["pending"])
    r = client.post(f"/api/escalations/{b['id']}", json={"decision": "deny"})
    assert r.status_code == 200
    assert client.get("/api/escalations").json()["pending"] == []
    d = client.get(f"/api/sessions/{a['session_id']}").json()
    assert any(h["action_id"] == a["id"] and h["decision"] == "allow" and h["note"] for h in d["human_decisions"])
    resolved = next(x for x in d["actions"] if x["id"] == a["id"])
    assert resolved["decision"] == "allow" and resolved["stage"] == "human"  # resolve_escalation updated the action
    assert client.post(f"/api/escalations/{a['id']}", json={"decision": "maybe"}).status_code == 400
    assert client.post("/api/escalations/999999", json={"decision": "deny"}).status_code == 404
    allowed = next(x for x in d["actions"] if x["decision"] == "allow" and x["stage"] != "human")
    assert client.post(f"/api/escalations/{allowed['id']}", json={"decision": "deny"}).status_code == 409


def test_demo_run_validation_and_501_or_202(client, monkeypatch):
    bad = client.post("/api/demo/run", json={"context": "drug_discovery", "env": "nope", "card": "a02", "script": "honest"})
    assert bad.status_code == 400
    assert client.post("/api/demo/run", json={"context": "x", "env": "aspirin"}).status_code == 404
    assert client.post("/api/demo/run", json={"context": "drug_discovery", "env": "aspirin", "card": "a02", "script": "weird"}).status_code == 400
    r = client.post("/api/demo/run", json={"context": "drug_discovery", "env": "aspirin", "card": "a02",
                                           "script": "exploit", "provider": "mock"})
    try:
        import labwatcher.demo  # noqa: F401
        have_demo = True
    except Exception:
        have_demo = False
    if have_demo:
        assert r.status_code == 202 and r.json()["status"] == "running"
    else:
        assert r.status_code == 501 and "run_demo" in r.json()["detail"]
    jobs = client.get("/api/demo/jobs").json()
    assert "jobs" in jobs and jobs["demo_available"] is have_demo


def test_demo_run_with_fake_runner(client, monkeypatch):
    import sys, types, time
    fake = types.ModuleType("labwatcher.demo")
    calls = []

    def run_demo(context, env, card_id, script, provider="mock", store=None):
        calls.append((context, env, card_id, script, provider, store))
        return "fake-session"
    fake.run_demo = run_demo
    monkeypatch.setitem(sys.modules, "labwatcher.demo", fake)
    r = client.post("/api/demo/run", json={"context": "materials_discovery", "env": "coin_cell", "card": "m01",
                                           "script": "honest", "provider": "mock"})
    assert r.status_code == 202
    job_id = r.json()["id"]
    for _ in range(50):
        job = next(j for j in client.get("/api/demo/jobs").json()["jobs"] if j["id"] == job_id)
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done" and job["session_id"] == "fake-session"
    assert calls == [("materials_discovery", "coin_cell", "m01", "honest", "mock", client.store)]


# -- policy --------------------------------------------------------------------------------------------

def test_policy_get_defaults_and_save(client):
    p = client.get("/api/policy/drug_discovery").json()
    assert p["exists"] is False
    assert set(p["policy"]) == {"triage_system", "evaluator_system", "trailing_system", "suggestion_template"}
    r = client.post("/api/policy/drug_discovery", json={"triage_system": "NEW TRIAGE\nline two"})
    assert r.status_code == 200 and r.json()["exists"] is True
    on_disk = yaml.safe_load((client.tmp / "policies" / "drug_discovery.yaml").read_text())
    assert on_disk["triage_system"] == "NEW TRIAGE\nline two"
    assert on_disk["evaluator_system"] == p["policy"]["evaluator_system"]  # untouched fields preserved
    assert client.get("/api/policy/drug_discovery").json()["policy"]["triage_system"] == "NEW TRIAGE\nline two"
    # other context untouched; validation
    assert client.get("/api/policy/materials_discovery").json()["exists"] is False
    assert client.post("/api/policy/drug_discovery", json={"bogus": "x"}).status_code == 400
    assert client.post("/api/policy/drug_discovery", json={"triage_system": ""}).status_code == 400
    assert client.get("/api/policy/nope").status_code == 404


def test_policy_preserves_extra_keys(client):
    pdir = client.tmp / "policies"
    pdir.mkdir()
    (pdir / "materials_discovery.yaml").write_text(yaml.safe_dump({"triage_system": "t", "taxonomy_wording": {"interlock_bypass": "x"}}))
    r = client.post("/api/policy/materials_discovery", json={"evaluator_system": "e"})
    assert r.status_code == 200 and r.json()["extra"] == {"taxonomy_wording": {"interlock_bypass": "x"}}
    on_disk = yaml.safe_load((pdir / "materials_discovery.yaml").read_text())
    assert on_disk["taxonomy_wording"] == {"interlock_bypass": "x"} and on_disk["triage_system"] == "t"


# -- rules ----------------------------------------------------------------------------------------------

def test_rules_crud_and_validation(client):
    r = client.get("/api/rules/drug_discovery").json()
    assert r["exists"] is False and r["count"] > 0
    assert r["rules"] == sorted(r["rules"], key=lambda x: -x["priority"])
    rule = {"id": "no_curl", "match": {"tool": "^instrument$", "command": "^purge_"}, "decision": "escalate_human",
            "priority": 77, "reason": "Purging shared gas lines needs a human."}
    r = client.post("/api/rules/drug_discovery", json=rule)
    assert r.status_code == 201, r.text
    path = client.tmp / "rules" / "drug_discovery.yaml"
    on_disk = yaml.safe_load(path.read_text())
    assert isinstance(on_disk, list) and any(x["id"] == "no_curl" for x in on_disk)
    assert client.post("/api/rules/drug_discovery", json=rule).status_code == 409
    # invalid regex / decision / match
    bad = client.post("/api/rules/drug_discovery", json={"id": "bad", "match": {"command": "("}, "decision": "deny"})
    assert bad.status_code == 400 and "match.command" in bad.json()["errors"]
    bad = client.post("/api/rules/drug_discovery", json={"id": "bad", "match": {"command": "x"}, "decision": "nuke"})
    assert bad.status_code == 400 and "decision" in bad.json()["errors"]
    bad = client.post("/api/rules/drug_discovery", json={"id": "bad", "match": {}, "decision": "deny"})
    assert bad.status_code == 400 and "match" in bad.json()["errors"]
    bad = client.post("/api/rules/drug_discovery", json={"id": "bad", "match": {"nope": "x"}, "decision": "deny"})
    assert bad.status_code == 400 and "match.nope" in bad.json()["errors"]
    # edit
    r = client.put("/api/rules/drug_discovery/no_curl", json=dict(rule, priority=99, reason="edited"))
    assert r.status_code == 200 and r.json()["priority"] == 99
    assert next(x for x in client.get("/api/rules/drug_discovery").json()["rules"] if x["id"] == "no_curl")["reason"] == "edited"
    assert client.put("/api/rules/drug_discovery/ghost", json=rule).status_code == 404
    # dry run picks the highest priority match
    t = client.get("/api/rules/drug_discovery/test?tool=instrument&command=purge_line").json()
    assert t["winner"]["id"] == "no_curl"
    t = client.get("/api/rules/drug_discovery/test?tool=instrument&command=silence_alarm").json()
    assert t["winner"]["decision"] == "deny"
    # delete
    assert client.delete("/api/rules/drug_discovery/no_curl").status_code == 200
    assert client.delete("/api/rules/drug_discovery/no_curl").status_code == 404
    assert not any(x["id"] == "no_curl" for x in yaml.safe_load(path.read_text()))
    # materials rules file untouched
    assert not (client.tmp / "rules" / "materials_discovery.yaml").exists()


def test_rules_wrapper_mapping_preserved(client):
    rdir = client.tmp / "rules"
    rdir.mkdir()
    (rdir / "materials_discovery.yaml").write_text(yaml.safe_dump({"version": 1, "rules": [
        {"id": "r1", "match": {"tool": "^finish$"}, "decision": "allow", "priority": 1, "reason": ""}]}))
    r = client.post("/api/rules/materials_discovery", json={"id": "r2", "match": {"path": "^config/"}, "decision": "escalate_triage", "priority": 5})
    assert r.status_code == 201
    on_disk = yaml.safe_load((rdir / "materials_discovery.yaml").read_text())
    assert on_disk["version"] == 1 and [x["id"] for x in on_disk["rules"]] == ["r1", "r2"]


# -- settings -------------------------------------------------------------------------------------------

def test_settings_view(client):
    v = client.get("/api/settings").json()
    assert isinstance(v["effective"], dict) and v["effective"]
    assert "tools" in v["effective"] or "triage" in v["effective"]
    assert isinstance(v["locks"], dict) and v["locks"]
    assert isinstance(v["errors"], list) and isinstance(v["warnings"], list)
    assert v["source"] in ("labwatcher.settings", "settings.yaml", "built-in defaults")


# -- fixtures / store -----------------------------------------------------------------------------------

def test_memory_store_contract():
    """MemoryStore mirrors the real Store's calling convention (decision dict, auto idx, verdicts)."""
    s = MemoryStore()
    sid = s.create_session("drug_discovery", "aspirin", card="a02", id="sess-1")
    assert sid == "sess-1"
    aid = s.add_action(sid, 1, "instrument", args={"name": "fume_hood", "command": "silence_alarm"},
                       decision={"action": "escalate", "stage": "human", "score": 8, "reason": "r",
                                 "categories": ["interlock_bypass"], "latency_ms": 5})
    a = s.get_action(aid)
    assert a["instrument"] == "fume_hood" and a["command"] == "silence_alarm" and a["decision"] == "escalate"
    assert s.add_transcript(sid, "user", "hi") and s.transcript(sid)[0]["idx"] == 0
    s.add_transcript(sid, [{"role": "assistant", "content": "ok"}])
    assert [t["idx"] for t in s.transcript(sid)] == [0, 1]
    s.add_trailing(sid, 1, {c: 1 for c in TAXONOMY_IDS} | {"interlock_bypass": 8}, reason="x")
    s.add_enrichment(sid, "BiomedCore", "q", [{"title": "t"}])
    assert s.pending_escalations() and s.pending_escalations()[0]["id"] == aid
    assert s.pending_escalations()[0]["context"] == "drug_discovery"
    resolved = s.resolve_escalation(aid, "approve", note="fine")
    assert resolved["decision"] == "allow" and resolved["stage"] == "human" and "[human: fine]" in resolved["reason"]
    assert s.pending_escalations() == [] and s.human_decisions(sid)[0]["decision"] == "allow"
    d = s.session(sid)
    assert set(d) == {"session", "actions", "transcript", "trailing", "human_decisions", "enrichment"}
    assert d["session"]["flagged"] and d["session"]["escalated_count"] == 1 and d["session"]["max_score"] == 8
    assert d["trailing"][0]["top_category"] == "interlock_bypass" and d["trailing"][0]["max_score"] == 8
    s.end_session(sid, outcome="honest")
    assert s.sessions(status="completed")[0]["id"] == sid
    assert s.summary()["sessions"] == 1 and s.summary("materials_discovery")["sessions"] == 0
    assert s.summary()["by_category"] == {"interlock_bypass": 1}  # from the hot trailing window only
    assert s.actions_since(0)[0]["id"] == aid and s.actions_since(aid) == []
    with pytest.raises(ValueError):
        s.resolve_escalation(aid, "maybe")
    with pytest.raises(KeyError):
        s.resolve_escalation(999, "deny")


def test_memory_store_matches_sqlite_store_on_seed():
    try:
        from labwatcher.store import Store
    except Exception:
        pytest.skip("labwatcher.store not available")
    mem, sql = MemoryStore(), Store(":memory:")
    fixtures.seed_demo(mem)
    fixtures.seed_demo(sql)
    km = {k: v for k, v in mem.summary().items() if k not in ("trend",)}
    ks = {k: v for k, v in sql.summary().items() if k not in ("trend",)}
    assert km == ks
    assert [s["id"] for s in mem.sessions(sort="severity")] == [s["id"] for s in sql.sessions(sort="severity")]
    sid = mem.sessions()[0]["id"]
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("id", "ts")} for r in rows]
    assert strip(mem.actions(sid)) == strip(sql.actions(sid))
    assert strip(mem.transcript(sid)) == strip(sql.transcript(sid))
    assert strip(mem.trailing(sid)) == strip(sql.trailing(sid))
    assert [p["session_id"] for p in mem.pending_escalations()] == [p["session_id"] for p in sql.pending_escalations()]


def test_seed_is_deterministic_and_idempotent_via_is_empty():
    a, b = MemoryStore(), MemoryStore()
    ids_a, ids_b = fixtures.seed_demo(a), fixtures.seed_demo(b)
    assert ids_a == ids_b and len(ids_a) == len(fixtures.SESSION_PLAN)
    assert a.summary() == b.summary()
    assert not fixtures.is_empty(a)
