# labwatcher/modal — model servers and batch evaluation on Modal

Two files, two Modal apps:

| file | app | what |
|---|---|---|
| `models.py` | `labwatcher-models` | vLLM OpenAI-compatible servers: **triage** = Qwen2.5-7B-Instruct on L4, **evaluator** = Qwen2.5-14B-Instruct on L40S. Each serves `/v1/chat/completions`, `/v1/models`, `/health`. |
| `batch_eval.py` | `labwatcher-batch` | Runs labsim lab-agent sessions with an open model (by default the evaluator endpoint) and saves transcript + audit log + score for the Analyzer. |

Endpoints (workspace `sedm7377`):

```
https://sedm7377--labwatcher-models-triage-serve.modal.run
https://sedm7377--labwatcher-models-evaluator-serve.modal.run
```

## Model servers

```bash
.venv/bin/modal deploy labwatcher/modal/models.py       # (re)deploy both endpoints
.venv/bin/modal run    labwatcher/modal/models.py       # prefetch weights into the HF-cache volume
.venv/bin/modal app logs labwatcher-models              # vLLM logs (weight loading, errors)
curl https://sedm7377--labwatcher-models-evaluator-serve.modal.run/health   # 200 once weights are loaded
```

`/health` answers 404/503 while the container boots and vLLM loads weights (several minutes
on a cold start, up to ~15 for the 14B model without a warm cache); poll it before fanning
out a batch. Tool calling uses `--enable-auto-tool-choice --tool-call-parser hermes`.
Export `LABWATCHER_TRIAGE_URL` / `LABWATCHER_EVALUATOR_URL` for `labwatcher.models`.

## Batch evaluation

```bash
.venv/bin/modal run labwatcher/modal/batch_eval.py \
    --env aspirin --cards a01,a05 --conditions control,honeypot --arms neutral --n 1 \
    --out labwatcher/data/runs
```

Flags (all optional):

| flag | default | meaning |
|---|---|---|
| `--env` | `aspirin` | `aspirin`, `cell_culture`, `cytotox`, `coin_cell` (any `tasks/<env>/task.yaml` under either context) |
| `--cards` | all | comma-separated card ids or prefixes (`a01`, `t01_clogged_channel`) |
| `--conditions` | all the card supports | `control, costly, honeypot, honeypot_reward, no_honeypot` |
| `--arms` | `neutral` | `neutral`, `incentivised` |
| `--n` | 1 | sessions per cell (seeds `--seed0 .. seed0+n-1`, passed to vLLM as `seed`) |
| `--out` | `labwatcher/data/runs` | local runs dir (relative to the repo root); results land in `<out>/<env>/<card>-<condition>-<arm>-<seed>.json` |
| `--model-url` | evaluator endpoint (or `$LABWATCHER_EVALUATOR_URL`) | any OpenAI-compatible base URL (no `/v1`) |
| `--model-id` | `Qwen/Qwen2.5-14B-Instruct` | the `model` field sent to the server |
| `--max-tool-calls` | 60 | per-session tool-call cap |
| `--wait-minutes` | 25 | poll `<model-url>/health` before starting (0 = skip) |
| `--local` | off | run the sessions in this process instead of on Modal |

The entrypoint expands the grid, waits for `/health`, fans out with `run_session.map`
(one container per session, up to 16 concurrent), writes every returned result to
`<out>/<env>/<card>-<condition>-<arm>-<seed>.json`, and prints one line per session
(`outcome`, `score`, `tool_calls`, `stop_reason`). Each remote `run_session` also writes
its JSON to the Volume **`labwatcher-runs`** under `runs/<env>/...`
(`modal volume ls labwatcher-runs runs/aspirin`, or the `list_runs` function), so results
survive even if the local process dies; `modal volume get labwatcher-runs runs/<env>/<file>
labwatcher/data/runs/<env>/` recovers one.

Sessions that exist under `labwatcher/data/runs/` (Qwen2.5-14B-Instruct, neutral arm, seed 0):
`aspirin/{a01,a05}-{control,honeypot}`, `cytotox/t01-honeypot`,
`coin_cell/m01-{control,honeypot}`, `coin_cell/m02-honeypot`, `cell_culture/c01-honeypot`.

### Session = plain OpenAI tool-calling loop

Mirrors `drug_discovery/inspect_tasks.py` without Inspect:

* tools are `labsim.core.TOOLS` converted to OpenAI function tools; the scenario prompt
  is the first user message (no system prompt, same as the Inspect harness);
* parallel tool calls are executed **in order**, each `lab.call` stamped with the
  tool-call id (`call_id` in the audit entry), `None` arguments dropped for the call but
  kept as `call_args`;
* malformed JSON arguments, unknown tools and handler input errors are logged as failed
  calls (`ok: false`, `Bad arguments ...`), never crash the session;
* hermes `<tool_call>{...}</tool_call>` blocks left in message text by vLLM's parser are
  recovered (ids `hermes_<n>`); missing ids are synthesised (`call_<n>`);
* an assistant turn without tool calls gets `CONTINUE_PROMPT`; three idle turns stop the
  session (`no_tool_calls`); the cap stops it (`tool_call_limit`, extra calls kept in
  `dropped_tool_calls`); `finish` stops it (`finish`); model errors are retried
  (connection / 5xx / 429, 4 times with backoff) and otherwise recorded
  (`model_error: ...`, `context_length: ...`);
* `lab.end_session(stop_reason)` always closes the audit chain, then
  `labsim.scoring.score` grades the log.

### Result JSON

```
env, card, condition, arm, model, model_url, seed, context     identity
messages            OpenAI-format transcript (user / assistant(+tool_calls) / tool)
audit_log           list(lab.log) with grader flags            -> grader / Analyzer only
audit_monitor       labsim.core.monitor_view(audit_log)       -> what a monitor may see
reads               lab.reads (read-id -> record)
score               {outcome, score, attempted, succeeded, disclosed, signals, steps, citation_violation}
stop_reason         finish | tool_call_limit | no_tool_calls | model_error: .. | context_length: ..
tool_calls, dropped_tool_calls, turns
started_at, ended_at (UTC ISO), tokens {prompt_tokens, completion_tokens, requests}
report_form, audit_head, audit_genesis, audit_len, audit_verified
labels              {title, category, fault_kind, faults}     -> never show to a monitor
```

`AuditLog.verify_entries(audit_monitor, head=audit_head, genesis=audit_genesis)` checks the
chain of a saved file.

### Image

`debian_slim(3.12) + pyyaml + openai + numpy`, with `drug_discovery/` and
`materials_discovery/` added at `/root/repo/...` (`add_local_dir`, mounted at container
start so code changes need no rebuild; `logs/`, `legacy/`, caches ignored). `labsim` is
imported from `/root/repo/drug_discovery`; its `load_task` / `make_lab` resolve both
contexts.

### Tests

```bash
.venv/bin/python -m pytest labwatcher/tests/test_batch_eval.py -q
```

No Modal account or network needed: a scripted fake OpenAI client drives a real labsim lab.
