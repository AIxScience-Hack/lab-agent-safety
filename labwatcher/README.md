# labwatcher — Apollo Watcher for lab-automation agents

Read `SPEC.md` (the contract every module follows) and `WATCHER_REFERENCE.md` (what the
original Watcher does) first. This file is the operator's guide.

## Modules

| file | role |
|---|---|
| `pipeline.py` | `Action`, `Decision`, `Watcher.grade_action` (Stage 1 -> 2 -> 3 -> thresholds), `load_policy` |
| `rules.py` | Stage 1 `RuleEngine` (`load`, `evaluate`, `add`, `update`, `delete`, `save`, `validate_rule`), `TAXONOMY` |
| `rules/<context>.yaml` | built-in rules: `id, match:{tool, command, path, args}, decision, priority, reason, category` |
| `models.py` | `ModelRouter(settings).complete_json(role, system, user)`; providers `ModalVLLM`, `AnthropicProvider`, `MockProvider` |
| `trailing.py` | `TrailingMonitor.maybe_review / review / build_suggestion`, `wrap_suggestion` |
| `policies/<context>.yaml` | `triage_system`, `evaluator_system`, `trailing_system` prompts (`{taxonomy}` substituted) |
| `settings.py` / `settings.yaml` | `Settings.load(org_path, user_path)` layered defaults -> org -> user with `modifiable / locked / additions_allowed` permissions |
| `store.py` | `Store` over SQLite WAL: `create_session, end_session, add_action, add_transcript, add_trailing, add_human_decision, add_enrichment, summary, sessions, session, pending_escalations, resolve_escalation` |
| `hooks.py` | `WatchedLab(lab, watcher, trailing, store, ...)`: proxy around a labsim `Lab`; `pending_suggestions` for the harness |
| `enrich/amass.py` | `AmassClient`, `AmassCache`, `enrich_session(context, env, card_title, keywords)` |
| `demo.py` | `run_demo(context, env, card_id, script, provider="mock", store=None, arm="incentivised", condition=None, human_auto=None, on_escalate=None) -> session_id` runs one oracle script through `WatchedLab` (`human_auto="live"` + `on_escalate(action, decision) -> verdict | (verdict, note)` makes escalations wait for a reviewer -- the Live UI; a run that fails part-way is closed with status `failed`) (honest/exploit default to the honeypot condition, `full` to control), stores the transcript, scores with `labsim.scoring.score` and attaches cached Amass enrichment. `python -m labwatcher.demo --seed` = honest + exploit for all 38 cards (76 sessions, ~7 s mock); `--replay labwatcher/data/runs` grades recorded Modal batch runs; `--provider modal` uses the real endpoints. An empty store is seeded the same way when the UI starts (`LABWATCHER_SEED=quick|fixture|0` for one card per env / the synthetic `ui/fixtures.py` rows / nothing) |
| `inspect_watched.py` | Inspect task `watched_benchmark` (the drug_discovery benchmark with every call graded) |
| `modal/models.py` | Modal app `labwatcher-models`: vLLM triage (Qwen2.5-7B, L4) and evaluator (Qwen2.5-14B, L40S) |
| `modal/batch_eval.py` | Modal app `labwatcher-batch`: run labsim sessions with an open model, save JSON results |
| `ui/app.py`, `ui/static/` | FastAPI `create_app()` / `app`; Analyzer, Live, session, policy, rules, settings pages |

## Decision flow in one screen

```
Action ──► RuleEngine.evaluate ──► deny / allow / escalate_human  (stage="rules", score=None)
              │ escalate_triage or no rule
              ▼
          triage complete_json ──► confidence >= 0.8 and score < escalate_at ──► allow (stage="triage")
              │ otherwise
              ▼
          evaluator complete_json (40 msgs, triage verdict, last 5 human decisions, Amass precedent)
              │
              ▼
          tool threshold:  score >= deny_at ──► deny (stage="threshold"), text "BLOCKED by LabWatcher: ..."
                           score >= escalate_at ──► escalate (stage="human"): Live UI or human.auto
                           else allow (stage="evaluator")
```

Every `every_n_actions` (5) calls `TrailingMonitor.maybe_review` grades the last 20 transcript
messages + audit entries on the nine categories; a category >= 7 produces
`<system-reminder>LabWatcher: ...</system-reminder>` which `WatchedLab` queues in
`pending_suggestions` and the harness appends to the agent's messages.

## Run it

```bash
# UI with the offline mock grader (seeds demo sessions on first start)
../run_demo.sh                                   # or: .venv/bin/python -m uvicorn labwatcher.ui.app:app --port 8787

# real graders on Modal
.venv/bin/modal deploy labwatcher/modal/models.py
export LABWATCHER_TRIAGE_URL=https://<ws>--labwatcher-models-triage-serve.modal.run
export LABWATCHER_EVALUATOR_URL=https://<ws>--labwatcher-models-evaluator-serve.modal.run
LABWATCHER_PROVIDER=modal ../run_demo.sh

# grade one scripted oracle session from Python
.venv/bin/python -c "from labwatcher.demo import run_demo; print(run_demo('drug_discovery','aspirin','a01','exploit'))"
```

Provider resolution per role: `LABWATCHER_PROVIDER` if set; otherwise `settings.models.<role>.fallback`
(`modal` only when `<base_url>/health` answers, `anthropic` only with `ANTHROPIC_API_KEY`, `mock` always).

## Settings layers

`Settings.load()` reads `settings.yaml` (built-in), then `$LABWATCHER_ORG_SETTINGS`, then
`$LABWATCHER_USER_SETTINGS`. `permissions:` in a higher layer governs what lower layers may
change; violations are recorded as warnings and shown on `/settings`, never silently applied.
`version` and `taxonomy` are locked; `contexts` is `additions_allowed`; everything else is
`modifiable` by default.

## HTTP API (no auth)

`GET /api/health`, `/api/taxonomy`, `/api/catalog`, `/api/summary?context=`,
`/api/sessions?context=&min_score=&status=&since=`, `/api/sessions/{sid}`,
`/api/live/events` (SSE), `/api/escalations`, `POST /api/escalations/{action_id}` `{decision, note}`,
`POST /api/demo/run` `{context, env, card, script, provider, human: live | approve | deny | timeout_allow}` (`live` pauses the run on each escalation until `POST /api/escalations/{id}` resolves it), `/api/demo/jobs`,
`GET|POST /api/policy/{context}`, `GET|POST /api/rules/{context}`,
`PUT|DELETE /api/rules/{context}/{rule_id}`, `GET /api/rules/{context}/test?tool=&command=&path=`,
`GET /api/settings`.

## Tests

```bash
.venv/bin/python -m pytest labwatcher/tests -q
```

All tests run offline with `MockProvider`; `test_batch_eval.py` drives a real labsim lab with a
scripted fake OpenAI client, `test_amass.py` uses recorded responses.
