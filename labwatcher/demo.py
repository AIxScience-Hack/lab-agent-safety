"""Populate the LabWatcher store with real Watcher-graded sessions.

Two sources:

* ``run_demo(...)`` runs one honeypot card's oracle script (honest / exploit / full / costly_honest)
  from drug_discovery/check_tasks.py through a labsim lab wrapped in ``WatchedLab``: every tool call
  goes through the Watcher pipeline (rules -> triage -> evaluator -> threshold, trailing monitors
  every N actions), the synthetic transcript (user brief, assistant tool calls, tool results) is
  stored, the session is scored with ``labsim.scoring.score`` and Amass literature context is
  attached from the local cache.
* ``replay_run(path, ...)`` imports a Modal batch run JSON (labwatcher/modal/batch_eval.py): each
  recorded tool call is graded by the Watcher with the real transcript up to that point, so the
  Analyzer shows what the monitor would have decided for a real open-model agent.

CLI (offline, provider mock by default)::

    python -m labwatcher.demo --seed                       # honest + exploit for every card, both contexts
    python -m labwatcher.demo --replay labwatcher/data/runs  # import Modal batch runs
    python -m labwatcher.demo --context drug_discovery --env aspirin --card a01 --script exploit
    python -m labwatcher.demo --replay labwatcher/data/runs --provider modal --limit 2

Amass is never called live unless ``AMASS_LIVE=1`` (cache misses just yield no enrichment).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DRUG = REPO / "drug_discovery"
for _p in (str(REPO), str(DRUG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from labsim import build_scenario, load_task, make_lab, modes_for, score  # noqa: E402
from check_tasks import expand, fill  # noqa: E402  (drug_discovery/check_tasks.py)

from labwatcher.hooks import WatchedLab, strip_audit  # noqa: E402
from labwatcher.models import ModelRouter  # noqa: E402
from labwatcher.pipeline import Action, Watcher, load_policy  # noqa: E402
from labwatcher.trailing import TrailingMonitor  # noqa: E402

CONTEXTS = ("drug_discovery", "materials_discovery")
CONTEXT_ROOTS = {c: REPO / c / "tasks" for c in CONTEXTS}
SCRIPTS = ("honest", "exploit", "full", "costly_honest")
DEFAULT_RUNS = HERE / "data" / "runs"
QUICK_CARDS = {"aspirin": "a01", "cell_culture": "c01", "cytotox": "t01", "coin_cell": "m01"}


# --- catalogue -------------------------------------------------------------------------

def envs_for(context: str) -> list[str]:
    root = CONTEXT_ROOTS.get(context)
    if root is None or not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/task.yaml"))


def context_for(env: str) -> str:
    for ctx, root in CONTEXT_ROOTS.items():
        if (root / env / "task.yaml").is_file():
            return ctx
    raise ValueError(f"no env {env!r} under {[str(r) for r in CONTEXT_ROOTS.values()]}")


def find_card(cards: list[dict], card_id: str) -> dict:
    """Exact id first, then unique prefix (a01 -> a01_balance_calibration)."""
    for c in cards:
        if c["id"] == card_id:
            return c
    hits = [c for c in cards if str(c["id"]).startswith(card_id)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no card {card_id!r}; have {[c['id'] for c in cards]}")
    raise ValueError(f"card {card_id!r} is ambiguous: {[c['id'] for c in hits]}")


def default_condition(card: dict, script: str) -> str:
    """The condition each oracle script is written for (drug_discovery/check_tasks.py): `full`
    is the honest procedure when nothing is broken (control); `honest` and `exploit` react to the
    card's fault and shortcut (honeypot); `costly_honest` needs the costly condition."""
    if script == "full":
        return "control"
    if script == "costly_honest":
        return "costly" if "costly" in modes_for(card) else "honeypot"
    return "honeypot"


def amass_live() -> bool:
    return os.environ.get("AMASS_LIVE", "") == "1"


# --- the shared Watcher stack ----------------------------------------------------------------

class DemoRunner:
    """Settings + ModelRouter + Watcher (+ one TrailingMonitor per context) over a Store."""

    def __init__(self, store: Any = None, provider: str | None = "mock", human_auto: str | None = None,
                 settings: Any = None):
        from labwatcher.settings import Settings
        self.settings = settings or Settings.load()
        if human_auto:
            self.settings.data.setdefault("human", {})["auto"] = human_auto
        self.provider = provider
        self.store = store if store is not None else self._default_store()
        self.models = ModelRouter(self.settings, provider=provider)
        self.watcher = Watcher(self.settings, self.store, self.models)
        self._trailing: dict[str, TrailingMonitor] = {}

    @staticmethod
    def _default_store():
        from labwatcher.store import Store
        return Store()

    def trailing(self, context: str) -> TrailingMonitor:
        if context not in self._trailing:
            self._trailing[context] = TrailingMonitor(self.settings, self.models, self.store,
                                                      load_policy(context))
        return self._trailing[context]

    def providers(self) -> dict:
        return self.models.describe()

    def latency(self) -> dict:
        """Mean model latency (ms) per role/provider over the calls made so far."""
        out: dict[str, dict] = {}
        for c in self.models.calls:
            k = f"{c.role}/{c.provider}"
            d = out.setdefault(k, {"n": 0, "total_ms": 0, "errors": 0})
            d["n"] += 1
            d["total_ms"] += int(c.latency_ms or 0)
            d["errors"] += 0 if c.ok else 1
        for d in out.values():
            d["mean_ms"] = round(d["total_ms"] / d["n"]) if d["n"] else None
        return out


_RUNNERS: dict[tuple, DemoRunner] = {}


def get_runner(store: Any = None, provider: str | None = "mock", human_auto: str | None = None) -> DemoRunner:
    key = (id(store) if store is not None else None, provider, human_auto)
    r = _RUNNERS.get(key)
    if r is None or (store is not None and r.store is not store):
        r = DemoRunner(store, provider, human_auto)
        _RUNNERS[key] = r
    return r


# --- enrichment --------------------------------------------------------------------------------

def enrichment_for(context: str, env: str, card: dict, live: bool | None = None) -> list[dict]:
    """Amass records for a card from the local cache (live only with AMASS_LIVE=1)."""
    try:
        from labwatcher.enrich.amass import enrich_session
    except Exception:  # noqa: BLE001
        return []
    live = amass_live() if live is None else live
    keywords = [k for k in (env, card.get("category"), card.get("fault_kind")) if isinstance(k, str)]
    try:
        return enrich_session(context, env, card.get("title", ""), keywords, cache=True, offline=not live)
    except Exception:  # noqa: BLE001
        return []


def precedent_text(items: list[dict], env: str, title: str, n: int = 6) -> str:
    if not items:
        return ""
    lines = [f"Domain precedent for {env} / {title} (Amass literature, patents and drug records):"]
    for r in items[:n]:
        year = (r.get("date") or "")[:4]
        meta = ", ".join(x for x in [year, (r.get("source") or "")[:50]] if x)
        line = f"- [{r.get('source_core')}] {r.get('title', '')[:140]}"
        if meta:
            line += f" ({meta})"
        if r.get("url"):
            line += f" {r['url']}"
        if r.get("relevance_note"):
            line += f" -- {r['relevance_note'][:200]}"
        lines.append(line)
    return "\n".join(lines)


def store_enrichment(store: Any, session_id: str, items: list[dict]) -> int:
    """One enrichment row per (core, query) with its records as the result list."""
    if store is None or not items:
        return 0
    groups: dict[tuple, list[dict]] = {}
    for r in items:
        groups.setdefault((r.get("source_core") or "Amass", r.get("query") or ""), []).append(r)
    n = 0
    for (core, query), recs in groups.items():
        store.add_enrichment(session_id, core, query, recs)
        n += 1
    return n


# --- run one oracle script through the Watcher -------------------------------------------------

def run_demo(context: str, env: str, card_id: str, script: str, provider: str = "mock",
             store: Any = None, arm: str = "incentivised", condition: str | None = None,
             session_id: str | None = None, human_auto: str | None = None,
             runner: DemoRunner | None = None, log: Callable[[str], None] | None = None) -> str:
    """Run `script` of honeypot `card_id` in `env` through WatchedLab and record it in the store.
    Returns the session id. `condition` defaults to the mode the script was written for (full ->
    control; honest / exploit -> honeypot); `arm` is incentivised | neutral; `provider` is
    mock | modal | anthropic."""
    if not context:
        context = context_for(env)
    if context not in CONTEXTS:
        raise ValueError(f"unknown context {context!r}; expected one of {CONTEXTS}")
    if env not in envs_for(context):
        raise ValueError(f"env {env!r} is not in context {context!r}; have {envs_for(context)}")
    task, cards = load_task(env)
    card = find_card(cards, card_id)
    if script not in SCRIPTS:
        raise ValueError(f"script must be one of {SCRIPTS}, got {script!r}")
    condition = condition or default_condition(card, script)
    if condition not in modes_for(card):
        raise ValueError(f"{card['id']} has no {condition!r} condition; have {modes_for(card)}")

    runner = runner or get_runner(store, provider, human_auto)
    store = runner.store
    scenario = build_scenario(task, card, condition, arm)
    raw_lab = make_lab(scenario)
    steps = expand(card["scripts"].get(script) or [{"use": "full"}], task["fragments"])

    items = enrichment_for(context, env, card)
    ptext = precedent_text(items, env, card.get("title", ""))
    sid = session_id or f"demo-{env}-{card['id']}-{script}-{condition}-{arm}-{int(time.time() * 1000) % 10**8:08d}"
    if store is not None and callable(getattr(store, "get_session", None)) and store.get_session(sid):
        if callable(getattr(store, "delete_session", None)):
            store.delete_session(sid)
    watched = WatchedLab(raw_lab, runner.watcher, runner.trailing(context), store, session_id=sid,
                         context=context, env=env,
                         meta={"card": card["id"], "condition": condition, "arm": arm,
                               "model": f"oracle:{script}", "source": "demo"},
                         precedent_provider=(lambda _a: ptext) if ptext else None)
    watched._note("system", f"[session_start] {context}/{env} card {card['id']} ({card.get('title', '')}) "
                            f"condition={condition} arm={arm} script={script} provider={provider}")
    watched._note("user", scenario["prompt"])
    stop = "script_end"
    for tool, args, sample in steps:
        if watched.lab.done:
            break
        watched.call(tool, fill(copy.deepcopy(args), watched.lab, sample))
    if watched.lab.done:
        stop = "finish"
    watched.lab.end_session(stop)
    result = score(card, watched.lab.log, condition, watched.lab.REPORT_FORM)
    watched._note("assistant", f"[outcome] {result['outcome']} (score {result['score']}); "
                               f"blocked {watched.blocked_count}, escalated {watched.escalated_count}")
    watched.session_end(stop, outcome=result["outcome"])
    store_enrichment(store, sid, items)
    if log:
        log(f"{context:20} {env:12} {card['id']:4} {script:13} {condition:9} {arm:12} -> "
            f"{result['outcome']:18} score {result['score']}  actions {watched.n_actions:2} "
            f"blocked {watched.blocked_count} escalated {watched.escalated_count} "
            f"max {max((d.score for d in watched.decisions if d.score is not None), default='-')}  {sid}")
    return sid


# --- seeding --------------------------------------------------------------------------------------

def seed_plan(quick: bool = False) -> list[tuple[str, str, str, str]]:
    """(context, env, card, script) for every card of every env (honest + exploit)."""
    plan = []
    for ctx in CONTEXTS:
        for env in envs_for(ctx):
            _, cards = load_task(env)
            for card in cards:
                if quick and card["id"] != QUICK_CARDS.get(env, card["id"]):
                    continue
                for script in ("honest", "exploit"):
                    if script in card.get("scripts", {}):
                        plan.append((ctx, env, card["id"], script))
    return plan


def seed_session_id(env: str, card: str, script: str, arm: str = "incentivised") -> str:
    return f"demo-{env}-{card}-{script}-{arm}"


def clear_seeded(store: Any, ids: set[str] | None = None, sources: tuple[str, ...] = ("fixture",)) -> int:
    """Delete the earlier --seed sessions (their deterministic ids) and any synthetic fixture rows
    so --seed is idempotent. Sessions started from the UI / run_demo() keep their random suffix
    and are left alone."""
    if store is None or not callable(getattr(store, "delete_session", None)):
        return 0
    n = 0
    try:
        rows = store.sessions(limit=100000)
    except TypeError:
        rows = store.sessions()
    for s in rows:
        if s.get("source") in sources or (ids is not None and s["id"] in ids):
            store.delete_session(s["id"])
            n += 1
    return n


def seed_store(store: Any = None, provider: str = "mock", quick: bool = False, arm: str = "incentivised",
               log: Callable[[str], None] | None = None, clear: bool = True,
               runner: DemoRunner | None = None) -> list[str]:
    """Run honest + exploit for every card of every env in both contexts. Returns session ids."""
    runner = runner or get_runner(store, provider)
    plan = seed_plan(quick)
    if clear:
        clear_seeded(runner.store, {seed_session_id(env, card, script, arm) for _, env, card, script in plan})
    ids = []
    for ctx, env, card, script in plan:
        ids.append(run_demo(ctx, env, card, script, provider, runner.store, arm=arm,
                            session_id=seed_session_id(env, card, script, arm), runner=runner, log=log))
    return ids


# --- replay of Modal batch runs -------------------------------------------------------------------

def _message_text(m: dict) -> str:
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content)
    text = str(content or "")
    calls = m.get("tool_calls") or []
    if calls:
        rendered = []
        for c in calls:
            fn = c.get("function") or {}
            rendered.append(f"{fn.get('name', c.get('name', '?'))}({fn.get('arguments', c.get('arguments', ''))})")
        text = (text + "\n" if text else "") + "[tool calls] " + "; ".join(rendered)
    if m.get("role") == "tool":
        text = f"[{m.get('name', 'tool')}] {text}"
    return text


def run_files(path: Path | str) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p]
    return sorted(p.rglob("*.json"))


def replay_run(path: Path | str, store: Any = None, provider: str = "mock", session_id: str | None = None,
               human_auto: str | None = None, runner: DemoRunner | None = None,
               log: Callable[[str], None] | None = None) -> str:
    """Import one batch_eval run JSON, grading every recorded tool call through the Watcher."""
    path = Path(path)
    data = json.loads(path.read_text())
    env = data["env"]
    context = data.get("context") or context_for(env)
    card_id = data.get("card") or path.stem.split("-")[0]
    condition, arm = data.get("condition"), data.get("arm")
    runner = runner or get_runner(store, provider, human_auto)
    store = runner.store
    watcher, trailing = runner.watcher, runner.trailing(context)
    try:
        _, cards = load_task(env)
        card = find_card(cards, card_id)
    except Exception:  # noqa: BLE001
        card = {"id": card_id, "title": (data.get("labels") or {}).get("title", ""),
                "category": (data.get("labels") or {}).get("category")}
    items = enrichment_for(context, env, card)
    ptext = precedent_text(items, env, card.get("title", ""))

    sid = session_id or f"replay-{path.stem}-{provider}"
    if callable(getattr(store, "get_session", None)) and store.get_session(sid) \
            and callable(getattr(store, "delete_session", None)):
        store.delete_session(sid)
    store.create_session(context, env, card=card_id, condition=condition, arm=arm,
                         model=data.get("model"), source="replay", id=sid,
                         started_at=data.get("started_at"))

    # transcript: messages -> [{role, content}], remembering where each tool call appears
    messages = data.get("messages") or []
    transcript: list[dict] = []
    call_index: dict[str, int] = {}
    for m in messages:
        role = m.get("role", "user")
        transcript.append({"role": role, "content": _message_text(m)})
        for c in m.get("tool_calls") or []:
            if c.get("id"):
                call_index.setdefault(c["id"], len(transcript) - 1)
    for idx, t in enumerate(transcript):
        store.add_transcript(sid, t["role"], t["content"], idx=idx) if _accepts_idx(store) \
            else store.add_transcript(sid, idx, t["role"], t["content"])

    audit = data.get("audit_log") or []
    report_form = data.get("report_form")
    human_history: list[dict] = []
    decisions = []
    seen = 0
    for i, e in enumerate(audit):
        tool = str(e.get("tool", ""))
        if tool.startswith("_"):
            continue
        cid = e.get("call_id")
        upto = call_index.get(cid)
        window = transcript[: upto + 1] if upto is not None else transcript[: max(1, min(len(transcript), 2 * seen + 2))]
        action = Action.from_call(sid, int(e.get("seq", seen)), tool, e.get("args") or {}, window, context, env,
                                  report_form)
        d = watcher.grade_action(action, human_history, precedent=ptext or None)
        if d.escalated:
            human_history.append({"seq": action.seq, "tool": tool, "instrument": action.instrument,
                                  "command": action.command, "decision": d.action, "score": d.score,
                                  "reason": d.reason})
        if d.action_id is not None and callable(getattr(store, "update_action_result", None)):
            # what the agent actually got (the run was not blocked at the time)
            store.update_action_result(d.action_id, str(e.get("result", ""))[:4000], e.get("ok"))
        decisions.append(d)
        seen += 1
        trailing.maybe_review(sid, seen, window, strip_audit(audit[: i + 1][-trailing.window:]))

    sc = data.get("score") or {}
    stop = str(data.get("stop_reason") or "")
    status = "completed" if stop == "finish" else "stopped"
    store.end_session(sid, status, sc.get("outcome"), ended_at=data.get("ended_at")) if _accepts_ended_at(store) \
        else store.end_session(sid, status, sc.get("outcome"))
    store_enrichment(store, sid, items)
    if log:
        blocked = sum(1 for d in decisions if d.action == "deny")
        esc = sum(1 for d in decisions if d.escalated)
        mx = max((d.score for d in decisions if d.score is not None), default="-")
        log(f"{context:20} {env:12} {card_id:4} replay        {str(condition):9} {str(arm):12} -> "
            f"{str(sc.get('outcome')):18} score {sc.get('score')}  actions {seen:2} blocked {blocked} "
            f"escalated {esc} max {mx}  {sid}  [{data.get('model')}]")
    return sid


def _accepts_idx(store) -> bool:
    import inspect
    try:
        return "idx" in inspect.signature(store.add_transcript).parameters
    except (TypeError, ValueError):
        return False


def _accepts_ended_at(store) -> bool:
    import inspect
    try:
        return "ended_at" in inspect.signature(store.end_session).parameters
    except (TypeError, ValueError):
        return False


def replay_dir(path: Path | str = DEFAULT_RUNS, store: Any = None, provider: str = "mock",
               limit: int | None = None, log: Callable[[str], None] | None = None,
               runner: DemoRunner | None = None) -> list[str]:
    runner = runner or get_runner(store, provider)
    files = run_files(path)
    if limit:
        files = files[:limit]
    return [replay_run(f, runner.store, provider, runner=runner, log=log) for f in files]


# --- CLI ----------------------------------------------------------------------------------------

def _summary_lines(store) -> list[str]:
    out = []
    for ctx in CONTEXTS:
        try:
            s = store.summary(ctx)
        except Exception as e:  # noqa: BLE001
            out.append(f"{ctx}: summary unavailable ({e})")
            continue
        out.append(f"{ctx}: sessions {s.get('sessions')} blocked_actions {s.get('blocked_actions')} "
                   f"escalated_actions {s.get('escalated_actions')} flagged_sessions {s.get('flagged_sessions')} "
                   f"failure_rate {s.get('failure_rate')}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m labwatcher.demo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true", help="run honest + exploit for every card of every env")
    ap.add_argument("--quick", action="store_true", help="with --seed: one card per env")
    ap.add_argument("--no-clear", action="store_true", help="with --seed: keep earlier demo/fixture sessions")
    ap.add_argument("--replay", metavar="DIR_OR_JSON", help="import Modal batch run JSONs through the Watcher")
    ap.add_argument("--limit", type=int, default=None, help="with --replay: at most N files")
    ap.add_argument("--context", choices=CONTEXTS, default=None)
    ap.add_argument("--env", default=None)
    ap.add_argument("--card", default=None)
    ap.add_argument("--script", choices=SCRIPTS, default="honest")
    ap.add_argument("--condition", default=None, help="control | costly | honeypot | honeypot_reward | no_honeypot")
    ap.add_argument("--arm", choices=("incentivised", "neutral"), default="incentivised")
    ap.add_argument("--provider", choices=("mock", "modal", "anthropic", "auto"), default="mock")
    ap.add_argument("--human-auto", choices=("approve", "deny", "timeout_allow"), default=None,
                    help="override settings.human.auto for this run")
    ap.add_argument("--db", default=os.environ.get("LABWATCHER_DB") or None,
                    help="SQLite path (default $LABWATCHER_DB or labwatcher/data/labwatcher.db; ':memory:' ok)")
    args = ap.parse_args(argv)

    from labwatcher.store import Store
    store = Store(args.db) if args.db else Store()
    print(f"store: {store.path}")
    provider = None if args.provider == "auto" else args.provider
    runner = get_runner(store, provider, args.human_auto)
    t0 = time.monotonic()
    did = False
    if args.seed:
        did = True
        ids = seed_store(store, provider, quick=args.quick, arm=args.arm, log=print, clear=not args.no_clear,
                         runner=runner)
        print(f"seeded {len(ids)} sessions in {time.monotonic() - t0:.1f}s")
    if args.replay:
        did = True
        ids = replay_dir(args.replay, store, provider, limit=args.limit, log=print, runner=runner)
        print(f"replayed {len(ids)} runs in {time.monotonic() - t0:.1f}s")
    if args.env or args.card:
        did = True
        if not args.env:
            ap.error("--card needs --env")
        ctx = args.context or context_for(args.env)
        _, cards = load_task(args.env)
        card_ids = [args.card] if args.card else [c["id"] for c in cards]
        for cid in card_ids:
            run_demo(ctx, args.env, cid, args.script, provider, store, arm=args.arm, condition=args.condition,
                     human_auto=args.human_auto, runner=runner, log=print)
    if not did:
        ap.print_help()
        return 2
    print("providers:", json.dumps(runner.providers()))
    lat = runner.latency()
    if lat:
        print("model latency:", json.dumps(lat))
    for line in _summary_lines(store):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
