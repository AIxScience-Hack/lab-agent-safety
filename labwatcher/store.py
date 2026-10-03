"""SQLite persistence for LabWatcher (schema per SPEC.md; stdlib sqlite3, WAL, lock-guarded).

Every public method takes the lock, so a Store may be shared by the FastAPI app, the hooks
and the demo runner. JSON columns are decoded on read (``args``, ``categories``, ``triage``,
``evaluator``, ``scores``, ``result``).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "labwatcher.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    context TEXT NOT NULL,
    env TEXT NOT NULL,
    card TEXT,
    condition TEXT,
    arm TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    outcome TEXT,
    max_score INTEGER,
    flagged INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    escalated_count INTEGER NOT NULL DEFAULT 0,
    source TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    instrument TEXT,
    command TEXT,
    path TEXT,
    args_json TEXT,
    result TEXT,
    ok INTEGER,
    decision TEXT,
    stage TEXT,
    score INTEGER,
    rule_id TEXT,
    reason TEXT,
    categories_json TEXT,
    latency_ms INTEGER,
    triage_json TEXT,
    evaluator_json TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trailing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at_action INTEGER,
    scores_json TEXT,
    top_category TEXT,
    max_score INTEGER,
    reason TEXT,
    suggestion TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
    decision TEXT NOT NULL,
    note TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    query TEXT,
    result_json TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_actions_session ON actions(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_actions_decision ON actions(decision);
CREATE INDEX IF NOT EXISTS ix_transcript_session ON transcript(session_id, idx);
CREATE INDEX IF NOT EXISTS ix_trailing_session ON trailing(session_id, at_action);
CREATE INDEX IF NOT EXISTS ix_human_action ON human_decisions(action_id);
CREATE INDEX IF NOT EXISTS ix_sessions_context ON sessions(context, started_at);
"""

HACK_OUTCOMES = ("HACK", "HACK_DISCLOSED", "hack", "hack_disclosed")
SORTS = {
    "severity": "COALESCE(max_score, 0) DESC, flagged DESC, blocked_count DESC, started_at DESC",
    "date": "started_at DESC",
    "status": "status ASC, started_at DESC",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dumps(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, sort_keys=True, default=str)


def _loads(s: Any, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if is_dataclass(obj):
        return asdict(obj)
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


class Store:
    JSON_FIELDS = {"args_json": "args", "categories_json": "categories", "triage_json": "triage",
                   "evaluator_json": "evaluator", "scores_json": "scores", "result_json": "result"}

    def __init__(self, path: str | Path | None = None, flag_threshold: int = 7):
        self.path = Path(path) if path else DEFAULT_PATH
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flag_threshold = int(flag_threshold)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if str(self.path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    # --- helpers ------------------------------------------------------------------

    def _tx(self):
        return _Tx(self)

    def _row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for col, key in self.JSON_FIELDS.items():
            if col in d:
                d[key] = _loads(d.pop(col), [] if key == "categories" else None)
        if "flagged" in d:
            d["flagged"] = bool(d["flagged"])
        if "ok" in d and d["ok"] is not None:
            d["ok"] = bool(d["ok"])
        return d

    def _rows(self, cur) -> list[dict]:
        return [self._row(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- sessions -----------------------------------------------------------------

    def create_session(self, context: str, env: str, card: str | None = None,
                       condition: str | None = None, arm: str | None = None,
                       model: str | None = None, source: str | None = None,
                       id: str | None = None, started_at: str | None = None) -> str:
        sid = id or uuid.uuid4().hex[:12]
        with self._tx():
            self._conn.execute(
                "INSERT INTO sessions(id, context, env, card, condition, arm, model, started_at, status, source)"
                " VALUES (?,?,?,?,?,?,?,?,'running',?)",
                (sid, context, env, card, condition, arm, model, started_at or now_iso(), source))
        return sid

    def end_session(self, session_id: str, status: str = "completed", outcome: str | None = None,
                    ended_at: str | None = None) -> dict:
        with self._tx():
            self._conn.execute("UPDATE sessions SET status=?, outcome=COALESCE(?, outcome), ended_at=? WHERE id=?",
                               (status, outcome, ended_at or now_iso(), session_id))
            self._refresh_session(session_id)
            return self._row(self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def _refresh_session(self, session_id: str) -> None:
        """Recompute max_score / flagged / blocked_count / escalated_count from the rows."""
        c = self._conn
        a = c.execute(
            "SELECT MAX(score) AS mx, "
            "SUM(CASE WHEN decision='deny' THEN 1 ELSE 0 END) AS blocked, "
            "SUM(CASE WHEN decision='escalate' OR stage='human' THEN 1 ELSE 0 END) AS escalated "
            "FROM actions WHERE session_id=?", (session_id,)).fetchone()
        t = c.execute("SELECT MAX(max_score) AS mx FROM trailing WHERE session_id=?", (session_id,)).fetchone()
        outcome = c.execute("SELECT outcome FROM sessions WHERE id=?", (session_id,)).fetchone()
        scores = [x for x in (a["mx"], t["mx"]) if x is not None]
        max_score = max(scores) if scores else None
        blocked = int(a["blocked"] or 0)
        escalated = int(a["escalated"] or 0)
        flagged = int((max_score is not None and max_score >= self.flag_threshold) or blocked > 0
                      or (outcome is not None and outcome["outcome"] in HACK_OUTCOMES))
        c.execute("UPDATE sessions SET max_score=?, flagged=?, blocked_count=?, escalated_count=? WHERE id=?",
                  (max_score, flagged, blocked, escalated, session_id))

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    # --- actions ------------------------------------------------------------------

    def add_action(self, session_id: str, seq: int, tool: str, args: dict | None = None,
                   decision: Any = None, instrument: str | None = None, command: str | None = None,
                   path: str | None = None, result: str | None = None, ok: bool | None = None,
                   ts: str | None = None) -> int:
        """Record a graded tool call. `decision` may be a pipeline Decision dataclass or a dict
        with keys action/stage/score/rule_id/reason/categories/latency_ms/triage/evaluator."""
        d = _as_dict(decision)
        args = args or {}
        if tool == "instrument":
            instrument = instrument if instrument is not None else args.get("name")
            command = command if command is not None else args.get("command")
        if path is None and isinstance(args, dict):
            path = args.get("path")
        with self._tx():
            cur = self._conn.execute(
                "INSERT INTO actions(session_id, seq, tool, instrument, command, path, args_json, result, ok,"
                " decision, stage, score, rule_id, reason, categories_json, latency_ms, triage_json, evaluator_json, ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, int(seq), tool, instrument, command, path, _dumps(args), result,
                 None if ok is None else int(bool(ok)),
                 d.get("action") or d.get("decision"), d.get("stage"), d.get("score"), d.get("rule_id"),
                 d.get("reason"), _dumps(d.get("categories") or []), d.get("latency_ms"),
                 _dumps(d.get("triage")), _dumps(d.get("evaluator")), ts or now_iso()))
            self._refresh_session(session_id)
            return int(cur.lastrowid)

    def update_action_result(self, action_id: int, result: str | None, ok: bool | None) -> None:
        with self._tx():
            self._conn.execute("UPDATE actions SET result=?, ok=? WHERE id=?",
                               (result, None if ok is None else int(bool(ok)), action_id))

    def get_action(self, action_id: int) -> dict | None:
        with self._lock:
            return self._row(self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())

    def actions(self, session_id: str) -> list[dict]:
        with self._lock:
            return self._rows(self._conn.execute(
                "SELECT * FROM actions WHERE session_id=? ORDER BY seq, id", (session_id,)))

    # --- transcript ---------------------------------------------------------------

    def add_transcript(self, session_id: str, role: str | list | dict, content: str | None = None,
                       idx: int | None = None, ts: str | None = None) -> int | list[int]:
        """Append one message (role, content) or a list of {role, content} dicts. `idx` defaults
        to the next index in the session. Returns the row id(s)."""
        if isinstance(role, list):
            return [self.add_transcript(session_id, m) for m in role]
        if isinstance(role, dict):
            return self.add_transcript(session_id, role.get("role", "user"), role.get("content"),
                                       role.get("idx"), role.get("ts"))
        with self._tx():
            if idx is None:
                r = self._conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM transcript WHERE session_id=?",
                                       (session_id,)).fetchone()
                idx = int(r[0])
            cur = self._conn.execute(
                "INSERT INTO transcript(session_id, idx, role, content, ts) VALUES (?,?,?,?,?)",
                (session_id, int(idx), role, content if isinstance(content, str) or content is None
                 else json.dumps(content, default=str), ts or now_iso()))
            return int(cur.lastrowid)

    def transcript(self, session_id: str) -> list[dict]:
        with self._lock:
            return self._rows(self._conn.execute(
                "SELECT * FROM transcript WHERE session_id=? ORDER BY idx, id", (session_id,)))

    # --- trailing -----------------------------------------------------------------

    def add_trailing(self, session_id: str, at_action: int, scores: dict, reason: str | None = None,
                     suggestion: str | None = None, top_category: str | None = None,
                     max_score: int | None = None, ts: str | None = None) -> int:
        scores = {k: v for k, v in (scores or {}).items()}
        numeric = {k: v for k, v in scores.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if max_score is None:
            max_score = int(max(numeric.values())) if numeric else None
        if top_category is None and numeric:
            top_category = max(numeric.items(), key=lambda kv: kv[1])[0]
        with self._tx():
            cur = self._conn.execute(
                "INSERT INTO trailing(session_id, at_action, scores_json, top_category, max_score, reason, suggestion, ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (session_id, int(at_action), _dumps(scores), top_category, max_score, reason, suggestion, ts or now_iso()))
            self._refresh_session(session_id)
            return int(cur.lastrowid)

    def trailing(self, session_id: str) -> list[dict]:
        with self._lock:
            return self._rows(self._conn.execute(
                "SELECT * FROM trailing WHERE session_id=? ORDER BY at_action, id", (session_id,)))

    # --- human decisions ----------------------------------------------------------

    def add_human_decision(self, session_id: str, action_id: int | None, decision: str,
                           note: str | None = None, ts: str | None = None) -> int:
        with self._tx():
            cur = self._conn.execute(
                "INSERT INTO human_decisions(session_id, action_id, decision, note, ts) VALUES (?,?,?,?,?)",
                (session_id, action_id, decision, note, ts or now_iso()))
            return int(cur.lastrowid)

    def human_decisions(self, session_id: str, limit: int | None = None) -> list[dict]:
        """Human decisions of a session, oldest first, joined with the action they resolved."""
        q = ("SELECT h.*, a.seq AS action_seq, a.tool AS action_tool, a.instrument AS action_instrument,"
             " a.command AS action_command, a.score AS action_score, a.reason AS action_reason"
             " FROM human_decisions h LEFT JOIN actions a ON a.id = h.action_id"
             " WHERE h.session_id=? ORDER BY h.id")
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(q, (session_id,)).fetchall()]
        return rows[-limit:] if limit else rows

    def pending_escalations(self) -> list[dict]:
        """Escalated actions that no human decision has resolved yet, oldest first."""
        q = ("SELECT a.*, s.context, s.env, s.card FROM actions a JOIN sessions s ON s.id = a.session_id"
             " WHERE a.decision='escalate' AND NOT EXISTS"
             " (SELECT 1 FROM human_decisions h WHERE h.action_id = a.id) ORDER BY a.id")
        with self._lock:
            return self._rows(self._conn.execute(q))

    def resolve_escalation(self, action_id: int, decision: str, note: str | None = None) -> dict:
        """Record the human verdict (approve/allow -> allow, deny -> deny) and update the action."""
        verdict = {"approve": "allow", "allow": "allow", "deny": "deny", "reject": "deny"}.get(str(decision).lower())
        if verdict is None:
            raise ValueError(f"decision must be approve/allow or deny, got {decision!r}")
        with self._tx():
            row = self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            self._conn.execute(
                "INSERT INTO human_decisions(session_id, action_id, decision, note, ts) VALUES (?,?,?,?,?)",
                (row["session_id"], action_id, verdict, note, now_iso()))
            reason = row["reason"] or ""
            if note:
                reason = f"{reason} [human: {note}]".strip()
            self._conn.execute("UPDATE actions SET decision=?, stage='human', reason=? WHERE id=?",
                               (verdict, reason, action_id))
            self._refresh_session(row["session_id"])
            return self._row(self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone())

    # --- enrichment ---------------------------------------------------------------

    def add_enrichment(self, session_id: str, source: str, query: str | None, result: Any,
                       ts: str | None = None) -> int:
        with self._tx():
            cur = self._conn.execute(
                "INSERT INTO enrichment(session_id, source, query, result_json, ts) VALUES (?,?,?,?,?)",
                (session_id, source, query, _dumps(result), ts or now_iso()))
            return int(cur.lastrowid)

    def enrichment(self, session_id: str) -> list[dict]:
        with self._lock:
            return self._rows(self._conn.execute(
                "SELECT * FROM enrichment WHERE session_id=? ORDER BY id", (session_id,)))

    # --- analyzer queries ---------------------------------------------------------

    def summary(self, context: str | None = None) -> dict:
        where, params = ("WHERE s.context=?", (context,)) if context else ("", ())
        with self._lock:
            c = self._conn
            s = c.execute(f"SELECT COUNT(*) AS n, SUM(flagged) AS flagged FROM sessions s {where}", params).fetchone()
            a = c.execute(
                "SELECT SUM(CASE WHEN a.decision='deny' THEN 1 ELSE 0 END) AS blocked, "
                "SUM(CASE WHEN a.decision='escalate' OR a.stage='human' THEN 1 ELSE 0 END) AS escalated "
                f"FROM actions a JOIN sessions s ON s.id = a.session_id {where}", params).fetchone()
            cats: dict[str, int] = {}
            for r in c.execute(
                    "SELECT a.categories_json AS cj FROM actions a JOIN sessions s ON s.id = a.session_id "
                    f"{where}{' AND' if where else ' WHERE'} a.decision IN ('deny','escalate') "
                    "OR (a.stage='human' AND a.decision='deny')", params):
                for cat in _loads(r["cj"], []) or []:
                    cats[cat] = cats.get(cat, 0) + 1
            for r in c.execute(
                    "SELECT t.top_category AS tc FROM trailing t JOIN sessions s ON s.id = t.session_id "
                    f"{where}{' AND' if where else ' WHERE'} t.max_score >= ? AND t.top_category IS NOT NULL",
                    params + (self.flag_threshold,)):
                cats[r["tc"]] = cats.get(r["tc"], 0) + 1
            trend = [{"day": r["day"], "sessions": int(r["n"]), "flagged": int(r["flagged"] or 0)}
                     for r in c.execute(
                         "SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS n, SUM(flagged) AS flagged "
                         f"FROM sessions s {where} GROUP BY day ORDER BY day", params)]
            by_outcome = {(r["outcome"] if r["outcome"] is not None else "unknown"): int(r["n"])
                          for r in c.execute(
                              f"SELECT outcome, COUNT(*) AS n FROM sessions s {where} GROUP BY outcome", params)}
            by_status = {r["status"]: int(r["n"]) for r in c.execute(
                f"SELECT status, COUNT(*) AS n FROM sessions s {where} GROUP BY status", params)}
        n = int(s["n"] or 0)
        flagged = int(s["flagged"] or 0)
        return {
            "context": context,
            "sessions": n,
            "blocked_actions": int(a["blocked"] or 0),
            "escalated_actions": int(a["escalated"] or 0),
            "flagged_sessions": flagged,
            "failure_rate": round(flagged / n, 4) if n else 0.0,
            "by_category": dict(sorted(cats.items(), key=lambda kv: (-kv[1], kv[0]))),
            "trend": trend,
            "by_outcome": by_outcome,
            "by_status": by_status,
        }

    def sessions(self, context: str | None = None, min_score: int | None = None,
                 status: str | None = None, since: str | None = None, env: str | None = None,
                 flagged: bool | None = None, sort: str = "severity", limit: int = 200) -> list[dict]:
        clauses, params = [], []
        if context:
            clauses.append("context=?"); params.append(context)
        if env:
            clauses.append("env=?"); params.append(env)
        if min_score is not None:
            clauses.append("COALESCE(max_score, 0) >= ?"); params.append(int(min_score))
        if status:
            clauses.append("status=?"); params.append(status)
        if since:
            clauses.append("started_at >= ?"); params.append(since)
        if flagged is not None:
            clauses.append("flagged=?"); params.append(int(bool(flagged)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = SORTS.get(sort, SORTS["severity"])
        with self._lock:
            return self._rows(self._conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY {order} LIMIT ?", (*params, int(limit))))

    def session(self, session_id: str) -> dict | None:
        sess = self.get_session(session_id)
        if sess is None:
            return None
        return {
            "session": sess,
            "actions": self.actions(session_id),
            "transcript": self.transcript(session_id),
            "trailing": self.trailing(session_id),
            "human_decisions": self.human_decisions(session_id),
            "enrichment": self.enrichment(session_id),
        }

    def delete_session(self, session_id: str) -> bool:
        with self._tx():
            cur = self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return cur.rowcount > 0


class _Tx:
    """`with store._tx():` -> lock + BEGIN IMMEDIATE / COMMIT (ROLLBACK on error)."""

    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        try:
            if not self.store._conn.in_transaction:
                self.store._conn.execute("BEGIN IMMEDIATE")
                self.owner = True
            else:
                self.owner = False
        except Exception:
            self.store._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.owner:
                if exc_type is None:
                    self.store._conn.execute("COMMIT")
                else:
                    self.store._conn.execute("ROLLBACK")
        finally:
            self.store._lock.release()
        return False
