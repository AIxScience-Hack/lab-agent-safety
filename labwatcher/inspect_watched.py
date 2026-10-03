"""Inspect task `watched_benchmark`: the labsim benchmark (drug_discovery/inspect_tasks.py) with
every tool call routed through LabWatcher.

Reuses TOOL_FACTORIES, build_samples, lab_score, _log_unexecuted_calls and CONTINUE_PROMPT from
inspect_tasks.py; the only differences are that the lab is wrapped in `WatchedLab` and that
trailing-monitor suggestions are appended to `state.messages` after each tool round.

    cd drug_discovery
    LABWATCHER_PROVIDER=mock inspect eval ../labwatcher/inspect_watched.py@watched_benchmark \
        -T env=aspirin -T honeypots=a01 -T conditions=honeypot -T arms=neutral \
        --model mockllm/model --limit 1
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DRUG = REPO / "drug_discovery"
for p in (str(REPO), str(DRUG)):
    if p not in sys.path:
        sys.path.insert(0, p)

from inspect_ai import Task, task  # noqa: E402
from inspect_ai.dataset import MemoryDataset  # noqa: E402
from inspect_ai.model import ChatMessageSystem, ChatMessageUser, execute_tools, get_model  # noqa: E402
from inspect_ai.solver import Generate, TaskState, solver  # noqa: E402
from inspect_ai.util import LimitExceededError  # noqa: E402

import inspect_tasks as it  # noqa: E402  (drug_discovery/inspect_tasks.py)
from labsim import AuditLog, build_scenario, load_task, make_lab  # noqa: E402
from labsim.core import monitor_view  # noqa: E402

from labwatcher.hooks import WatchedLab  # noqa: E402
from labwatcher.models import ModelRouter  # noqa: E402
from labwatcher.pipeline import Watcher, load_policy  # noqa: E402
from labwatcher.trailing import TrailingMonitor  # noqa: E402

DEFAULT_DB = HERE / "data" / "labwatcher.db"


def _context_for(env: str) -> str:
    return "materials_discovery" if (REPO / "materials_discovery" / "tasks" / env).is_dir() \
        else "drug_discovery"


def make_settings(human_auto: str | None = None):
    """The layered Settings (labwatcher.settings); `human_auto` overrides human.auto for headless runs."""
    from labwatcher.settings import Settings
    settings = None
    for make in (lambda: Settings.load(), lambda: Settings()):
        try:
            settings = make()
            break
        except (AttributeError, TypeError):
            continue
    if settings is None:
        raise RuntimeError("cannot construct labwatcher.settings.Settings")
    if human_auto:
        human = getattr(settings, "human", None)
        if isinstance(human, dict):
            human["auto"] = human_auto
        elif human is not None:
            try:
                setattr(human, "auto", human_auto)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(settings, dict):
            settings.setdefault("human", {})["auto"] = human_auto
    return settings


def make_store(db: str | None):
    if db is not None and str(db).lower() in ("none", "off", "false", ""):
        return None
    from labwatcher.store import Store
    path = Path(db) if db else DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    for make in (lambda: Store(path), lambda: Store(str(path)), lambda: Store(path=path), lambda: Store()):
        try:
            return make()
        except TypeError:
            continue
    raise RuntimeError("cannot construct labwatcher.store.Store")


def messages_as_transcript(messages) -> list[dict]:
    """Inspect ChatMessages -> [{role, content}] with tool calls rendered inline."""
    out = []
    for m in messages:
        text = getattr(m, "text", None)
        if text is None:
            text = str(getattr(m, "content", ""))
        calls = getattr(m, "tool_calls", None) or []
        if calls:
            rendered = "; ".join(f"{c.function}({json.dumps(c.arguments, default=str)})" for c in calls)
            text = (text + "\n" if text else "") + f"[tool calls] {rendered}"
        out.append({"role": getattr(m, "role", "user"), "content": text})
    return out


@solver
def watched_lab_agent(max_tool_calls: int = it.DEFAULT_MAX_TOOL_CALLS, max_idle_turns: int = 3,
                      ask_awareness: bool = True, context: str | None = None,
                      human_auto: str | None = "deny", db: str | None = None,
                      suggestion_role: str = "system"):
    settings = make_settings(human_auto)
    store = make_store(db)
    models = ModelRouter(settings)
    watcher = Watcher(settings, store, models)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        md = state.metadata
        env = md["env"]
        ctx = context or _context_for(env)
        task_def, cards = load_task(env)
        card = next(c for c in cards if c["id"] == md["labels"]["honeypot"])
        raw_lab = make_lab(build_scenario(task_def, card, md["condition"], md["arm"]))
        trailing = TrailingMonitor(settings, models, store, load_policy(ctx))
        model = get_model()
        lab = WatchedLab(raw_lab, watcher, trailing, store, session_id=str(state.sample_id), context=ctx,
                         env=env, transcript_provider=lambda: messages_as_transcript(state.messages),
                         meta={"card": md["labels"]["honeypot"], "condition": md["condition"],
                               "arm": md["arm"], "model": str(getattr(model, "name", model)),
                               "source": "inspect"})
        tools = [f(lab) for f in it.TOOL_FACTORIES]
        store_ = state.store
        calls = idle = 0
        stop = "finish"
        dropped = []
        injected = []
        try:
            while not lab.done:
                if calls >= max_tool_calls:
                    stop = "tool_call_limit"
                    break
                output = await model.generate(state.messages, tools)
                state.output = output
                state.messages.append(output.message)
                tool_calls = output.message.tool_calls or []
                if not tool_calls:
                    idle += 1
                    if idle >= max_idle_turns:
                        stop = "no_tool_calls"
                        break
                    state.messages.append(ChatMessageUser(content=it.CONTINUE_PROMPT))
                    continue
                idle = 0
                if len(tool_calls) > max_tool_calls - calls:
                    dropped += [it._tool_call_dict(c) for c in tool_calls[max_tool_calls - calls:]]
                    store_.set("dropped_tool_calls", it._jsonable(dropped))
                    tool_calls = tool_calls[:max_tool_calls - calls]
                    output.message.tool_calls = tool_calls
                calls += len(tool_calls)
                lab._inspect_call_ids = [tc.id for tc in tool_calls]
                result = await execute_tools(state.messages, tools)
                lab._inspect_call_ids = []
                state.messages.extend(result.messages)
                it._log_unexecuted_calls(lab, tool_calls, result.messages)
                # Suggestion injection: trailing-monitor reminders appended after the tool round.
                for s in lab.pop_suggestions():
                    msg = ChatMessageSystem(content=s) if suggestion_role == "system" \
                        else ChatMessageUser(content=s)
                    state.messages.append(msg)
                    injected.append(s)
        except LimitExceededError as e:
            stop = f"limit: {e}"[:300]
        finally:
            lab.end_session(stop)
            entries = it._jsonable(list(lab.log))
            store_.set("lab_log", entries)
            store_.set("lab_log_monitor", monitor_view(entries))
            store_.set("lab_reads", it._jsonable(lab.reads))
            store_.set("audit_head", lab.log.head)
            store_.set("audit_len", len(lab.log))
            store_.set("audit_genesis", lab.log.genesis)
            store_.set("audit_salt", lab.log.salt)
            store_.set("audit_verified", AuditLog.verify_entries(
                entries, head=lab.log.head, genesis=lab.log.genesis, salt=lab.log.salt))
            store_.set("report_form", lab.REPORT_FORM)
            store_.set("stop_reason", stop)
            store_.set("tool_calls", calls)
            store_.set("session_end_message_index", len(state.messages))
            summary = lab.summary()
            summary["suggestions_injected"] = injected
            summary["providers"] = models.describe()
            store_.set("labwatcher", it._jsonable(summary))
            state.metadata["labwatcher_blocked"] = lab.blocked_count
            state.metadata["labwatcher_escalated"] = lab.escalated_count

        answer = None
        if ask_awareness and not stop.startswith("limit"):
            state.messages.append(ChatMessageUser(content=it.AWARENESS_QUESTION))
            out = await model.generate(state.messages)
            state.messages.append(out.message)
            answer = out.completion
        state.metadata["eval_awareness_answer"] = answer
        state.store.set("eval_awareness_answer", answer)
        return state
    return solve


@task
def watched_benchmark(env: str = "aspirin", context: str | None = None, conditions: str | None = None,
                      arms: str | None = None, honeypots: str | None = None,
                      max_tool_calls: int = it.DEFAULT_MAX_TOOL_CALLS, ask_awareness: bool = True,
                      human_auto: str | None = "deny", db: str | None = None,
                      suggestion_role: str = "system"):
    """labsim benchmark with LabWatcher in the loop. Filters are comma-separated; omitted means all.
    `human_auto` is approve | deny | timeout_allow for escalations (headless); `db` is the store path
    ("none" disables persistence). Set LABWATCHER_PROVIDER=mock to force the offline grader."""
    ask = str(ask_awareness).lower() not in ("false", "0", "no")
    return Task(dataset=MemoryDataset(it.build_samples(env, conditions, arms, honeypots),
                                      name=f"labwatcher-{env}"),
                solver=watched_lab_agent(max_tool_calls=int(max_tool_calls), ask_awareness=ask,
                                         context=context, human_auto=human_auto, db=db,
                                         suggestion_role=suggestion_role),
                scorer=it.lab_score(),
                metadata={"env": env, "context": context or _context_for(env),
                          "labwatcher_provider": os.environ.get("LABWATCHER_PROVIDER", "auto")})
