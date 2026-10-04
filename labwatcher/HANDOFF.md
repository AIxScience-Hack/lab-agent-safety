# LabWatcher handoff (2026-10-04, branch `watcher-trial`)

State at shutdown: **WIP commit with partial fixer changes.** All suites were green at the moment of
commit (`labwatcher/tests` 271 passed / 1 skipped; `drug_discovery` 277 passed; `check_tasks.py` 0 errors on
all four tasks), but the fixer agent was stopped mid-task, so some findings below are unverified.

## Workflow

- Dynamic workflow run ID: `wf_00fc43d9-dc5` (script
  `~/.claude/projects/-Users-tina-Downloads-lab-agent-safety-drug-discovery/e5816a49-7717-4ae0-b122-6f092d3f4342/workflows/scripts/labwatcher-build-wf_00fc43d9-dc5.js`).
  Phases: Build (7 builders, all done) → Integrate (done) → Verify (3 reviewers, done, 19 findings) → Fix
  (started, **interrupted**). Resume with `Workflow({scriptPath, resumeFromRunId: "wf_00fc43d9-dc5"})`: every
  stage except Fix replays from cache; Fix re-runs from scratch on the current tree.
- Journal with every agent's full return value: `.../subagents/workflows/wf_00fc43d9-dc5/journal.jsonl`.

## Verifier findings (19) and status

Status key: **done** = change present and covered by `labwatcher/tests/test_fixes.py`; **likely** = matching
code present in the uncommitted fixer diff, not independently verified; **open** = no change found.

| # | Sev | Where | Finding | Status |
|---|---|---|---|---|
| 1 | high | ui/app.py, demo.py, hooks.py | Live Approve/Deny never gated a real action (demo runs auto-approved headlessly); needs interactive mode with `on_escalate` registering pending escalations and blocking until `/api/escalations/{id}` | likely (on_escalate/pending plumbing added; test end to end) |
| 2 | high | hooks.py `_resolve_escalation` | Human verdict recorded in `human_decisions` but action row kept `decision=escalate` | likely |
| 3 | med | pipeline.py, trailing.py, demo.py | Policies/rules cached for process lifetime; Policy/Rules Panel edits had no effect until restart | likely (mtime-keyed reload) |
| 4 | med | pipeline.py `rules_for` | Stage 1 fails open on an unparsable rules file (zero rules, no error) | likely (`rules_degraded` / fail-closed path) |
| 5 | med | models.py | `*.max_tokens`, `*.timeout_s`, `models.<role>.temperature` settings never passed to providers | likely |
| 6 | low | pipeline.py `_human` | Unrecognised `human.auto` value treated as approve | likely (whitelist) |
| 7 | low | settings.py `ToolsView.normalise` | Tool entry with no mode and no thresholds silently becomes auto_approve | **open** |
| 8 | med | rules/materials_discovery.yaml | `md-cycler-vmax-over` / `md-oven-hot` bypassed when numeric arg sent as quoted string | likely (quote-tolerant regex) |
| 9 | med | rules/materials_discovery.yaml | NMP waste rules never match real item name `bowl_1`; lithium-to-wrong-container only triaged | likely |
| 10 | low | rules/materials_discovery.yaml | `md-furnace-hot-program` dead (wrong arg name, threshold above hard max) | likely |
| 11 | low | materials_discovery/envs/coin_cell.py `cycler_disable_safety_limits` | Creates placeholder channel with `cell=None`, bricking the channel | **open** |
| 12 | high | models.py `AnthropicProvider` | `temperature=` kwarg rejected by anthropic SDK 1.11 → anthropic provider always fails | check (diff touches `temperature=`; run a real call) |
| 13 | high | demo.py `run_demo` | No failure cleanup; crashed runs stay `running` forever | likely |
| 14 | med | store.py `summary()` | `by_category` SQL precedence bug leaks human-denied actions across contexts | likely |
| 15 | med | models.py `MockProvider._trailing` | Mock trailing scores the `[session_start]` header and LabWatcher's own injected reminders → honest demo sessions flagged | likely |
| 16 | med | ui/app.py write routes | Raw 500 on empty/non-JSON body instead of 400 | likely (`_json_object`) |
| 17 | low | ui/app.py `/api/demo/run` | Accepts empty `card` then fails in the job | likely |
| 18 | low | ui/static/index.html | Status filter lacks `stopped` (82 % of sessions) | likely |
| 19 | low | ui/static/app.js | "Flagged" legend disagrees with the store's flag rule | **open** |

After finishing: re-run `labwatcher/tests`, `drug_discovery` tests and `check_tasks.py`; then re-seed
(`python -m labwatcher.demo --seed`) because finding 15 inflated the shipped demo data.

## Remaining steps

1. **Finish the fixes** (table above): resume the workflow Fix phase or do it by hand; verify 1, 2, 12 with a
   real UI round-trip (start a demo run with provider `mock`, approve a pending escalation on `/live`).
2. **Replay the 31 Modal runs** into the store (they are real Qwen2.5-14B agent sessions recorded on Modal):
   ```bash
   .venv/bin/python -m labwatcher.demo --replay labwatcher/data/runs            # provider mock
   LABWATCHER_PROVIDER=modal .venv/bin/python -m labwatcher.demo --replay labwatcher/data/runs   # real graders
   ```
   Runs: aspirin a01/a05 (control+honeypot), a02/a03/a07/a10 honeypot; cytotox t01/t02/t03/t05; cell_culture
   c01/c02/c06; coin_cell m01–m08 (control+honeypot). Roughly a third ended with zero tool calls (the 14B model
   answered in prose); a stronger agent model on Modal would give richer transcripts.
3. **Launch the dashboard**: `./run_demo.sh` → http://127.0.0.1:8787 (Analyzer `/`, Live `/live`, Policy
   `/policy`, Rules `/rules`, Settings `/settings`). Context switcher toggles drug_discovery / materials_discovery.
4. **Modal**: app `labwatcher-models` was **stopped** at shutdown (it had `min_containers=1` on both GPUs, which
   costs money while idle). Redeploy with `.venv/bin/modal deploy labwatcher/modal/models.py`; first request
   cold-starts (~3–5 min); Modal returns HTTP 303 during cold start, clients must follow redirects.
   Endpoints: `https://sedm7377--labwatcher-models-{triage,evaluator}-serve.modal.run`.
5. **Devin**: six briefs in `devin/sessions/` are ready. Every `mcp__devin__*` call in this session failed with
   "no org_id could be resolved from your token … pass the target org via the X-Org-Id request header"; the MCP
   connection was established before the key change, so restart Claude Code (`--resume`) and launch the six
   sessions with `devin_session_create` (repo `AIxScience-Hack/lab-agent-safety`, base `watcher-trial`, tag
   `labwatcher`). The exact prompts I attempted are mirrored by the brief files.
6. **Amass**: `labwatcher/data/amass_cache.json` holds 24 cached queries (BiomedCore, DrugCore, PatentCore) used
   by the Stage 3 "domain precedent" input and the session page; `AMASS_LIVE=1` enables live calls.
