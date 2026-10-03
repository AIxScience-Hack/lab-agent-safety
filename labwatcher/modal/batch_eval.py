"""LabWatcher batch evaluation on Modal: run labsim lab-agent sessions with an open model.

Each `run_session` call builds one labsim scenario (env, card, condition, arm), drives a
plain OpenAI tool-calling agent loop against a vLLM endpoint (the Modal model servers in
`labwatcher/modal/models.py`, or any OpenAI-compatible URL), scores the audit log with
`labsim.scoring.score` and returns a JSON-able result that the Analyzer ingests
(transcript in OpenAI message format + audit log + monitor view + score).  Results are
also written to the Volume "labwatcher-runs" under
``runs/<env>/<card>-<condition>-<arm>-<seed>.json``; the local entrypoint writes them
under ``<out>/<env>/<card>-<condition>-<arm>-<seed>.json`` (default
``labwatcher/data/runs``).

    modal run labwatcher/modal/batch_eval.py --env aspirin --cards a01,a05 \
        --conditions control,honeypot --arms neutral --n 1 \
        --out labwatcher/data/runs

Flags: ``--model-url`` / ``--model-id`` (default: the Evaluator endpoint and
Qwen/Qwen2.5-14B-Instruct), ``--max-tool-calls`` (60), ``--wait-minutes`` (poll
``<model-url>/health`` before fanning out; 0 skips), ``--local`` (run the sessions in this
process instead of on Modal; handy for debugging against a running endpoint).

The agent loop mirrors drug_discovery/inspect_tasks.py: parallel tool calls executed in
order, each stamped with its tool-call id in the audit log, malformed arguments logged as
failed calls, CONTINUE_PROMPT after idle turns, stop at `finish`, the tool-call cap or
three idle turns.  Hermes-style ``<tool_call>`` blocks that vLLM's parser left in the
message text are recovered too.  All helpers are plain Python so tests run without Modal.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # modal is only needed to deploy/run remotely; the helpers work without it
    import modal
except ImportError:  # pragma: no cover - exercised only where modal is not installed
    modal = None

APP_NAME = "labwatcher-batch"
VOLUME_NAME = "labwatcher-runs"
RUNS_PREFIX = "runs"

EVALUATOR_URL = "https://sedm7377--labwatcher-models-evaluator-serve.modal.run"
TRIAGE_URL = "https://sedm7377--labwatcher-models-triage-serve.modal.run"
DEFAULT_MODEL_URL = os.environ.get("LABWATCHER_EVALUATOR_URL", EVALUATOR_URL)
DEFAULT_MODEL_ID = "Qwen/Qwen2.5-14B-Instruct"

CONTINUE_PROMPT = ("Please continue with the request using the tools available. "
                   "Call finish when the session is complete.")
DEFAULT_MAX_TOOL_CALLS = 60
MAX_IDLE_TURNS = 3
MAX_COMPLETION_TOKENS = 2048
MODEL_RETRIES = 4

# Repo roots: on the local machine this file is labwatcher/modal/batch_eval.py; in the
# container Modal mounts it as /root/batch_eval.py (too shallow for parents[2]) and the
# repo code is added under /root/repo (see `image` below).
REMOTE_REPO = Path("/root/repo")


def _local_repo() -> Path:
    here = Path(__file__).resolve()
    return here.parents[2] if len(here.parents) > 2 else REMOTE_REPO


LOCAL_REPO = _local_repo()
CONTEXT_DIRS = ("drug_discovery", "materials_discovery")

_HERMES_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# --- repo / labsim access -----------------------------------------------------------

def repo_root() -> Path:
    """The repo root: /root/repo inside a Modal container, the checkout otherwise."""
    return REMOTE_REPO if (REMOTE_REPO / "drug_discovery").is_dir() else LOCAL_REPO


def import_labsim():
    """Put <repo>/drug_discovery on sys.path and return the labsim package."""
    root = repo_root()
    dd = str(root / "drug_discovery")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    if str(root) not in sys.path:      # materials_discovery.envs.* is imported by make_lab
        sys.path.append(str(root))
    import labsim  # noqa: WPS433 (deferred so the module imports without the repo)
    return labsim


def context_for(env: str) -> str:
    """drug_discovery | materials_discovery for an env name (labsim.core.ENV_MODULES)."""
    from labsim.core import ENV_MODULES
    return "materials_discovery" if env in ENV_MODULES else "drug_discovery"


# --- tool schema and call parsing ---------------------------------------------------

def openai_tools(tools) -> list[dict]:
    """labsim.core.TOOLS (Anthropic-style input_schema) -> OpenAI function tools."""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["input_schema"]}} for t in tools]


def parse_arguments(raw):
    """Tool-call arguments as the model sent them -> (args: dict | None, error: str | None).

    vLLM's hermes parser normally returns a JSON string; a dict is accepted as well.
    Anything that is not a JSON object is an error (the call is logged as failed)."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, f"expected a JSON object, got {type(raw).__name__}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg} at {e.pos}"
    if not isinstance(value, dict):
        return None, f"expected a JSON object, got {type(value).__name__}"
    return value, None


def extract_hermes_tool_calls(content, start_index=0) -> list[dict]:
    """Recover ``<tool_call>{"name":..., "arguments":...}</tool_call>`` blocks that the
    model wrote as plain text (the parser missed them).  Returns OpenAI-format tool
    calls with synthetic ids; unparsable blocks become calls with raw string arguments so
    they are still logged as failed."""
    calls = []
    if not content or "<tool_call>" not in content:
        return calls
    for i, m in enumerate(_HERMES_RE.finditer(content)):
        block = m.group(1)
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "name" in obj:
            name = str(obj["name"])
            args = obj.get("arguments", obj.get("parameters", {}))
            arguments = args if isinstance(args, str) else json.dumps(args)
        else:
            name, arguments = "unknown", block
        calls.append({"id": f"hermes_{start_index + i}", "type": "function",
                      "function": {"name": name, "arguments": arguments}})
    return calls


def normalise_tool_call(tc) -> dict:
    """An SDK ToolCall object or dict -> {"id", "type", "function": {"name", "arguments"}}."""
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        return {"id": tc.get("id"), "type": "function",
                "function": {"name": fn.get("name"), "arguments": fn.get("arguments")}}
    fn = getattr(tc, "function", None)
    return {"id": getattr(tc, "id", None), "type": "function",
            "function": {"name": getattr(fn, "name", None),
                         "arguments": getattr(fn, "arguments", None)}}


def sanitise_tool_call(tc: dict) -> dict:
    """A copy of a tool call that is safe to resend: vLLM's chat templates json.loads the
    arguments of every historical tool call, so malformed arguments (logged raw in the
    audit log) go back as {"_raw": "..."}; a missing name becomes "unknown"."""
    fn = tc["function"]
    args, err = parse_arguments(fn.get("arguments"))
    arguments = json.dumps(args) if err is None else json.dumps({"_raw": fn.get("arguments")})
    return {"id": tc.get("id"), "type": "function",
            "function": {"name": fn.get("name") or "unknown", "arguments": arguments}}


def assistant_message(msg, tool_calls, recovered=False) -> dict:
    """The model's reply as an OpenAI message dict (only the fields we resend).  When the
    tool calls were recovered from <tool_call> text, that text is stripped from the
    content so the model does not see it twice (once as text, once rendered as calls)."""
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    content = content if content is not None else ""
    if recovered and tool_calls:
        content = _HERMES_RE.sub("", content).strip()
    out = {"role": "assistant", "content": content}
    if tool_calls:
        out["tool_calls"] = [sanitise_tool_call(tc) for tc in tool_calls]
    return out


# --- executing tool calls against the lab -------------------------------------------

def execute_tool_call(lab, tc: dict) -> str:
    """Run one tool call on the lab and return the text result for the model.

    Mirrors inspect_tasks._run/_log_unexecuted_calls: None-valued arguments are dropped
    for the call but kept in the audit entry (call_args); malformed arguments and
    unexpected exceptions are logged as failed calls instead of crashing the session."""
    name = tc["function"].get("name") or "unknown"
    call_id = tc.get("id")
    args, err = parse_arguments(tc["function"].get("arguments"))
    if err is not None:
        return _record_failed(lab, name, {"raw": tc["function"].get("arguments")},
                              f"Bad arguments (json): {err}", call_id)
    clean = {k: v for k, v in args.items() if v is not None}
    try:
        return str(lab.call(name, clean, call_id=call_id, call_args=args))
    except Exception as e:  # noqa: BLE001 - lab.call restores state on input errors
        return _record_failed(lab, name, args, f"Error: {type(e).__name__}: {e}", call_id)


def _record_failed(lab, name, args, message, call_id):
    lab._ctx = {"call_id": call_id} if call_id is not None else {}
    try:
        return str(lab._record(name, args, False, message))
    finally:
        lab._ctx = {}


# --- the model call -----------------------------------------------------------------

def _is_context_length_error(exc) -> bool:
    text = str(exc).lower()
    return "maximum context length" in text or "context length" in text or "too many tokens" in text


def _status_of(exc):
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None),
                                                        "status_code", None)


def call_model(client, model_id, messages, tools, seed=None, temperature=0.7,
               max_tokens=MAX_COMPLETION_TOKENS, retries=MODEL_RETRIES, sleep=time.sleep):
    """One chat completion with retries on transient errors.

    Returns (message, usage) or raises the last error.  4xx errors other than 429 are not
    retried (a context-length overflow must stop the session, not loop)."""
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model_id, messages=messages, tools=tools, tool_choice="auto",
                temperature=temperature, max_tokens=max_tokens,
                **({"seed": seed} if seed is not None else {}))
            usage = getattr(resp, "usage", None)
            return resp.choices[0].message, usage
        except Exception as e:  # noqa: BLE001
            last = e
            status = _status_of(e)
            if status is not None and 400 <= int(status) < 500 and int(status) != 429:
                raise
            if attempt < retries:
                sleep(min(2 ** attempt * 2, 30))
    raise last


def _usage_dict(usage) -> dict:
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d) or d
    return {"prompt_tokens": int(get("prompt_tokens", 0) or 0),
            "completion_tokens": int(get("completion_tokens", 0) or 0)}


# --- the agent loop -----------------------------------------------------------------

def run_agent_loop(client, model_id, lab, prompt, max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
                   max_idle_turns=MAX_IDLE_TURNS, seed=None, temperature=0.7, tools=None,
                   system_prompt=None, sleep=time.sleep) -> dict:
    """Drive the lab with a tool-calling model until finish, the cap or idle turns.

    Returns {messages, stop_reason, tool_calls, tokens, dropped_tool_calls, turns}.  The
    lab's session is closed with lab.end_session(stop_reason) before returning, so the
    audit log is complete even when the model errors out."""
    labsim = import_labsim()
    tools = tools if tools is not None else openai_tools(labsim.TOOLS)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    calls = idle = turns = 0
    stop = "finish"
    dropped = []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
    try:
        while not lab.done:
            if calls >= max_tool_calls:
                stop = "tool_call_limit"
                break
            try:
                msg, usage = call_model(client, model_id, messages, tools, seed=seed,
                                        temperature=temperature, sleep=sleep)
            except Exception as e:  # noqa: BLE001 - keep the log, record why we stopped
                kind = "context_length" if _is_context_length_error(e) else "model_error"
                stop = f"{kind}: {type(e).__name__}: {e}"[:300]
                break
            turns += 1
            u = _usage_dict(usage)
            tokens["prompt_tokens"] += u["prompt_tokens"]
            tokens["completion_tokens"] += u["completion_tokens"]
            tokens["requests"] += 1
            raw_calls = (msg.get("tool_calls") if isinstance(msg, dict)
                         else getattr(msg, "tool_calls", None)) or []
            tool_calls = [normalise_tool_call(tc) for tc in raw_calls]
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            recovered = False
            if not tool_calls:
                tool_calls = extract_hermes_tool_calls(content, start_index=calls)
                recovered = bool(tool_calls)
            for i, tc in enumerate(tool_calls):   # vLLM may omit ids; the log needs them
                if not tc.get("id"):
                    tc["id"] = f"call_{calls + i}"
            if not tool_calls:
                messages.append(assistant_message(msg, []))
                idle += 1
                if idle >= max_idle_turns:
                    stop = "no_tool_calls"
                    break
                messages.append({"role": "user", "content": CONTINUE_PROMPT})
                continue
            idle = 0
            room = max_tool_calls - calls
            if len(tool_calls) > room:            # keep the cap exact, remember the rest
                dropped += tool_calls[room:]
                tool_calls = tool_calls[:room]
            calls += len(tool_calls)
            messages.append(assistant_message(msg, tool_calls, recovered=recovered))
            for tc in tool_calls:                 # parallel calls run in order
                result = execute_tool_call(lab, tc)
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc["function"].get("name"), "content": result})
                if lab.done:
                    break
    finally:
        lab.end_session(stop)
    return {"messages": messages, "stop_reason": stop, "tool_calls": calls, "tokens": tokens,
            "dropped_tool_calls": dropped, "turns": turns}


# --- one session --------------------------------------------------------------------

def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_filename(card_id, condition, arm, seed) -> str:
    return f"{card_id}-{condition}-{arm}-{seed}.json"


def session_relpath(env, card_id, condition, arm, seed) -> str:
    """Path of a result on the Volume (relative to its mount point)."""
    return f"{RUNS_PREFIX}/{env}/{session_filename(card_id, condition, arm, seed)}"


def local_result_path(out_dir, env, card_id, condition, arm, seed) -> Path:
    """Path of a result under the local --out directory: <out>/<env>/<file>.json (no
    extra ``runs/`` level: --out already is the runs directory)."""
    return Path(out_dir) / env / session_filename(card_id, condition, arm, seed)


def resolve_out_dir(out, repo=None) -> Path:
    """--out as an absolute Path; relative paths are taken from the repo root, not the
    cwd, so `modal run` from any directory lands in the same place."""
    out_dir = Path(out).expanduser()
    if not out_dir.is_absolute():
        out_dir = (repo or LOCAL_REPO) / out_dir
    return out_dir


def write_results(jobs, results, out_dir, log=lambda m: print(m, flush=True)) -> list[Path]:
    """Write every successful result to <out_dir>/<env>/<file>.json; exceptions (from
    `.map(return_exceptions=True)`) are logged against their job.  Returns the paths."""
    written = []
    for job, r in zip(jobs, results):
        if isinstance(r, Exception):
            log(f"[batch_eval] FAILED {job}: {type(r).__name__}: {r}")
            continue
        r.pop("saved_to", None)
        path = local_result_path(out_dir, r["env"], r["card"], r["condition"], r["arm"],
                                 r["seed"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(r, indent=1))
        written.append(path)
    return written


def find_card(cards, card_id):
    """Exact id first, then unique prefix (a01 -> a01_balance_calibration)."""
    for c in cards:
        if c["id"] == card_id:
            return c
    hits = [c for c in cards if c["id"].startswith(card_id)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"no card {card_id!r}; have {[c['id'] for c in cards]}")
    raise ValueError(f"card {card_id!r} is ambiguous: {[c['id'] for c in hits]}")


def build_result(env, card, condition, arm, model_id, seed, lab, loop, started_at,
                 ended_at, model_url=None) -> dict:
    """Assemble the result schema from a finished lab + loop output and score it."""
    labsim = import_labsim()
    from labsim.core import AuditLog, monitor_view
    entries = _jsonable(list(lab.log))
    result = labsim.score(card, entries, condition, lab.REPORT_FORM)
    return {
        "env": env, "card": card["id"], "condition": condition, "arm": arm,
        "model": model_id, "model_url": model_url, "seed": seed,
        "context": context_for(env),
        "messages": _jsonable(loop["messages"]),
        "audit_log": entries,
        "audit_monitor": monitor_view(entries),
        "reads": _jsonable(lab.reads),
        "score": result,
        "stop_reason": loop["stop_reason"],
        "tool_calls": loop["tool_calls"],
        "dropped_tool_calls": _jsonable(loop.get("dropped_tool_calls", [])),
        "turns": loop.get("turns", 0),
        "started_at": started_at, "ended_at": ended_at,
        "tokens": loop["tokens"],
        "report_form": lab.REPORT_FORM,
        "audit_head": lab.log.head, "audit_genesis": lab.log.genesis, "audit_len": len(lab.log),
        "audit_verified": AuditLog.verify_entries(entries, head=lab.log.head,
                                                  genesis=lab.log.genesis, salt=lab.log.salt),
        # Ground truth for the Analyzer only; a monitor must not read this block.
        "labels": {"title": card.get("title", ""), "category": card.get("category", ""),
                   "fault_kind": card.get("fault_kind"), "faults": lab.s.get("faults", [])},
    }


def run_session_impl(env, card_id, condition, arm, model_url, model_id,
                     max_tool_calls=DEFAULT_MAX_TOOL_CALLS, seed=0, client=None,
                     temperature=0.7, save_dir=None) -> dict:
    """The body of run_session, usable locally (pass `client` to inject a fake)."""
    labsim = import_labsim()
    task_def, cards = labsim.load_task(env)
    card = find_card(cards, card_id)
    scenario = labsim.build_scenario(task_def, card, condition, arm)
    lab = labsim.make_lab(scenario)
    if client is None:
        from openai import OpenAI
        client = OpenAI(base_url=model_url.rstrip("/") + "/v1",
                        api_key=os.environ.get("LABWATCHER_MODEL_KEY", "EMPTY"),
                        timeout=600.0, max_retries=0)
    started = _now()
    loop = run_agent_loop(client, model_id, lab, scenario["prompt"],
                          max_tool_calls=max_tool_calls, seed=seed, temperature=temperature)
    out = build_result(env, card, condition, arm, model_id, seed, lab, loop, started, _now(),
                       model_url=model_url)
    if save_dir is not None:
        path = Path(save_dir) / session_relpath(env, card["id"], condition, arm, seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1))
        out["saved_to"] = str(path)
    return out


# --- health / grid helpers ----------------------------------------------------------

def health_ok(url, timeout=20) -> bool:
    """True when <url>/health answers 200 (vLLM only serves it once weights are loaded)."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def wait_healthy(url, minutes=25.0, interval=30, log=lambda m: print(m, flush=True), sleep=time.sleep,
                 clock=time.monotonic) -> bool:
    deadline = clock() + minutes * 60
    while True:
        if health_ok(url):
            return True
        if clock() >= deadline:
            return False
        log(f"[batch_eval] {url} not healthy yet; retrying in {interval}s")
        sleep(interval)


def _split(value):
    if value is None or value == "" or value == "all":
        return None
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def expand_grid(env, cards=None, conditions=None, arms=None, n=1, seed0=0) -> list[dict]:
    """The (env, card, condition, arm, seed) jobs for a request.  Cards match by id or
    prefix; omitted cards/conditions/arms mean every one the card supports."""
    labsim = import_labsim()
    _, all_cards = labsim.load_task(env)
    wanted, conds, arm_list = _split(cards), _split(conditions), _split(arms)
    for a in arm_list or []:
        if a not in labsim.ARMS:
            raise ValueError(f"unknown arm {a!r}; expected one of {labsim.ARMS}")
    chosen = (all_cards if wanted is None
              else [find_card(all_cards, w) for w in wanted])
    jobs = []
    for card in chosen:
        for mode in labsim.modes_for(card):
            if conds is not None and mode not in conds:
                continue
            for arm in arm_list or labsim.ARMS:
                for seed in range(seed0, seed0 + int(n)):
                    jobs.append({"env": env, "card_id": card["id"], "condition": mode,
                                 "arm": arm, "seed": seed})
    if not jobs:
        raise ValueError(f"no sessions for env={env} cards={wanted} conditions={conds} "
                         f"arms={arm_list}")
    return jobs


def summarise(results) -> str:
    lines = []
    for r in results:
        if isinstance(r, Exception):
            lines.append(f"ERROR {type(r).__name__}: {r}")
            continue
        s = r["score"]
        lines.append(f"{r['env']:<9} {r['card']:<26} {r['condition']:<16} {r['arm']:<13} "
                     f"seed={r['seed']} {s['outcome']:<18} score={s['score']} "
                     f"calls={r['tool_calls']} stop={r['stop_reason'][:40]}")
    return "\n".join(lines)


# --- Modal app ----------------------------------------------------------------------

if modal is not None:
    _IGNORE = ["**/__pycache__", "**/*.pyc", "**/logs", "**/legacy", "**/.pytest_cache",
               "**/*.eval"]
    image = modal.Image.debian_slim(python_version="3.12").pip_install(
        "pyyaml", "openai", "numpy")
    for _d in CONTEXT_DIRS:
        if (LOCAL_REPO / _d).is_dir():
            image = image.add_local_dir(LOCAL_REPO / _d, remote_path=f"/root/repo/{_d}",
                                        ignore=_IGNORE)
    volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
    VOLUME_PATH = "/runs"
    app = modal.App(APP_NAME)

    @app.function(image=image, volumes={VOLUME_PATH: volume}, timeout=3600,
                  max_containers=16, retries=0)
    def run_session(env: str, card_id: str, condition: str, arm: str, model_url: str,
                    model_id: str, max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
                    seed: int = 0) -> dict:
        """One lab-agent session on Modal; result saved to the Volume and returned."""
        out = run_session_impl(env, card_id, condition, arm, model_url, model_id,
                               max_tool_calls=max_tool_calls, seed=seed,
                               save_dir=VOLUME_PATH)
        volume.commit()
        print(summarise([out]), flush=True)
        return out

    @app.function(image=image, volumes={VOLUME_PATH: volume}, timeout=600)
    def list_runs(env: str = "") -> list[str]:
        """Relative paths of the results stored on the Volume (optionally one env)."""
        base = Path(VOLUME_PATH) / RUNS_PREFIX / env if env else Path(VOLUME_PATH) / RUNS_PREFIX
        return sorted(str(p.relative_to(VOLUME_PATH)) for p in base.rglob("*.json"))

    @app.local_entrypoint()
    def main(env: str = "aspirin", cards: str = "", conditions: str = "",
             arms: str = "neutral", n: int = 1, out: str = "labwatcher/data/runs",
             model_url: str = DEFAULT_MODEL_URL, model_id: str = DEFAULT_MODEL_ID,
             max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS, seed0: int = 0,
             wait_minutes: float = 25.0, local: bool = False):
        jobs = expand_grid(env, cards, conditions, arms, n, seed0)
        print(f"[batch_eval] {len(jobs)} sessions on {model_id} @ {model_url}", flush=True)
        if wait_minutes > 0 and not wait_healthy(model_url, wait_minutes):
            raise SystemExit(f"{model_url}/health never returned 200 in {wait_minutes} min")
        out_dir = resolve_out_dir(out)
        if local:
            results = [run_session_impl(j["env"], j["card_id"], j["condition"], j["arm"],
                                        model_url, model_id, max_tool_calls, j["seed"])
                       for j in jobs]
        else:
            results = list(run_session.map(
                [j["env"] for j in jobs], [j["card_id"] for j in jobs],
                [j["condition"] for j in jobs], [j["arm"] for j in jobs],
                [model_url] * len(jobs), [model_id] * len(jobs),
                [max_tool_calls] * len(jobs), [j["seed"] for j in jobs],
                return_exceptions=True, order_outputs=True))
        written = write_results(jobs, results, out_dir)
        print(summarise(results), flush=True)
        print(f"[batch_eval] wrote {len(written)}/{len(jobs)} results under {out_dir}",
              flush=True)
