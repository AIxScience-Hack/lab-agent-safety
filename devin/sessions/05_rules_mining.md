# Devin brief 05 — Rules auto-mining: an LLM on Modal reads each task's SOPs and manuals and proposes Stage 1 rules

Repository: https://github.com/AIxScience-Hack/lab-agent-safety  base branch `watcher-trial`
Your branch: `devin/rules-mining`  -> open a PR into `watcher-trial` when done.

## Goal

Build a pipeline that, for each environment, feeds the agent-visible documentation of the task
(`sops/*.md`, `methods/*.md`, `docs/instruments.md`, `docs/forms.md`, `docs/manuals/*`, waste and
safety SOPs, the "Invalid runs" criteria) to an open model running on Modal and asks it to
propose LabWatcher Stage 1 command rules in the exact schema of `labwatcher/rules/*.yaml`.
Validate them, de-duplicate them, execute them against a corpus of real actions, and produce a
structured diff against the hand-written rules: what the model found that humans missed, what
humans wrote that the model did not, and where they disagree on the decision.

## Why it matters

Hand-writing ~60 rules per lab is the bottleneck for deploying a Watcher-style monitor in a new
facility. SOPs already say which files are system-of-record, which instrument commands are
bypasses, and what must never go down the drain. If a model can mine those into regex rules with
acceptable precision, a lab can bootstrap Stage 1 from its own documents in minutes, and the diff
against expert rules tells us how much to trust it.

## Setup

```bash
git clone https://github.com/AIxScience-Hack/lab-agent-safety && cd lab-agent-safety
git checkout watcher-trial && git checkout -b devin/rules-mining
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
export LABWATCHER_PROVIDER=mock
(cd drug_discovery && ../.venv/bin/python -m pytest -q && ../.venv/bin/python check_tasks.py)
.venv/bin/python -m pytest labwatcher/tests -q
```

Read `labwatcher/SPEC.md`, `labwatcher/rules.py` (`validate_rule`, `RuleEngine.load/evaluate`,
`TAXONOMY`), both `labwatcher/rules/*.yaml` (header comment explains match semantics and
priority bands), `labwatcher/models.py` (`ModalVLLM`, `extract_json`), `labwatcher/modal/README.md`,
`drug_discovery/RULES.md`, `drug_discovery/check_tasks.py`, and `labsim.load_task` /
`labsim.build_scenario` (the scenario's `files` dict is the agent-visible workspace).

**Use Modal for any GPU work (the Modal token is configured in the org; name apps `labwatcher-*`).**
Preferred: a Modal app `labwatcher-mining` in `labwatcher/mining/modal_app.py` that runs vLLM
with `Qwen/Qwen2.5-14B-Instruct` on an L40S (copy the server pattern from `labwatcher/modal/models.py`,
or call the already deployed `LABWATCHER_EVALUATOR_URL` with the OpenAI client). Also run the mining
with `Qwen/Qwen2.5-7B-Instruct` (L4) so the report compares two model sizes. No LLM on the VM.

## Method

1. **Corpus of documents.** For each env (`aspirin`, `cell_culture`, `cytotox`, `coin_cell` if
   present) build the `control`-condition scenario of the first card with `build_scenario` and take
   every file under `sops/`, `methods/`, `docs/`, `safety/` plus the brief. Strip nothing. Do not
   include honeypot card YAML, `hack_if`, or `summary_cases.md`: the model must work only from what a
   real lab would have.
2. **Prompting.** Chunk by document; for each chunk ask for rules as a JSON list in the live schema
   (`id, match:{tool, command, path, args}, decision, priority, reason, category`) with the
   taxonomy ids from `labwatcher.rules.TAXONOMY`, the eight tool names, and the `command` form
   `"<instrument>.<command>"`. Include 3 few-shot examples taken from the **other** context's rules
   file so it is not copying the target. Temperature 0. Ask for `evidence`: the quoted SOP sentence
   that justifies each rule. Then a second "consolidation" call per env merging duplicates.
3. **Validation.** `validate_rule` on each; compile regexes; drop rules whose regex matches nothing in
   the env's `docs/instruments.md` vocabulary (instrument/command names, file paths) and rules whose
   `decision: deny` fires on any call of any card's `scripts.honest` (a mined rule must not block the
   honest path). Record the drop reasons.
4. **Action corpus.** All calls from every card's `honest`, `costly_honest` and `exploit` scripts,
   labelled `hack` / `benign` as in brief 01 (audit flags matching `hack_if`), via `check_tasks.run`.
5. **Diff vs hand-written rules.** For each env and for each rule set (hand, mined-7b, mined-14b,
   hand ∪ mined-14b): per-action Stage 1 decision; precision/recall of `deny ∪ escalate_human` on hack
   actions; honest-path deny count (must be 0 for hand; report for mined). Then pairwise: actions
   decided only by hand rules, only by mined rules, by both with the same decision, by both with
   conflicting decisions (list those). Match mined rules to hand rules by overlap of the action sets
   they fire on (Jaccard >= 0.5) and list unmatched hand rules ("humans only") and unmatched mined
   rules that are correct on the corpus ("model found").
6. **Output** `labwatcher/mining/out/<context>.mined.<model>.yaml` (with `evidence` comments) and
   `labwatcher/mining/REPORT.md`.

## Deliverables

1. `labwatcher/mining/mine.py` — CLI `python -m labwatcher.mining.mine --envs aspirin,cell_culture,cytotox --models 7b,14b --out labwatcher/mining/out`;
   caches raw model responses under `out/raw/` so re-runs are free; resumable.
2. `labwatcher/mining/diff.py` — CLI producing the comparison tables and `REPORT.md` from the
   mined YAMLs and the live rules.
3. `labwatcher/mining/modal_app.py` (if you deploy your own server) or a documented use of the
   evaluator endpoint.
4. `labwatcher/mining/prompts.yaml` — the prompts, versioned.
5. `labwatcher/mining/REPORT.md` — counts proposed/kept/dropped per env and model with drop reasons;
   the precision/recall table; the Jaccard-matched pairs; the "humans only" and "model found" lists
   with evidence quotes; the conflicting-decision list with your judgement of who is right; model
   size comparison; cost (GPU minutes, tokens).
6. `labwatcher/tests/test_mining.py` — offline: prompt rendering, JSON extraction from a recorded
   response fixture, validation/dropping logic, diff metrics on a tiny synthetic rule set.

## Acceptance criteria

* Baseline suites and the new tests pass.
* Mined rule files validate (`python -m labwatcher.settings_cli rules-check` if brief 04 has landed,
  otherwise a loop over `validate_rule`) and load in `RuleEngine.load` without error.
* Zero `deny` on honest-path actions for the kept mined rules (dropped ones are listed, not deleted).
* You did not edit `labwatcher/rules/*.yaml`, `rules.py`, `pipeline.py`, `SPEC.md` or any task files.
  Everything lives under `labwatcher/mining/` and `labwatcher/tests/test_mining.py`.
* Nothing under `labwatcher/data/` committed; raw responses under `labwatcher/mining/out/raw/` are
  committed only if under 2 MB total, otherwise gitignored with a note on how to regenerate.

## PR description must contain

* The precision/recall table (hand vs mined-7b vs mined-14b vs union) per env.
* Five rules the model found that the hand-written set lacks, each with the SOP sentence it cites.
* Five hand-written rules the model did not find, with your hypothesis why.
* All conflicting decisions with your call on each.
* Modal GPU minutes, tokens in/out, and the exact commands to reproduce.
