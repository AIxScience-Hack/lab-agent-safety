"""LabWatcher web UI -- FastAPI app serving Watcher Live, Analyzer, Policy Panel, Rules Panel and
Settings.  Run with ``uvicorn labwatcher.ui.app:app --port 8787`` from the repo root.

The app talks to the rest of LabWatcher only through the SPEC interfaces and duck-types around
them (``labwatcher.store.Store``, ``labwatcher.settings``, ``labwatcher.demo.run_demo``); when a
module is missing it falls back to ``labwatcher.ui.fixtures`` so the UI still runs offline.

Environment knobs: ``LABWATCHER_STORE=memory`` forces the in-memory store, ``LABWATCHER_SEED=0``
disables demo seeding of an empty store, ``LABWATCHER_POLICY_DIR`` / ``LABWATCHER_RULES_DIR``
redirect the YAML editors.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import fixtures
from .fixtures import (CONTEXT_ENVS, CONTEXTS, DEFAULT_LOCKS, DEFAULT_POLICY, DEFAULT_SETTINGS,
                       FALLBACK_CARDS, FALLBACK_RULES, TAXONOMY, MemoryStore)

PKG_DIR = Path(__file__).resolve().parent.parent          # labwatcher/
REPO_DIR = PKG_DIR.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
POLICY_KEYS = ["triage_system", "evaluator_system", "trailing_system", "suggestion_template"]
RULE_DECISIONS = ["allow", "deny", "escalate_triage", "escalate_human"]
RULE_MATCH_KEYS = ["tool", "command", "path", "args"]
RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
LIST_KEYS = ("actions", "transcript", "trailing", "human_decisions", "enrichment")


# ----------------------------------------------------------------------------------------------
# Store access (duck-typed over the SPEC interface)
# ----------------------------------------------------------------------------------------------

def open_store(path: str | None = None):
    """Return (store, backend_name, warnings)."""
    warnings: list[str] = []
    if os.environ.get("LABWATCHER_STORE", "").lower() == "memory":
        return MemoryStore(), "memory", warnings
    try:
        from labwatcher.store import Store  # type: ignore
    except Exception as exc:  # module not written yet, or broken
        warnings.append(f"labwatcher.store unavailable ({exc.__class__.__name__}: {exc}); using in-memory store")
        return MemoryStore(), "memory", warnings
    for ctor in ((lambda: Store(path)) if path else (lambda: Store()), lambda: Store(path=path), lambda: Store()):
        try:
            return ctor(), "sqlite", warnings
        except TypeError:
            continue
        except Exception as exc:
            warnings.append(f"Store() failed ({exc}); using in-memory store")
            break
    return MemoryStore(), "memory", warnings


def _loads(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row(r) -> dict:
    if isinstance(r, dict):
        return dict(r)
    if dataclasses.is_dataclass(r):
        return dataclasses.asdict(r)
    try:
        return dict(r)  # sqlite3.Row
    except Exception:
        return dict(vars(r))


def norm_action(a) -> dict:
    d = _row(a)
    d["args"] = _loads(d.pop("args_json", None) if "args_json" in d else d.get("args"), {})
    d["categories"] = _loads(d.pop("categories_json", None) if "categories_json" in d else d.get("categories"), [])
    d["triage"] = _loads(d.pop("triage_json", None) if "triage_json" in d else d.get("triage"), None)
    d["evaluator"] = _loads(d.pop("evaluator_json", None) if "evaluator_json" in d else d.get("evaluator"), None)
    d["ok"] = bool(d.get("ok", True))
    return d


def norm_trailing(t) -> dict:
    d = _row(t)
    d["scores"] = _loads(d.pop("scores_json", None) if "scores_json" in d else d.get("scores"), {})
    return d


def norm_enrichment(e) -> dict:
    d = _row(e)
    d["result"] = _loads(d.pop("result_json", None) if "result_json" in d else d.get("result"), [])
    return d


def norm_session(s) -> dict:
    d = _row(s)
    d["flagged"] = bool(d.get("flagged"))
    d["card_title"] = fixtures.card_title(d.get("card") or "") if d.get("card") else None
    return d


def session_detail(store, sid: str) -> dict | None:
    raw = store.session(sid)
    if raw is None:
        return None
    raw = _row(raw)
    if "session" in raw and isinstance(raw["session"], (dict, tuple)) or dataclasses.is_dataclass(raw.get("session")):
        sess = norm_session(raw["session"])
        lists = raw
    else:
        lists = {k: raw.pop(k, []) for k in LIST_KEYS}
        sess = norm_session(raw)
    detail = {"session": sess}
    detail["actions"] = sorted((norm_action(a) for a in lists.get("actions") or []), key=lambda a: a.get("seq", 0))
    detail["transcript"] = sorted((_row(t) for t in lists.get("transcript") or []), key=lambda t: t.get("idx", 0))
    detail["trailing"] = sorted((norm_trailing(t) for t in lists.get("trailing") or []), key=lambda t: t.get("at_action", 0))
    detail["human_decisions"] = [_row(h) for h in lists.get("human_decisions") or []]
    detail["enrichment"] = [norm_enrichment(e) for e in lists.get("enrichment") or []]
    return detail


def _supported_kwargs(fn, kwargs: dict) -> dict:
    """Keep only the keyword arguments ``fn`` accepts (stores differ slightly in filter support)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def list_sessions(store, context=None, **filters) -> list[dict]:
    kwargs = {"context": context, "limit": 1000, **{k: v for k, v in filters.items() if v is not None}}
    rows = store.sessions(**_supported_kwargs(store.sessions, kwargs))
    out = [norm_session(r) for r in rows]
    # apply every filter locally too, in case the store ignored one
    for k, v in filters.items():
        if v is None:
            continue
        if k == "until":
            out = [r for r in out if (r.get("started_at") or "") <= v]
        elif k == "since":
            out = [r for r in out if (r.get("started_at") or "") >= v]
        elif k == "min_score":
            out = [r for r in out if (r.get("max_score") or 0) >= v]
        elif k == "flagged":
            out = [r for r in out if bool(r.get("flagged")) == bool(v)]
        elif k in ("status", "env"):
            out = [r for r in out if r.get(k) == v]
    return out


def _session_row(store, sid: str) -> dict:
    getter = getattr(store, "get_session", None)
    if callable(getter):
        row = getter(sid)
        return norm_session(row) if row else {}
    d = session_detail(store, sid)
    return d["session"] if d else {}


def actions_since(store, after_id: int, context=None, limit=200) -> list[dict]:
    """New graded actions (id > after_id) across sessions, oldest first, annotated with session info."""
    if hasattr(store, "actions_since"):
        rows = store.actions_since(after_id, context=context, limit=limit)
        cache: dict[str, dict] = {}
        out = []
        for r in rows:
            a = norm_action(r)
            sid = a["session_id"]
            if sid not in cache:
                cache[sid] = _session_row(store, sid)
            s = cache[sid]
            a.update(context=s.get("context"), env=s.get("env"), card=s.get("card"), card_title=s.get("card_title"))
            out.append(a)
        return out
    # Fallback for stores without a cross-session tail: scan sessions that can still produce
    # actions (running, or ended in the last 10 minutes); on first connect also the latest 20.
    now = datetime.now(timezone.utc)
    recent_end = (now - timedelta(minutes=10)).isoformat()
    sessions = list_sessions(store, context)
    candidates = [s for s in sessions if s.get("status") == "running" or (s.get("ended_at") or "") >= recent_end]
    if after_id == 0:
        seen = {s["id"] for s in candidates}
        candidates += [s for s in sorted(sessions, key=lambda s: s.get("started_at") or "", reverse=True)[:20]
                       if s["id"] not in seen]
    out = []
    for s in candidates:
        d = session_detail(store, s["id"])
        for a in (d or {}).get("actions", []):
            if (a.get("id") or 0) > after_id:
                a.update(context=s.get("context"), env=s.get("env"), card=s.get("card"), card_title=s.get("card_title"))
                out.append(a)
    out.sort(key=lambda a: a.get("id") or 0)
    return out[-limit:] if after_id == 0 else out[:limit]


def pending_escalations(store, context=None) -> list[dict]:
    fn = getattr(store, "pending_escalations", None)
    if callable(fn):
        rows = [norm_action(r) for r in fn(**_supported_kwargs(fn, {"context": context}))]
        if context:
            rows = [r for r in rows if r.get("context") in (None, context)]
        return rows
    out = []
    for s in list_sessions(store, context, status="running"):
        d = session_detail(store, s["id"])
        if not d:
            continue
        decided = {h.get("action_id") for h in d["human_decisions"]}
        for a in d["actions"]:
            if a.get("decision") == "escalate" and a.get("id") not in decided:
                a.update(context=s["context"], env=s["env"], card=s.get("card"))
                out.append(a)
    return out


def find_action(store, action_id: int, context=None) -> dict | None:
    for a in pending_escalations(store, context):
        if a.get("id") == action_id:
            return a
    for s in list_sessions(store, context):
        d = session_detail(store, s["id"])
        for a in (d or {}).get("actions", []):
            if a.get("id") == action_id:
                a.update(context=s["context"], env=s["env"], card=s.get("card"))
                return a
    return None


def compute_summary(store, context=None, days=14) -> dict:
    base: dict = {}
    try:
        base = dict(store.summary(context) or {})
    except Exception:
        base = {}
    sessions = list_sessions(store, context)
    total = len(sessions)
    flagged = sum(1 for s in sessions if s.get("flagged"))
    blocked = sum(int(s.get("blocked_count") or 0) for s in sessions)
    escalated = sum(int(s.get("escalated_count") or 0) for s in sessions)
    today = datetime.now(timezone.utc).date()
    day_keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    trend = {k: {"day": k, "sessions": 0, "flagged": 0, "blocked": 0} for k in day_keys}
    for s in sessions:
        day = (s.get("started_at") or "")[:10]
        if day in trend:
            trend[day]["sessions"] += 1
            trend[day]["flagged"] += 1 if s.get("flagged") else 0
            trend[day]["blocked"] += int(s.get("blocked_count") or 0)
    by_cat: Counter = Counter({cid: 0 for cid, _ in TAXONOMY})
    if isinstance(base.get("by_category"), dict):
        for cid, n in base["by_category"].items():
            if cid in by_cat:
                by_cat[cid] += int(n or 0)
    elif isinstance(base.get("by_category"), list):
        for item in base["by_category"]:
            if isinstance(item, dict) and item.get("id") in by_cat:
                by_cat[item["id"]] += int(item.get("count") or 0)
    else:  # derive from the per-session rows (deny/escalate/score>=7 actions, hot trailing windows)
        for s in sessions:
            d = session_detail(store, s["id"]) or {}
            for a in d.get("actions", []):
                if a.get("decision") in ("deny", "escalate") or (a.get("score") or 0) >= 7:
                    for c in a.get("categories") or []:
                        if c in by_cat:
                            by_cat[c] += 1
            for t in d.get("trailing", []):
                if (t.get("max_score") or 0) >= 7 and t.get("top_category") in by_cat:
                    by_cat[t["top_category"]] += 1
    return {
        "context": context,
        "total_sessions": base.get("total_sessions", base.get("sessions", total)),
        "blocked_actions": base.get("blocked_actions", blocked),
        "escalated_actions": base.get("escalated_actions", escalated),
        "flagged_sessions": base.get("flagged_sessions", flagged),
        "failure_rate": base.get("failure_rate", (flagged / total) if total else 0.0),
        "running_sessions": sum(1 for s in sessions if s.get("status") == "running"),
        "by_status": base.get("by_status") or dict(Counter(s.get("status") for s in sessions)),
        "by_outcome": base.get("by_outcome") or dict(Counter((s.get("outcome") or "unknown") for s in sessions)),
        "by_category": [{"id": cid, "label": label, "count": by_cat[cid]} for cid, label in TAXONOMY],
        "trend": list(trend.values()),
    }


# ----------------------------------------------------------------------------------------------
# Catalogue (contexts -> envs -> cards)
# ----------------------------------------------------------------------------------------------

def build_catalog() -> dict:
    catalog: dict[str, dict] = {}
    for ctx in CONTEXTS:
        envs: dict[str, list[dict]] = {}
        root = REPO_DIR / ctx / "tasks"
        if root.is_dir():
            for task_yaml in sorted(root.glob("*/task.yaml")):
                env = task_yaml.parent.name
                cards = []
                for hp in sorted((task_yaml.parent / "honeypots").glob("*.yaml")):
                    try:
                        data = yaml.safe_load(hp.read_text()) or {}
                    except Exception:
                        continue
                    cards.append({"id": str(data.get("id") or hp.stem), "title": data.get("title") or hp.stem,
                                  "category": data.get("category"), "fault_kind": data.get("fault_kind")})
                envs[env] = cards
        for env in CONTEXT_ENVS[ctx]:
            if env not in envs or not envs[env]:
                envs[env] = [{"id": cid, "title": title, "category": None, "fault_kind": None}
                             for cid, title in FALLBACK_CARDS.get(env, [])]
        catalog[ctx] = {"envs": envs}
    return catalog


# ----------------------------------------------------------------------------------------------
# Policies & rules (YAML files)
# ----------------------------------------------------------------------------------------------

class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    if "\n" in data or len(data) > 80:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_presenter)


def _dump_yaml(data) -> str:
    return yaml.dump(data, Dumper=_LiteralDumper, sort_keys=False, allow_unicode=True, width=100)


def _check_context(context: str) -> str:
    if context not in CONTEXTS:
        raise HTTPException(404, f"unknown context {context!r}; expected one of {CONTEXTS}")
    return context


def load_policy(policy_dir: Path, context: str) -> dict:
    path = policy_dir / f"{context}.yaml"
    data: dict = {}
    exists = path.is_file()
    if exists:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise HTTPException(500, f"{path.name} is not valid YAML: {exc}")
    policy = {k: (data.get(k) if isinstance(data.get(k), str) else DEFAULT_POLICY[k]) for k in POLICY_KEYS}
    extra = {k: v for k, v in data.items() if k not in POLICY_KEYS}
    return {"context": context, "path": str(path), "exists": exists, "policy": policy, "extra": extra}


def save_policy(policy_dir: Path, context: str, updates: dict) -> dict:
    current = load_policy(policy_dir, context)
    bad = [k for k in updates if k not in POLICY_KEYS]
    if bad:
        raise HTTPException(400, f"unknown policy keys: {bad}; allowed: {POLICY_KEYS}")
    for k, v in updates.items():
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(400, f"{k} must be a non-empty string")
    merged = dict(current["extra"])
    merged.update(current["policy"])
    merged.update(updates)
    ordered = {k: merged[k] for k in POLICY_KEYS}
    ordered.update({k: v for k, v in merged.items() if k not in POLICY_KEYS})
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / f"{context}.yaml").write_text(_dump_yaml(ordered))
    return load_policy(policy_dir, context)


def _read_rules_file(rules_dir: Path, context: str) -> tuple[list[dict], dict | None]:
    path = rules_dir / f"{context}.yaml"
    if not path.is_file():
        return [dict(r) for r in FALLBACK_RULES[context]], None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise HTTPException(500, f"{path.name} is not valid YAML: {exc}")
    if data is None:
        return [], {"rules": []}
    if isinstance(data, list):
        return [dict(r) for r in data], None
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        return [dict(r) for r in data["rules"]], data
    raise HTTPException(500, f"{path.name}: expected a list of rules or a mapping with a 'rules' list")


def _write_rules_file(rules_dir: Path, context: str, rules: list[dict], wrapper: dict | None) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    if wrapper is None:
        payload: Any = rules
    else:
        payload = dict(wrapper)
        payload["rules"] = rules
    (rules_dir / f"{context}.yaml").write_text(_dump_yaml(payload))


def validate_rule(rule: Any) -> dict:
    """Validate a rule dict; raises HTTPException(400) with field errors. Returns a cleaned rule."""
    errors: dict[str, str] = {}
    if not isinstance(rule, dict):
        raise HTTPException(400, {"errors": {"_": "rule must be an object"}})
    rid = str(rule.get("id") or "").strip()
    if not RULE_ID_RE.match(rid):
        errors["id"] = "id is required: letters, digits, _ . : - (max 64 chars)"
    decision = rule.get("decision")
    if decision not in RULE_DECISIONS:
        errors["decision"] = f"decision must be one of {RULE_DECISIONS}"
    try:
        priority = int(rule.get("priority", 50))
    except (TypeError, ValueError):
        errors["priority"] = "priority must be an integer"
        priority = 50
    match = rule.get("match") or {}
    if not isinstance(match, dict):
        errors["match"] = "match must be an object with tool/command/path/args regexes"
        match = {}
    clean_match = {}
    for key, pattern in match.items():
        if key not in RULE_MATCH_KEYS:
            errors[f"match.{key}"] = f"unknown match key; allowed: {RULE_MATCH_KEYS}"
            continue
        if pattern is None or pattern == "":
            continue
        if not isinstance(pattern, str):
            errors[f"match.{key}"] = "pattern must be a string"
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors[f"match.{key}"] = f"invalid regex: {exc}"
            continue
        clean_match[key] = pattern
    if not clean_match and not any(k.startswith("match") for k in errors):
        errors["match"] = "at least one of tool/command/path/args is required"
    reason = rule.get("reason")
    if reason is not None and not isinstance(reason, str):
        errors["reason"] = "reason must be a string"
    if errors:
        raise HTTPException(400, {"errors": errors})
    out = {"id": rid, "match": clean_match, "decision": decision, "priority": priority, "reason": reason or ""}
    for k, v in rule.items():
        if k not in out and k not in ("match",):
            out[k] = v
    return out


# ----------------------------------------------------------------------------------------------
# Settings (duck-typed over labwatcher.settings)
# ----------------------------------------------------------------------------------------------

def _as_dict(obj) -> dict | None:
    for attr in ("to_dict", "as_dict", "model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                v = fn()
                if isinstance(v, dict):
                    return v
            except Exception:
                pass
    for attr in ("effective", "data", "values"):
        v = getattr(obj, attr, None)
        if isinstance(v, dict):
            return v
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return obj
    return None


def _layer_name(layer) -> str:
    if isinstance(layer, (str, Path)):
        return str(layer)
    name = getattr(layer, "name", None) or layer.__class__.__name__
    path = getattr(layer, "path", None)
    return f"{name} ({path})" if path else str(name)


def load_settings_view() -> dict:
    view = {"source": "fallback", "effective": None, "locks": {}, "errors": [], "warnings": [], "layers": []}
    try:
        import labwatcher.settings as settings_mod  # type: ignore
    except Exception as exc:
        view["warnings"].append(f"labwatcher.settings unavailable ({exc.__class__.__name__}: {exc}); showing settings.yaml / defaults")
        settings_mod = None
    obj = None
    if settings_mod is not None:
        candidates = []
        Settings = getattr(settings_mod, "Settings", None)
        if Settings is not None:
            for name in ("load", "from_layers", "from_files", "default"):
                fn = getattr(Settings, name, None)
                if callable(fn):
                    candidates.append(fn)
            candidates.append(Settings)
        for name in ("load_settings", "load", "effective_settings"):
            fn = getattr(settings_mod, name, None)
            if callable(fn):
                candidates.append(fn)
        for fn in candidates:
            try:
                obj = fn()
                break
            except Exception as exc:
                view["warnings"].append(f"{getattr(fn, '__qualname__', fn)}() failed: {exc}")
        if obj is not None:
            eff = _as_dict(obj)
            if eff is not None:
                view["source"] = "labwatcher.settings"
                view["effective"] = eff
                for attr in ("locks", "permissions", "lock_status"):
                    v = getattr(obj, attr, None)
                    if isinstance(v, dict):
                        view["locks"] = v
                        break
                for attr in ("errors", "warnings"):
                    v = getattr(obj, attr, None)
                    if isinstance(v, (list, tuple)):
                        view[attr] = [str(x) for x in v]
                for attr in ("layers", "sources"):
                    v = getattr(obj, attr, None)
                    if isinstance(v, (list, tuple)) and v:
                        view["layers"] = [_layer_name(x) for x in v]
                        break
    if view["effective"] is None:
        path = PKG_DIR / "settings.yaml"
        if path.is_file():
            try:
                view["effective"] = yaml.safe_load(path.read_text()) or {}
                view["layers"] = [str(path)]
                view["source"] = "settings.yaml"
            except yaml.YAMLError as exc:
                view["errors"].append(f"settings.yaml is not valid YAML: {exc}")
        if view["effective"] is None:
            view["effective"] = DEFAULT_SETTINGS
            view["source"] = "built-in defaults"
    eff = view["effective"]
    if not view["locks"]:
        for key in ("permissions", "locks", "_permissions"):
            if isinstance(eff.get(key), dict):
                view["locks"] = eff[key]
                break
    if not view["locks"]:
        view["locks"] = {k: DEFAULT_LOCKS.get(k, "modifiable") for k in eff if not str(k).startswith("_")}
    return view


# ----------------------------------------------------------------------------------------------
# App factory
# ----------------------------------------------------------------------------------------------

def create_app(store=None, policy_dir: Path | None = None, rules_dir: Path | None = None,
               seed: bool | None = None) -> FastAPI:
    app = FastAPI(title="LabWatcher", version="0.1", docs_url="/api/docs", redoc_url=None)
    warnings: list[str] = []
    if store is None:
        store, backend, warnings = open_store(os.environ.get("LABWATCHER_DB"))
    else:
        backend = "memory" if isinstance(store, MemoryStore) else "custom"
    if seed is None:
        seed = os.environ.get("LABWATCHER_SEED", "1") != "0"
    if seed:
        try:
            if fixtures.is_empty(store):
                fixtures.seed_demo(store)
        except Exception as exc:
            warnings.append(f"demo seeding failed against {backend} store: {exc}")
    app.state.store = store
    app.state.backend = backend
    app.state.warnings = warnings
    app.state.policy_dir = Path(policy_dir or os.environ.get("LABWATCHER_POLICY_DIR") or PKG_DIR / "policies")
    app.state.rules_dir = Path(rules_dir or os.environ.get("LABWATCHER_RULES_DIR") or PKG_DIR / "rules")
    app.state.jobs: dict[str, dict] = {}
    app.state.catalog = build_catalog()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def page(name: str):
        return FileResponse(STATIC_DIR / name, media_type="text/html")

    # -- pages --------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def analyzer_page():
        return page("index.html")

    @app.get("/live", include_in_schema=False)
    def live_page():
        return page("live.html")

    @app.get("/session/{sid}", include_in_schema=False)
    def session_page(sid: str):
        if store.session(sid) is None:
            raise HTTPException(404, f"no session {sid!r}")
        return page("session.html")

    @app.get("/policy", include_in_schema=False)
    def policy_page():
        return page("policy.html")

    @app.get("/rules", include_in_schema=False)
    def rules_page():
        return page("rules.html")

    @app.get("/settings", include_in_schema=False)
    def settings_page():
        return page("settings.html")

    # -- analyzer API ---------------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "backend": backend, "warnings": warnings,
                "demo_available": _demo_available()[0]}

    @app.get("/api/taxonomy")
    def taxonomy():
        return [{"id": cid, "label": label} for cid, label in TAXONOMY]

    @app.get("/api/catalog")
    def catalog():
        return {"contexts": CONTEXTS, "catalog": app.state.catalog}

    @app.get("/api/summary")
    def summary(context: str | None = None, days: int = 14):
        if context:
            _check_context(context)
        return compute_summary(store, context, days=max(2, min(days, 90)))

    @app.get("/api/sessions")
    def sessions(context: str | None = None, status: str | None = None, env: str | None = None,
                 flagged: bool | None = None, min_score: int | None = None, since: str | None = None,
                 until: str | None = None, sort: str = "date", order: str = "desc", limit: int = 500):
        if context:
            _check_context(context)
        rows = list_sessions(store, context, status=status, env=env, flagged=flagged,
                             min_score=min_score, since=since, until=until)
        # apply filters locally too in case the store ignored some of them
        rows = [r for r in rows
                if (status is None or r.get("status") == status)
                and (env is None or r.get("env") == env)
                and (flagged is None or bool(r.get("flagged")) == flagged)
                and (min_score is None or (r.get("max_score") or 0) >= min_score)
                and (since is None or (r.get("started_at") or "") >= since)
                and (until is None or (r.get("started_at") or "") <= until)]
        keys = {
            "severity": lambda r: (r.get("max_score") or 0, int(r.get("blocked_count") or 0), r.get("started_at") or ""),
            "status": lambda r: (r.get("status") or "", r.get("started_at") or ""),
            "date": lambda r: r.get("started_at") or "",
            "env": lambda r: (r.get("env") or "", r.get("started_at") or ""),
            "blocked": lambda r: (int(r.get("blocked_count") or 0), r.get("started_at") or ""),
        }
        rows.sort(key=keys.get(sort, keys["date"]), reverse=(order != "asc"))
        return {"count": len(rows), "sessions": rows[:limit]}

    @app.get("/api/sessions/{sid}")
    def session_api(sid: str):
        d = session_detail(store, sid)
        if d is None:
            raise HTTPException(404, f"no session {sid!r}")
        return d

    # -- live API -------------------------------------------------------------------------------
    def _sse(event: str, data, event_id=None) -> str:
        head = f"id: {event_id}\n" if event_id is not None else ""
        return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    @app.get("/api/live/events")
    async def live_events(request: Request, context: str | None = None, after: int = 0,
                          once: bool = False, backlog: int = 30):
        if context:
            _check_context(context)

        async def gen():
            last = after
            first = True
            while True:
                try:
                    acts = await asyncio.to_thread(actions_since, store, last, context)
                    if first and after == 0 and backlog:
                        acts = acts[-backlog:]
                    for a in acts:
                        yield _sse("action", a, a.get("id"))
                        last = max(last, int(a.get("id") or 0))
                    pend = await asyncio.to_thread(pending_escalations, store, context)
                    yield _sse("escalations", pend)
                    if first:
                        yield _sse("hello", {"backend": backend, "after": last})
                    first = False
                except Exception as exc:  # keep the stream alive, report the error
                    yield _sse("error", {"message": str(exc)})
                if once:
                    return
                yield ": keepalive\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/escalations")
    def escalations(context: str | None = None):
        if context:
            _check_context(context)
        return {"pending": pending_escalations(store, context)}

    @app.post("/api/escalations/{action_id}")
    async def resolve_escalation(action_id: int, request: Request):
        body = await request.json() if await request.body() else {}
        decision = str(body.get("decision", "")).lower()
        if decision in ("allow", "approved"):
            decision = "approve"
        if decision in ("denied", "reject"):
            decision = "deny"
        if decision not in ("approve", "deny"):
            raise HTTPException(400, "decision must be 'approve' or 'deny'")
        note = str(body.get("note") or "")
        action = find_action(store, action_id)
        if action is None:
            raise HTTPException(404, f"no action {action_id}")
        if action.get("decision") != "escalate":
            raise HTTPException(409, f"action {action_id} was not escalated (decision={action.get('decision')})")
        verdict = "allow" if decision == "approve" else "deny"
        resolver = getattr(store, "resolve_escalation", None)
        if callable(resolver):
            try:
                resolver(action_id, decision, note or None)
            except KeyError:
                raise HTTPException(404, f"no action {action_id}")
            hid = None
        else:
            hid = store.add_human_decision(action["session_id"], action_id, verdict, note or None)
        return {"ok": True, "id": hid, "action_id": action_id, "session_id": action["session_id"],
                "decision": decision, "verdict": verdict, "note": note,
                "pending": pending_escalations(store, action.get("context"))}

    # -- demo runs --------------------------------------------------------------------------------
    def _demo_available():
        try:
            from labwatcher.demo import run_demo  # type: ignore
        except Exception as exc:
            return False, f"labwatcher.demo.run_demo is not available ({exc.__class__.__name__}: {exc})"
        return True, run_demo

    @app.post("/api/demo/run", status_code=202)
    async def demo_run(request: Request):
        body = await request.json()
        context = _check_context(str(body.get("context", "")))
        env = str(body.get("env") or "")
        card = str(body.get("card") or "")
        script = str(body.get("script") or "honest")
        provider = str(body.get("provider") or "mock")
        envs = app.state.catalog[context]["envs"]
        if env not in envs:
            raise HTTPException(400, f"unknown env {env!r} for {context}; expected one of {sorted(envs)}")
        if card and card not in {c["id"] for c in envs[env]}:
            raise HTTPException(400, f"unknown card {card!r} for {env}")
        if script not in ("honest", "exploit"):
            raise HTTPException(400, "script must be 'honest' or 'exploit'")
        if provider not in ("mock", "modal", "anthropic"):
            raise HTTPException(400, "provider must be mock, modal or anthropic")
        ok, run_demo = _demo_available()
        if not ok:
            raise HTTPException(501, f"{run_demo}. The integrator's labwatcher/demo.py must define "
                                     "run_demo(context, env, card_id, script, provider='mock', store=None) -> session_id.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "context": context, "env": env, "card": card, "script": script, "provider": provider,
               "status": "running", "session_id": None, "error": None,
               "started_at": datetime.now(timezone.utc).isoformat()}
        app.state.jobs[job_id] = job

        def worker():
            try:
                job["session_id"] = run_demo(context, env, card, script, provider=provider, store=store)
                job["status"] = "done"
            except Exception as exc:  # surface to the UI
                job["status"] = "error"
                job["error"] = f"{exc.__class__.__name__}: {exc}"
            job["ended_at"] = datetime.now(timezone.utc).isoformat()

        threading.Thread(target=worker, name=f"demo-{job_id}", daemon=True).start()
        return job

    @app.get("/api/demo/jobs")
    def demo_jobs():
        jobs = sorted(app.state.jobs.values(), key=lambda j: j["started_at"], reverse=True)
        return {"jobs": jobs[:50], "demo_available": _demo_available()[0]}

    # -- policy -----------------------------------------------------------------------------------
    @app.get("/api/policy/{context}")
    def get_policy(context: str):
        return load_policy(app.state.policy_dir, _check_context(context))

    @app.post("/api/policy/{context}")
    async def post_policy(context: str, request: Request):
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("policy"), dict):
            body = body["policy"]
        if not isinstance(body, dict) or not body:
            raise HTTPException(400, "body must be an object of policy fields")
        return save_policy(app.state.policy_dir, _check_context(context), body)

    # -- rules -------------------------------------------------------------------------------------
    @app.get("/api/rules/{context}")
    def get_rules(context: str):
        rules, wrapper = _read_rules_file(app.state.rules_dir, _check_context(context))
        path = app.state.rules_dir / f"{context}.yaml"
        rules.sort(key=lambda r: -int(r.get("priority") or 0))
        return {"context": context, "path": str(path), "exists": path.is_file(), "count": len(rules),
                "decisions": RULE_DECISIONS, "match_keys": RULE_MATCH_KEYS, "rules": rules}

    @app.post("/api/rules/{context}", status_code=201)
    async def add_rule(context: str, request: Request):
        _check_context(context)
        rule = validate_rule(await request.json())
        rules, wrapper = _read_rules_file(app.state.rules_dir, context)
        if any(r.get("id") == rule["id"] for r in rules):
            raise HTTPException(409, f"rule {rule['id']!r} already exists; use PUT to edit it")
        rules.append(rule)
        _write_rules_file(app.state.rules_dir, context, rules, wrapper)
        return rule

    @app.put("/api/rules/{context}/{rule_id}")
    async def edit_rule(context: str, rule_id: str, request: Request):
        _check_context(context)
        body = await request.json()
        body.setdefault("id", rule_id)
        rule = validate_rule(body)
        rules, wrapper = _read_rules_file(app.state.rules_dir, context)
        idx = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
        if idx is None:
            raise HTTPException(404, f"no rule {rule_id!r} in {context}")
        if rule["id"] != rule_id and any(r.get("id") == rule["id"] for r in rules):
            raise HTTPException(409, f"rule {rule['id']!r} already exists")
        rules[idx] = rule
        _write_rules_file(app.state.rules_dir, context, rules, wrapper)
        return rule

    @app.delete("/api/rules/{context}/{rule_id}")
    def delete_rule(context: str, rule_id: str):
        _check_context(context)
        rules, wrapper = _read_rules_file(app.state.rules_dir, context)
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            raise HTTPException(404, f"no rule {rule_id!r} in {context}")
        _write_rules_file(app.state.rules_dir, context, kept, wrapper)
        return {"ok": True, "deleted": rule_id, "count": len(kept)}

    @app.get("/api/rules/{context}/test")
    def test_rules(context: str, tool: str = "", command: str = "", path: str = "", args: str = ""):
        """Dry-run the rules against a hypothetical action (highest priority wins; ties -> first)."""
        rules, _ = _read_rules_file(app.state.rules_dir, _check_context(context))
        probe = {"tool": tool, "command": command, "path": path, "args": args}
        hits = []
        for r in rules:
            m = r.get("match") or {}
            if not m:
                continue
            ok = True
            for key, pattern in m.items():
                try:
                    if not re.search(pattern, probe.get(key) or ""):
                        ok = False
                        break
                except re.error:
                    ok = False
                    break
            if ok:
                hits.append(r)
        winner = max(hits, key=lambda r: int(r.get("priority") or 0)) if hits else None
        return {"probe": probe, "matches": hits, "winner": winner}

    # -- settings ----------------------------------------------------------------------------------
    @app.get("/api/settings")
    def settings_api():
        view = load_settings_view()
        view["ui_warnings"] = warnings
        view["backend"] = backend
        return view

    @app.exception_handler(HTTPException)
    async def http_exc(_request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, (dict, list)) else {"detail": exc.detail}
        if isinstance(detail, dict) and "detail" not in detail:
            detail = {"detail": "validation failed", **detail}
        return JSONResponse(detail, status_code=exc.status_code)

    return app


app = create_app()
