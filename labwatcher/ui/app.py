"""LabWatcher web UI -- FastAPI app serving Watcher Live, Analyzer, Policy Panel, Rules Panel and
Settings.  Run with ``uvicorn labwatcher.ui.app:app --port 8787`` from the repo root.

The app talks to the rest of LabWatcher only through the SPEC interfaces and duck-types around
them (``labwatcher.store.Store``, ``labwatcher.settings``, ``labwatcher.demo.run_demo``); when a
module is missing it falls back to ``labwatcher.ui.fixtures`` so the UI still runs offline.

Environment knobs: ``LABWATCHER_STORE=memory`` forces the in-memory store, ``LABWATCHER_DB`` picks
the SQLite file, ``LABWATCHER_SEED`` controls seeding of an *empty* store (``1``/``demo`` = real
oracle sessions through the Watcher via labwatcher.demo, ``quick`` = one card per env, ``fixture`` =
synthetic rows, ``0`` = off),
``LABWATCHER_POLICY_DIR`` / ``LABWATCHER_RULES_DIR`` redirect the YAML editors.

The module-level ``app`` opens (and, when empty, seeds) the store at ASGI startup, not at import
time, so importing this module (e.g. from pytest) never touches ``labwatcher/data``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from labwatcher.store import flag_rule_text
from . import fixtures
from .fixtures import (CONTEXT_ENVS, CONTEXTS, DEFAULT_LOCKS, DEFAULT_POLICY, DEFAULT_SETTINGS,
                       FALLBACK_CARDS, FALLBACK_RULES, TAXONOMY, MemoryStore)

PKG_DIR = Path(__file__).resolve().parent.parent          # labwatcher/
REPO_DIR = PKG_DIR.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
POLICY_KEYS = ["triage_system", "evaluator_system", "trailing_system", "suggestion_template"]
RULE_DECISIONS = ["allow", "deny", "escalate_triage", "escalate_human"]
RULE_MATCH_KEYS = ["tool", "command", "path", "args"]
RULE_FIELDS = ("id", "match", "decision", "priority", "reason", "category", "enabled")
RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
LIST_KEYS = ("actions", "transcript", "trailing", "human_decisions", "enrichment")
TAXONOMY_IDS = [cid for cid, _ in TAXONOMY]
_CARD_TITLES: dict[str, str] = {}      # card id -> title, filled from the task directories by build_catalog


def card_title(card: str | None) -> str | None:
    if not card:
        return None
    return _CARD_TITLES.get(card) or fixtures.card_title(card)


# ----------------------------------------------------------------------------------------------
# Request / file helpers
# ----------------------------------------------------------------------------------------------

async def _json_object(request: Request, allow_empty: bool = False) -> dict:
    """The request body as a JSON object. Empty, non-JSON or non-object bodies are a 400 (not a
    raw 500 from json.decoder / AttributeError in the handler)."""
    raw = await request.body()
    if not raw or not raw.strip():
        if allow_empty:
            return {}
        raise HTTPException(400, "request body must be a JSON object")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"request body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise HTTPException(400, f"request body must be a JSON object, got {type(body).__name__}")
    return body


def _atomic_write(path: Path, text: str) -> None:
    """Write via <name>.tmp + os.replace so a concurrent reader (the Watcher, another request)
    never sees a half-written YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _invalidate_runners() -> None:
    """Tell cached demo runners (labwatcher.demo._RUNNERS) to re-read policies / rules."""
    try:
        from labwatcher import demo as _demo  # type: ignore
    except Exception:
        return
    fn = getattr(_demo, "invalidate_runners", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


class EscalationBroker:
    """Connects a running WatchedLab to the Live UI. `callback(timeout_s)` returns an
    `on_escalate(action, decision)` that registers the stored action id as waiting and blocks
    until `resolve(action_id, verdict, note)` is called from POST /api/escalations/{id} (or the
    timeout fires -> `on_timeout`, deny by default). The store row stays `escalate` meanwhile, so
    `pending_escalations` / the SSE `escalations` event show it with Approve / Deny."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waits: dict[int, dict] = {}

    def callback(self, timeout_s: float = 120.0, on_timeout: str = "timeout_deny"):
        timeout_s = max(0.1, float(timeout_s or 120.0))

        def on_escalate(action, decision):
            aid = getattr(decision, "action_id", None)
            if aid is None:          # not persisted -> the UI cannot show it; fail closed
                return on_timeout
            entry = {"event": threading.Event(), "verdict": None, "note": None,
                     "session_id": getattr(action, "session_id", None), "since": time.time()}
            with self._lock:
                self._waits[int(aid)] = entry
            try:
                if entry["event"].wait(timeout_s):
                    return entry["verdict"], entry["note"]
                return on_timeout
            finally:
                with self._lock:
                    self._waits.pop(int(aid), None)
        return on_escalate

    def resolve(self, action_id: int, verdict: str, note: str | None = None) -> bool:
        with self._lock:
            entry = self._waits.get(int(action_id))
        if entry is None:
            return False
        entry["verdict"], entry["note"] = verdict, note
        entry["event"].set()
        return True

    def waiting(self) -> list[dict]:
        with self._lock:
            return [{"action_id": k, "session_id": v["session_id"], "since": v["since"]}
                    for k, v in sorted(self._waits.items())]

    def is_waiting(self, action_id: int) -> bool:
        with self._lock:
            return int(action_id) in self._waits


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
    d["card_title"] = card_title(d.get("card"))
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


def action_row(store, action_id: int) -> dict | None:
    """One action by id: `store.get_action` when available (O(1)), else the scanning fallback."""
    getter = getattr(store, "get_action", None)
    if callable(getter):
        row = getter(action_id)
        return norm_action(row) if row else None
    return find_action(store, action_id)


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


def _flag_rule(store) -> str:
    fn = getattr(store, "flag_rule", None)
    if callable(fn):
        return fn()
    return flag_rule_text(getattr(store, "flag_threshold", 7))


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
        "flag_rule": base.get("flag_rule") or _flag_rule(store),
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
    for ctx_entry in catalog.values():
        for cards in ctx_entry["envs"].values():
            for c in cards:
                _CARD_TITLES.setdefault(c["id"], c["title"])
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
    text = _dump_yaml(ordered)
    # refuse to write anything the pipeline could not load back
    try:
        back = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"policy does not serialise to valid YAML: {exc}")
    if not isinstance(back, dict) or any(not isinstance(back.get(k), str) or not back[k].strip() for k in POLICY_KEYS):
        raise HTTPException(400, "policy would not round-trip: every prompt must be a non-empty string")
    try:
        from labwatcher.pipeline import load_policy as _pipeline_load_policy  # type: ignore
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / f"{context}.yaml").write_text(text)
            _pipeline_load_policy(context, td)
    except HTTPException:
        raise
    except ImportError:
        pass
    except Exception as exc:
        raise HTTPException(400, f"policy rejected by the pipeline loader: {exc}")
    _atomic_write(policy_dir / f"{context}.yaml", text)
    _invalidate_runners()
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
    """Atomically write the rules file after checking the result round-trips through the real
    engine (RuleEngine.load must see every rule and report no errors); otherwise 400 and the
    file on disk is left untouched."""
    if wrapper is None:
        payload: Any = rules
    else:
        payload = dict(wrapper)
        payload["rules"] = rules
    text = _dump_yaml(payload)
    try:
        back = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"rules do not serialise to valid YAML: {exc}")
    items = back.get("rules") if isinstance(back, dict) else back
    if not isinstance(items, list) or len(items) != len(rules):
        raise HTTPException(400, "rules would not round-trip as a list of rules; nothing written")
    rules_mod = _rules_module()
    engine_cls = getattr(rules_mod, "RuleEngine", None) if rules_mod else None
    if engine_cls is not None:
        engine = engine_cls.load(back if isinstance(back, (dict, list)) else {"rules": []})
        if engine.errors or len(engine.rules) != len(rules):
            raise HTTPException(400, {"errors": {"rules": "; ".join(engine.errors) or "engine dropped a rule"}})
    _atomic_write(rules_dir / f"{context}.yaml", text)
    _invalidate_runners()


def rules_status(rules_dir: Path) -> dict:
    """Per-context Stage 1 status for /api/health: rule count, load errors and whether the
    Watcher would run degraded (file missing / unparseable / wrong shape -> minimal deny set)."""
    out: dict[str, dict] = {}
    rules_mod = _rules_module()
    engine_cls = getattr(rules_mod, "RuleEngine", None) if rules_mod else None
    for ctx in CONTEXTS:
        path = rules_dir / f"{ctx}.yaml"
        entry = {"path": str(path), "exists": path.is_file(), "count": 0, "errors": [], "degraded": False}
        if not path.is_file():
            # no file yet: the panel serves the built-in defaults, which is not a fault
            entry["count"] = len(FALLBACK_RULES.get(ctx, []))
            out[ctx] = entry
            continue
        if engine_cls is None:
            try:
                rules, _ = _read_rules_file(rules_dir, ctx)
                entry["count"] = len(rules)
            except HTTPException as exc:
                entry["errors"] = [str(exc.detail)]
                entry["degraded"] = True
        else:
            engine = engine_cls.load(path)
            entry["count"] = len(engine.rules)
            entry["errors"] = list(engine.errors or [])
            entry["degraded"] = bool(engine.errors) and not engine.rules
        out[ctx] = entry
    return out


def _rules_module():
    """The real Stage 1 engine (``labwatcher.rules``) when importable, else None."""
    try:
        import labwatcher.rules as rules_mod  # type: ignore
        return rules_mod
    except Exception:
        return None


def validate_rule(rule: Any) -> dict:
    """Validate a rule dict; raises HTTPException(400) with field errors. Returns a cleaned rule
    restricted to the fields ``labwatcher.rules`` accepts (id, match, decision, priority, reason,
    category, enabled). When the real engine is importable its ``validate_rule`` is consulted too,
    so the panel can never save a rule the pipeline would reject at load time."""
    errors: dict[str, str] = {}
    if not isinstance(rule, dict):
        raise HTTPException(400, {"errors": {"_": "rule must be an object"}})
    rid = str(rule.get("id") or "").strip()
    if not RULE_ID_RE.match(rid):
        errors["id"] = "id is required: letters, digits, _ . : - (max 64 chars)"
    decision = rule.get("decision")
    if decision not in RULE_DECISIONS:
        errors["decision"] = f"decision must be one of {RULE_DECISIONS}"
    raw_priority = rule.get("priority", 50)
    try:
        if isinstance(raw_priority, bool):
            raise ValueError
        priority = int(raw_priority if raw_priority not in (None, "") else 50)
    except (TypeError, ValueError):
        errors["priority"] = "priority must be an integer"
        priority = 50
    category = rule.get("category")
    if category in ("", "null", "none"):
        category = None
    if category is not None and category not in TAXONOMY_IDS:
        errors["category"] = f"category must be a taxonomy id ({', '.join(TAXONOMY_IDS)}) or empty"
    enabled = rule.get("enabled", True)
    if isinstance(enabled, str):
        words = {"true": True, "1": True, "yes": True, "on": True, "false": False, "0": False, "no": False, "off": False}
        enabled = words.get(enabled.strip().lower(), enabled)
    if not isinstance(enabled, bool):
        errors["enabled"] = "enabled must be true/false"
        enabled = True
    for k in rule:
        if k not in RULE_FIELDS:
            errors[str(k)] = f"unknown field; allowed: {list(RULE_FIELDS)}"
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
    out = {"id": rid, "match": clean_match, "decision": decision, "priority": priority, "reason": reason or "",
           "category": category}
    if not enabled:
        out["enabled"] = False
    rules_mod = _rules_module()
    real_validate = getattr(rules_mod, "validate_rule", None) if rules_mod else None
    if callable(real_validate):
        try:
            problems = real_validate(out)
        except Exception as exc:  # the engine's validator should never raise; report rather than crash
            problems = [f"rules engine validation failed: {exc}"]
        if problems:
            raise HTTPException(400, {"errors": {"rule": "; ".join(str(p) for p in problems)}})
    return out


def _probe_text(probe: dict, key: str) -> str | None:
    """Same match text the real engine derives: command is ``<instrument>.<command>``, args is the
    sorted JSON dump."""
    if key == "tool":
        return probe.get("tool") or None
    if key == "command":
        inst, cmd = probe.get("instrument"), probe.get("command")
        if inst and cmd:
            return f"{inst}.{cmd}"
        return cmd or None
    if key == "path":
        return probe.get("path") or None
    if key == "args":
        return json.dumps(probe.get("args") or {}, sort_keys=True, default=str)
    return None


def dry_run_rules(rules: list[dict], probe: dict) -> dict:
    """Evaluate ``rules`` against a hypothetical action. Uses ``labwatcher.rules.RuleEngine`` when
    importable (identical semantics to Stage 1), else a local re-implementation."""
    rules_mod = _rules_module()
    engine_cls = getattr(rules_mod, "RuleEngine", None) if rules_mod else None
    if engine_cls is not None:
        engine = engine_cls.load({"rules": rules})
        action = {"tool": probe.get("tool"), "instrument": probe.get("instrument"), "command": probe.get("command"),
                  "path": probe.get("path"), "args": probe.get("args") or {}}
        hits = [h.to_dict() if hasattr(h, "to_dict") else dict(vars(h)) for h in engine.evaluate_all(action)]
        win = engine.evaluate(action)
        winner = (win.to_dict() if hasattr(win, "to_dict") else dict(vars(win))) if win else None
        by_id = {r.get("id"): r for r in rules}
        for h in hits:
            h.setdefault("id", h.get("rule_id"))
            h.setdefault("match", (by_id.get(h.get("rule_id")) or {}).get("match"))
        if winner:
            winner.setdefault("id", winner.get("rule_id"))
            winner.setdefault("match", (by_id.get(winner.get("rule_id")) or {}).get("match"))
        return {"probe": probe, "matches": hits, "winner": winner, "engine": "labwatcher.rules",
                "errors": list(getattr(engine, "errors", []) or [])}
    hits = []
    for r in rules:
        m = r.get("match") or {}
        if not m or r.get("enabled", True) is False:
            continue
        matched = {}
        ok = True
        for key, pattern in m.items():
            text = _probe_text(probe, key)
            try:
                found = re.search(pattern, text) if text is not None else None
            except re.error:
                found = None
            if not found:
                ok = False
                break
            matched[key] = found.group(0)
        if ok:
            hits.append({**r, "rule_id": r.get("id"), "matched": matched})
    hits.sort(key=lambda r: -int(r.get("priority") or 0))
    winner = hits[0] if hits else None
    return {"probe": probe, "matches": hits, "winner": winner, "engine": "ui", "errors": []}


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


def _settings_rows(obj) -> list[dict]:
    """Flat per-leaf rows ``{key, value, source, permission, locked}`` (``Settings.effective()``)."""
    fn = getattr(obj, "effective", None)
    if not callable(fn):
        return []
    try:
        rows = fn()
    except Exception:
        return []
    out = []
    for r in rows if isinstance(rows, (list, tuple)) else []:
        if isinstance(r, dict) and "key" in r:
            perm = str(r.get("permission") or "modifiable")
            out.append({"key": str(r["key"]), "value": r.get("value"), "source": str(r.get("source") or ""),
                        "permission": perm, "locked": bool(r.get("locked", perm == "locked"))})
    return out


def load_settings_view() -> dict:
    view = {"source": "fallback", "effective": None, "locks": {}, "errors": [], "warnings": [], "layers": [],
            "rows": []}
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
                view["rows"] = _settings_rows(obj)
                for env_name, label in (("LABWATCHER_ORG_SETTINGS", "org"), ("LABWATCHER_USER_SETTINGS", "user")):
                    if os.environ.get(env_name):
                        view.setdefault("layer_env", {})[label] = os.environ[env_name]
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

SEED_MODES = ("0", "off", "1", "demo", "quick", "fixture")


def seed_mode(seed) -> str:
    """Normalise the ``seed`` argument / ``LABWATCHER_SEED``: ``off`` | ``demo`` (full real seed via
    labwatcher.demo, mock graders) | ``quick`` (one card per env) | ``fixture`` (synthetic rows)."""
    if seed is None:
        seed = os.environ.get("LABWATCHER_SEED", "1")
    if seed is True:
        return "demo"
    if seed is False:
        return "off"
    s = str(seed).strip().lower()
    if s in ("0", "off", "false", "no", ""):
        return "off"
    if s in ("1", "demo", "true", "yes", "on"):
        return "demo"
    if s in ("quick", "fixture", "fixtures"):
        return "quick" if s == "quick" else "fixture"
    return "demo"


def seed_store(store, mode: str, warnings: list[str] | None = None) -> str:
    """Fill an *empty* store. Real Watcher-graded oracle sessions (``labwatcher.demo``) are preferred;
    the synthetic fixtures are only used when asked for explicitly or when demo.py cannot run.
    Returns the kind of data seeded (``demo`` | ``quick`` | ``fixture`` | ``none``)."""
    if mode == "off" or not fixtures.is_empty(store):
        return "none"
    if mode in ("demo", "quick"):
        try:
            from labwatcher import demo as _demo
            provider = os.environ.get("LABWATCHER_SEED_PROVIDER", "mock")
            _demo.seed_store(store, provider=provider, quick=(mode == "quick"), clear=False)
            return mode
        except Exception as exc:  # demo.py or an env failed: fall back so the UI still has data
            if warnings is not None:
                warnings.append(f"labwatcher.demo seeding failed ({exc.__class__.__name__}: {exc}); "
                                f"seeded synthetic fixtures instead")
    fixtures.seed_demo(store)
    return "fixture"


def create_app(store=None, policy_dir: Path | None = None, rules_dir: Path | None = None,
               seed: bool | str | None = None) -> FastAPI:
    """Build the FastAPI app. ``store`` defaults to ``labwatcher.store.Store`` (``LABWATCHER_DB``) and
    is opened lazily -- at ASGI startup or on the first request -- so constructing the app has no
    side effects on disk. ``seed`` (default from ``LABWATCHER_SEED``) fills an *empty* store at the
    same moment: ``True``/``"demo"`` runs the real oracle sessions through the Watcher
    (``python -m labwatcher.demo --seed``, mock graders), ``"quick"`` one card per env, ``"fixture"``
    the synthetic rows from ui/fixtures.py, ``False``/``"0"`` nothing."""
    warnings: list[str] = []
    mode = seed_mode(seed)
    boot_lock = threading.Lock()

    def ensure_store():
        """Open the store (once) and seed it if empty and seeding is enabled."""
        if app.state.store is not None and app.state.seeded:
            return app.state.store
        with boot_lock:
            if app.state.store is None:
                st, backend, warns = open_store(os.environ.get("LABWATCHER_DB"))
                warnings.extend(warns)
                app.state.store, app.state.backend = st, backend
            if not app.state.seeded:
                app.state.seeded = True
                try:
                    app.state.seeded_with = seed_store(app.state.store, mode, warnings)
                except Exception as exc:
                    app.state.seeded_with = "none"
                    warnings.append(f"seeding failed against {app.state.backend} store: {exc}")
        return app.state.store

    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(ensure_store)
        yield

    app = FastAPI(title="LabWatcher", version="0.1", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.backend = ("memory" if isinstance(store, MemoryStore) else "custom") if store is not None else None
    app.state.seeded = False
    app.state.seeded_with = "none"
    app.state.seed_mode = mode
    app.state.warnings = warnings
    app.state.policy_dir = Path(policy_dir or os.environ.get("LABWATCHER_POLICY_DIR") or PKG_DIR / "policies")
    app.state.rules_dir = Path(rules_dir or os.environ.get("LABWATCHER_RULES_DIR") or PKG_DIR / "rules")
    app.state.jobs: dict[str, dict] = {}
    app.state.broker = EscalationBroker()
    app.state.catalog = build_catalog()
    app.state.ensure_store = ensure_store
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def S():
        """The store (opened/seeded on first use when the lifespan did not run, e.g. bare TestClient)."""
        return ensure_store()

    def backend_name() -> str:
        return app.state.backend or "unopened"

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
        if S().session(sid) is None:
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
        S()
        rules = rules_status(app.state.rules_dir)
        degraded = [c for c, r in rules.items() if r["degraded"]]
        return {"ok": not degraded, "backend": backend_name(), "warnings": warnings,
                "seed_mode": app.state.seed_mode, "seeded_with": app.state.seeded_with,
                "demo_available": _demo_available()[0], "rules": rules, "rules_degraded": degraded,
                "live_waiting": app.state.broker.waiting()}

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
        return compute_summary(S(), context, days=max(2, min(days, 90)))

    @app.get("/api/sessions")
    def sessions(context: str | None = None, status: str | None = None, env: str | None = None,
                 flagged: bool | None = None, min_score: int | None = None, since: str | None = None,
                 until: str | None = None, sort: str = "date", order: str = "desc", limit: int = 500):
        if context:
            _check_context(context)
        rows = list_sessions(S(), context, status=status, env=env, flagged=flagged,
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
        d = session_detail(S(), sid)
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
            store = S()
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
                        yield _sse("hello", {"backend": backend_name(), "after": last})
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
        return {"pending": pending_escalations(S(), context)}

    @app.post("/api/escalations/{action_id}")
    async def resolve_escalation(action_id: int, request: Request):
        store = S()
        body = await _json_object(request, allow_empty=True)
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
        hid = None
        broker: EscalationBroker = app.state.broker
        if broker.resolve(action_id, decision, note or None):
            # a live session is blocked on this action: WatchedLab persists the verdict (action
            # row -> allow/deny, stage human, human_decisions row). Wait briefly for it so the
            # response's pending list is already up to date.
            live = True
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                row = await asyncio.to_thread(action_row, store, action_id)
                if row is not None and row.get("decision") != "escalate":
                    break
                await asyncio.sleep(0.05)
        else:
            live = False
            resolver = getattr(store, "resolve_escalation", None)
            if callable(resolver):
                try:
                    resolver(action_id, decision, note or None)
                except KeyError:
                    raise HTTPException(404, f"no action {action_id}")
            else:
                hid = store.add_human_decision(action["session_id"], action_id, verdict, note or None)
        return {"ok": True, "id": hid, "action_id": action_id, "session_id": action["session_id"],
                "decision": decision, "verdict": verdict, "note": note, "live": live,
                "pending": pending_escalations(store, action.get("context"))}

    # -- demo runs --------------------------------------------------------------------------------
    def _demo_available():
        try:
            from labwatcher.demo import run_demo  # type: ignore
        except Exception as exc:
            return False, f"labwatcher.demo.run_demo is not available ({exc.__class__.__name__}: {exc})"
        return True, run_demo

    def _human_timeout_s() -> float:
        try:
            from labwatcher.settings import Settings  # type: ignore
            v = Settings.load().human.get("timeout_s")
            return float(v) if v is not None else 120.0
        except Exception:
            return 120.0

    @app.post("/api/demo/run", status_code=202)
    async def demo_run(request: Request):
        body = await _json_object(request)
        context = _check_context(str(body.get("context", "")))
        env = str(body.get("env") or "")
        card = str(body.get("card") or "")
        script = str(body.get("script") or "honest")
        provider = str(body.get("provider") or "mock")
        human = str(body.get("human") or "approve").lower()
        if human == "interactive":
            human = "live"
        if human not in ("live", "approve", "deny", "timeout_allow"):
            raise HTTPException(400, "human must be live, approve, deny or timeout_allow")
        envs = app.state.catalog[context]["envs"]
        if env not in envs:
            raise HTTPException(400, f"unknown env {env!r} for {context}; expected one of {sorted(envs)}")
        if not card:
            raise HTTPException(400, f"card is required; expected one of {sorted(c['id'] for c in envs[env])}")
        if card not in {c["id"] for c in envs[env]}:
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
               "human": human, "status": "running", "session_id": None, "error": None,
               "started_at": datetime.now(timezone.utc).isoformat()}
        app.state.jobs[job_id] = job
        store = S()
        # Only pass the human-mode kwargs the runner understands (fake runners in tests may not).
        extra: dict = {}
        if human == "live":
            timeout_s = _human_timeout_s()
            extra = {"human_auto": "live", "on_escalate": app.state.broker.callback(timeout_s)}
        elif human != "approve":
            extra = {"human_auto": human}
        extra = _supported_kwargs(run_demo, extra)
        if human == "live" and "on_escalate" not in extra:
            raise HTTPException(501, "this labwatcher.demo.run_demo does not support live human review "
                                     "(no on_escalate parameter)")

        def worker():
            try:
                job["session_id"] = run_demo(context, env, card, script, provider=provider, store=store, **extra)
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
        body = await _json_object(request)
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
        errors: list[str] = []
        rules_mod = _rules_module()
        engine_cls = getattr(rules_mod, "RuleEngine", None) if rules_mod else None
        if engine_cls is not None:  # report rules the real engine would skip at load time
            try:
                errors = list(engine_cls.load({"rules": rules}).errors or [])
            except Exception as exc:
                errors = [f"rules engine could not load the file: {exc}"]
        return {"context": context, "path": str(path), "exists": path.is_file(), "count": len(rules),
                "decisions": RULE_DECISIONS, "match_keys": RULE_MATCH_KEYS, "categories": TAXONOMY_IDS,
                "errors": errors, "rules": rules}

    @app.post("/api/rules/{context}", status_code=201)
    async def add_rule(context: str, request: Request):
        _check_context(context)
        rule = validate_rule(await _json_object(request))
        rules, wrapper = _read_rules_file(app.state.rules_dir, context)
        if any(r.get("id") == rule["id"] for r in rules):
            raise HTTPException(409, f"rule {rule['id']!r} already exists; use PUT to edit it")
        rules.append(rule)
        _write_rules_file(app.state.rules_dir, context, rules, wrapper)
        return rule

    @app.put("/api/rules/{context}/{rule_id}")
    async def edit_rule(context: str, rule_id: str, request: Request):
        _check_context(context)
        body = await _json_object(request)
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
    def test_rules(context: str, tool: str = "", instrument: str = "", command: str = "", path: str = "",
                   args: str = ""):
        """Dry-run the rules against a hypothetical action with Stage 1 semantics: ``command`` is
        matched as ``<instrument>.<command>`` (``command=fume_hood.silence_alarm`` also accepted),
        ``args`` as the sorted JSON dump; highest priority wins, ties -> first."""
        rules, _ = _read_rules_file(app.state.rules_dir, _check_context(context))
        if command and not instrument and "." in command:
            instrument, command = command.split(".", 1)
        parsed_args: dict = {}
        if args:
            try:
                parsed_args = json.loads(args)
                if not isinstance(parsed_args, dict):
                    raise ValueError("args must be a JSON object")
            except ValueError as exc:
                raise HTTPException(400, f"args must be a JSON object: {exc}")
        if tool == "instrument":
            if instrument:
                parsed_args.setdefault("name", instrument)
            if command:
                parsed_args.setdefault("command", command)
        elif path:
            parsed_args.setdefault("path", path)
        probe = {"tool": tool, "instrument": instrument or None, "command": command or None,
                 "path": path or None, "args": parsed_args}
        return dry_run_rules(rules, probe)

    # -- settings ----------------------------------------------------------------------------------
    @app.get("/api/settings")
    def settings_api():
        S()
        view = load_settings_view()
        view["ui_warnings"] = warnings
        view["backend"] = backend_name()
        return view

    @app.exception_handler(HTTPException)
    async def http_exc(_request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, (dict, list)) else {"detail": exc.detail}
        if isinstance(detail, dict) and "detail" not in detail:
            detail = {"detail": "validation failed", **detail}
        return JSONResponse(detail, status_code=exc.status_code)

    return app


app = create_app()
