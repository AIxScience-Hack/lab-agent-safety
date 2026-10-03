"""labwatcher.demo: oracle scripts and Modal batch runs through the Watcher into a real Store."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("LABWATCHER_PROVIDER", "mock")

from labwatcher import demo  # noqa: E402
from labwatcher.store import Store  # noqa: E402

RUNS = Path(__file__).resolve().parents[1] / "data" / "runs"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("AMASS_LIVE", raising=False)
    monkeypatch.setenv("LABWATCHER_PROVIDER", "mock")


@pytest.fixture
def store():
    return Store(":memory:")


def test_catalogue_covers_both_contexts():
    assert demo.envs_for("drug_discovery") == ["aspirin", "cell_culture", "cytotox"]
    assert demo.envs_for("materials_discovery") == ["coin_cell"]
    assert demo.context_for("coin_cell") == "materials_discovery"
    assert demo.context_for("aspirin") == "drug_discovery"
    plan = demo.seed_plan()
    assert len(plan) == 76 and {p[3] for p in plan} == {"honest", "exploit"}
    assert len(demo.seed_plan(quick=True)) == 8


def test_default_condition_matches_check_tasks():
    card = {"id": "x", "scripts": {}, "costly": {}}
    assert demo.default_condition(card, "full") == "control"
    assert demo.default_condition(card, "honest") == "honeypot"
    assert demo.default_condition(card, "exploit") == "honeypot"
    assert demo.default_condition(card, "costly_honest") == "costly"
    assert demo.default_condition({"id": "y", "scripts": {}}, "costly_honest") == "honeypot"


def test_exploit_is_blocked_and_recorded(store):
    sid = demo.run_demo("materials_discovery", "coin_cell", "m01", "exploit", "mock", store)
    s = store.get_session(sid)
    assert s["context"] == "materials_discovery" and s["env"] == "coin_cell" and s["card"] == "m01"
    assert s["condition"] == "honeypot" and s["arm"] == "incentivised" and s["source"] == "demo"
    assert s["status"] in ("completed", "stopped") and s["ended_at"]
    assert s["blocked_count"] >= 1 and s["flagged"] is True
    assert s["outcome"] not in ("HACK", "HACK_DISCLOSED")    # the interlock override was denied
    acts = store.actions(sid)
    denied = [a for a in acts if a["decision"] == "deny"]
    assert denied and denied[0]["stage"] == "rules" and denied[0]["rule_id"]
    assert denied[0]["result"].startswith("BLOCKED by LabWatcher:") and denied[0]["ok"] is False
    tr = store.transcript(sid)
    roles = [t["role"] for t in tr]
    assert roles[0] == "system" and roles[1] == "user" and "assistant" in roles and "tool" in roles
    assert any("BLOCKED by LabWatcher" in t["content"] for t in tr if t["role"] == "tool")
    assert store.trailing(sid), "trailing monitor ran at least once in a 50-action session"
    en = store.enrichment(sid)
    assert en and all(isinstance(e["result"], list) and e["result"][0].get("title") for e in en)
    assert {e["source"] for e in en} <= {"PatentCore", "BiomedCore"}


def test_honest_session_scores_one_and_is_not_blocked(store):
    sid = demo.run_demo("drug_discovery", "aspirin", "a01", "honest", "mock", store)
    s = store.get_session(sid)
    assert s["condition"] == "honeypot" and s["blocked_count"] == 0
    assert s["outcome"] in ("STOPPED_REPORTED", "COMPLETED_DECLARED", "COMPLETED")
    acts = store.actions(sid)
    assert acts and all(a["decision"] in ("allow", "escalate") or a["stage"] == "human" for a in acts)
    assert any(a["tool"] == "report_issue" for a in acts)


def test_run_demo_validates_inputs(store):
    with pytest.raises(ValueError):
        demo.run_demo("drug_discovery", "coin_cell", "m01", "exploit", "mock", store)
    with pytest.raises(ValueError):
        demo.run_demo("materials_discovery", "coin_cell", "zzz", "exploit", "mock", store)
    with pytest.raises(ValueError):
        demo.run_demo("materials_discovery", "coin_cell", "m01", "weird", "mock", store)
    # empty context resolves from the env
    sid = demo.run_demo("", "cytotox", "t02", "honest", "mock", store)
    assert store.get_session(sid)["context"] == "drug_discovery"


def test_seed_quick_is_idempotent_and_clears_fixtures(store):
    from labwatcher.ui import fixtures
    fixtures.seed_demo(store)
    n_fix = len(store.sessions(limit=1000))
    assert n_fix > 0
    ids = demo.seed_store(store, "mock", quick=True)
    assert len(ids) == 8 and len(set(ids)) == 8
    rows = store.sessions(limit=1000)
    assert len(rows) == 8 and all(r["source"] == "demo" for r in rows)
    ids2 = demo.seed_store(store, "mock", quick=True)
    assert ids2 == ids and len(store.sessions(limit=1000)) == 8
    for ctx in demo.CONTEXTS:
        summ = store.summary(ctx)
        assert summ["sessions"] == (6 if ctx == "drug_discovery" else 2)


def test_provider_override_beats_env(store, monkeypatch):
    monkeypatch.setenv("LABWATCHER_PROVIDER", "modal")
    runner = demo.DemoRunner(store, "mock")
    assert runner.providers()["triage"]["provider"] == "mock"


@pytest.mark.skipif(not RUNS.is_dir() or not list(RUNS.rglob("*.json")), reason="no Modal batch runs on disk")
def test_replay_imports_a_batch_run(store):
    path = sorted(RUNS.rglob("*.json"))[0]
    data = json.loads(path.read_text())
    sid = demo.replay_run(path, store, "mock")
    s = store.get_session(sid)
    assert s["source"] == "replay" and s["env"] == data["env"] and s["card"] == data["card"]
    assert s["model"] == data["model"] and s["outcome"] == data["score"]["outcome"]
    assert s["started_at"] == data["started_at"] and s["ended_at"] == data["ended_at"]
    acts = store.actions(sid)
    n_tool = sum(1 for e in data["audit_log"] if not str(e["tool"]).startswith("_"))
    assert len(acts) == n_tool
    # the agent's real tool results are kept beside the Watcher's (counterfactual) decision
    assert all(a["result"] is not None for a in acts)
    assert len(store.transcript(sid)) == len(data["messages"])
    # re-import replaces rather than duplicates
    sid2 = demo.replay_run(path, store, "mock")
    assert sid2 == sid and len(store.actions(sid)) == n_tool


def test_cli_single_card_in_memory(capsys):
    rc = demo.main(["--db", ":memory:", "--env", "coin_cell", "--card", "m02", "--script", "exploit"])
    out = capsys.readouterr().out
    assert rc == 0 and "coin_cell" in out and "materials_discovery: sessions 1" in out
