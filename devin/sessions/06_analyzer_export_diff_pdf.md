# Devin brief 06 — Analyzer: transcript export, session diff viewer, per-session PDF report

Repository: https://github.com/AIxScience-Hack/lab-agent-safety  base branch `watcher-trial`
Your branch: `devin/analyzer-export`  -> open a PR into `watcher-trial` when done.

## Goal

Extend the LabWatcher Analyzer (`labwatcher/ui/`) with three reviewer features: (1) export a
session's full transcript with grades as JSON, Markdown and an Inspect-compatible JSONL;
(2) a side-by-side diff viewer for two sessions (honest vs exploit on the same card, or the same
card with and without the watcher) that aligns actions and highlights where decisions diverged;
(3) a per-session PDF incident report suitable for an EHS file: summary, decisions, trailing
scores, human decisions, Amass literature context, and the audit-chain verification result.

## Why it matters

A monitor's output has to survive contact with people who were not watching the live feed: the
lab supervisor the morning after, the safety committee, an auditor. Apollo's Watcher Analyzer
exists for exactly this, and today LabWatcher's session page is view-only. Diffing honest against
exploit sessions is also how the team debugs the pipeline (which action should have been caught,
at which stage) and how the calibration and red-team studies in briefs 01 and 03 will be read.

## Setup

```bash
git clone https://github.com/AIxScience-Hack/lab-agent-safety && cd lab-agent-safety
git checkout watcher-trial && git checkout -b devin/analyzer-export
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
export LABWATCHER_PROVIDER=mock
(cd drug_discovery && ../.venv/bin/python -m pytest -q && ../.venv/bin/python check_tasks.py)
.venv/bin/python -m pytest labwatcher/tests -q
./run_demo.sh &     # seeds demo sessions; open http://127.0.0.1:8787
```

Read `labwatcher/SPEC.md` (Store schema, UI routes), `labwatcher/store.py` (`session(id)` returns
the session with actions, transcript, trailing, human_decisions, enrichment), `labwatcher/ui/app.py`
(`create_app`, Jinja-free static pages, `/api/sessions/{sid}`), `labwatcher/ui/static/session.html`
+ `app.js` + `app.css` (vanilla JS, no build step: keep it that way), `labwatcher/ui/fixtures.py`
(demo data you can test against), `labwatcher/tests/test_ui.py` (TestClient patterns),
`drug_discovery/INSPECT.md` (what an Inspect log sample looks like), `labwatcher/enrich/amass.py`
(`summarise_record` output shape).

**Use Modal for any GPU work (the Modal token is configured in the org; name apps `labwatcher-*`).**
This brief needs no GPU; run everything with `LABWATCHER_PROVIDER=mock`. If you want real
sessions to screenshot, the batch runner in `labwatcher/modal/README.md` produces them on Modal.

## Deliverables

### 1. Export — `labwatcher/ui/export.py` + routes

* `GET /api/sessions/{sid}/export?format=json` — the complete session record (session row, actions
  with triage/evaluator JSON, transcript, trailing, human decisions, enrichment) as one document
  with a `schema_version`.
* `format=md` — a readable Markdown transcript: header table (context, env, card, condition, arm,
  model, outcome, max score, blocked/escalated counts), then each transcript message in order with
  the graded action rendered beside it (decision badge, stage, score, rule id, reason, categories),
  trailing reviews inserted at the action index where they ran, suggestions shown as block quotes,
  human decisions inline.
* `format=inspect` — JSONL of Inspect-style `ChatMessage` dicts (`role`, `content`, `tool_calls`
  with `id/function/arguments`, tool results with `tool_call_id`) plus a final metadata line with the
  LabWatcher decisions keyed by tool-call id, so a session can be loaded next to `inspect view` logs.
  Document the exact mapping in the module docstring.
* Export buttons on `/session/{id}`; `Content-Disposition` attachment with a sensible filename.

### 2. Diff viewer — `/compare?a=<sid>&b=<sid>` + `labwatcher/ui/static/compare.html`

* `GET /api/compare?a=&b=` returns an alignment of the two sessions' actions: pair by
  `(tool, instrument, command, normalised path)` in order using a longest-common-subsequence
  alignment, leaving unpaired actions as insertions/deletions. Each pair carries both decisions and
  a `divergence` flag when decision, stage or |score difference| >= 3 differ.
* The page shows two columns with aligned rows, colour-coded by decision, a sticky header with both
  sessions' summary, a "divergences only" toggle, and per-row expand to see both evaluator reasons.
  A picker on the Analyzer session list ("compare with...") fills `a`/`b`; the session page links to
  "compare with the honest/exploit twin" when the store has a session with the same `card` and the
  opposite `condition`/`source`.
* Keep it vanilla JS; reuse `app.css` classes.

### 3. PDF report — `GET /api/sessions/{sid}/report.pdf`

* Pure-Python PDF generation (`fpdf2` preferred; add the pinned version to `requirements.txt` with
  `uv pip install --python .venv/bin/python fpdf2`). A4, header with LabWatcher name, session id,
  generated-at, and the context/env/card line. Sections: 1 Summary (outcome, max score, counts, model,
  provider); 2 Timeline table of graded actions (seq, tool, command/path, decision, stage, score,
  rule, reason truncated); 3 Denied and escalated actions in full (reason, evaluator categories,
  human decision + note); 4 Trailing monitor scores (table of the nine categories per review, flagged
  cells bold) and suggestions injected; 5 Literature context (Amass enrichment rows: source, title,
  identifier, one-line summary; "no enrichment available" when empty); 6 Integrity (audit head,
  genesis, `audit_verified`, number of entries, whether the monitor view chain verifies); 7 Appendix:
  the full transcript, wrapped. Page numbers on every page.
* A "Download PDF" button on `/session/{id}`.

### 4. Tests — `labwatcher/tests/test_export.py`, `test_compare.py`, `test_pdf_report.py`

Using `create_app(store=...)` with the fixtures store: each export format returns 200 with the right
content type and round-trips (JSON re-loads; Markdown contains every action's seq and reason; inspect
JSONL parses line by line and tool-call ids are unique and cross-referenced); compare aligns an
honest/exploit pair from fixtures and flags at least one divergence, handles unequal lengths and
identical sessions (zero divergences); unknown session ids give 404; the PDF starts with `%PDF`,
has >= 2 pages, and `pypdf` (dev-only; or `fpdf2`'s own parsing) extracts the session id and the
word "LabWatcher" from page 1.

## Acceptance criteria

* Baseline suites and the new tests pass; the existing `labwatcher/tests/test_ui.py` still passes untouched.
* No build step, no new JS dependencies; `fpdf2` is the only new Python dependency (pinned in `requirements.txt`).
* Edits to `labwatcher/ui/app.py` are additive (new routes under a clearly marked block; or mount a
  sub-router from `export.py`); do not restructure `create_app`. No change to `store.py` schema;
  read through the existing `Store` methods only. No change to `labwatcher/SPEC.md`.
* Manual check with `./run_demo.sh`: open a demo session, download all three exports and the PDF,
  open `/compare` on an honest/exploit pair; attach screenshots of the compare page and page 1 of the
  PDF to the PR.
* Works with the `memory` store too (`LABWATCHER_STORE=memory`).

## PR description must contain

* Screenshots: compare page (divergences highlighted) and PDF page 1.
* The inspect JSONL mapping table (LabWatcher field -> Inspect field).
* The alignment algorithm in three sentences and its behaviour on unequal sessions.
* Lines changed in `ui/app.py` and `requirements.txt`.
