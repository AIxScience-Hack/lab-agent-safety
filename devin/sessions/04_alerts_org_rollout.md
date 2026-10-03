# Devin brief 04 — Slack / webhook alerts and organisation-wide YAML rollout

Repository: https://github.com/AIxScience-Hack/lab-agent-safety  base branch `watcher-trial`
Your branch: `devin/alerts-org-rollout`  -> open a PR into `watcher-trial` when done.

## Goal

Give LabWatcher the two operational features of Apollo's Watcher it still lacks: (1) alerts to
Slack or any webhook when an action is denied or escalated or a trailing monitor flags a
session, with category, session/user, agent model and a direct link to the Analyzer session
page, configurable by severity; (2) an organisation rollout kit: an admin-managed
`org-settings.yaml` that locks safety-critical settings, a `settings_cli` validator that fails
loudly on bad or forbidden overrides, and an installer script that puts the layered settings
and the Modal URLs in place on a lab workstation.

## Why it matters

A monitor nobody is paged by is a log. In a lab, a denied `heater.disable_cutoff` at 02:00 must
reach the EHS officer's phone, not wait for someone to open a dashboard. And the thresholds that
make LabWatcher safe (deny the release at 8, escalate interlock overrides to a human) must not be
quietly relaxed by a user-level YAML; Watcher solves this with layered settings and per-field
`locked` permissions, which `labwatcher/settings.py` already implements but nothing enforces at
deploy time.

## Setup

```bash
git clone https://github.com/AIxScience-Hack/lab-agent-safety && cd lab-agent-safety
git checkout watcher-trial && git checkout -b devin/alerts-org-rollout
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
export LABWATCHER_PROVIDER=mock
(cd drug_discovery && ../.venv/bin/python -m pytest -q && ../.venv/bin/python check_tasks.py)
.venv/bin/python -m pytest labwatcher/tests -q
```

Read `labwatcher/SPEC.md`, `labwatcher/WATCHER_REFERENCE.md` (Analyzer alerts, org YAML),
`labwatcher/settings.py` + `settings.yaml` (layers, `permissions`, `Settings.load`, `effective()`),
`labwatcher/hooks.py` (where decisions and trailing results are produced), `labwatcher/store.py`,
`labwatcher/ui/app.py` (`/api/settings`, `/settings`), `labwatcher/modal/README.md`.

**Use Modal for any GPU work (the Modal token is configured in the org; name apps `labwatcher-*`).**
This brief needs no GPU except for the end-to-end check below.

## Deliverables

### 1. `labwatcher/alerts.py`

* `AlertSink` protocol with `send(event: dict) -> bool`; implementations `SlackWebhookSink`
  (Slack incoming-webhook, Block Kit message: header with severity emoji-free label, fields for
  category, context/env/card, session id, agent model, decision + stage + score, reason, and a
  button/link `<base_url>/session/<id>`), `JsonWebhookSink` (POST the event as JSON, optional
  HMAC-SHA256 signature header `X-LabWatcher-Signature` using `alerts.signing_secret_env`), and
  `LogSink` (stdlib logging; default when no URL is set).
* `Alerter(settings, sinks)` with `on_decision(session, action, decision)` and
  `on_trailing(session, trailing_result)`. Fires when `decision.action in {"deny","escalate"}` and
  `score >= alerts.min_score` (rule-based denies have `score=None`; treat as 10), or when the top
  trailing category `>= alerts.trailing_min_score`. Dedupe: at most one alert per
  (session, category) per `alerts.cooldown_s`; never block the pipeline (thread pool, swallow and
  log sink errors; `httpx` timeout 5 s, one retry).
* Settings keys (add to `labwatcher/settings.yaml` with defaults and to the permissions block):
  ```yaml
  alerts:
    enabled: false
    min_score: 7
    trailing_min_score: 7
    cooldown_s: 300
    base_url: http://127.0.0.1:8787
    sinks:
      - {type: slack, url_env: LABWATCHER_SLACK_WEBHOOK}
      - {type: webhook, url_env: LABWATCHER_ALERT_WEBHOOK, signing_secret_env: LABWATCHER_ALERT_SECRET}
  ```
* Wiring: minimal, additive edits. In `labwatcher/hooks.py::WatchedLab` call the alerter after a
  decision is recorded and after a trailing review (an optional `alerter=` constructor argument,
  default built from settings). Expose recent alerts in the UI: `GET /api/alerts` (last 100, from a
  new `alerts` table in the store added via `Store` migration) and an "Alerts" card on `/settings`.
  Do not restructure `hooks.py` or `ui/app.py`; other work is landing there concurrently.

### 2. `labwatcher/settings_cli.py`

`python -m labwatcher.settings_cli <command>`:

* `validate [--org PATH] [--user PATH] [--strict]` — loads the layers with `Settings.load`, prints
  every warning/error with the dotted path, the layer, and the permission that forbade it; exit 0 if
  clean, 2 if a lower layer tried to change a `locked` key or an `additions_allowed` key's existing
  value, 3 on schema errors (unknown tool mode, threshold outside 1-10, deny_at < escalate_at,
  unknown taxonomy id in a rule file referenced by a context, missing policy/rules files). `--strict`
  turns warnings into exit 2.
* `effective [--org] [--user] [--format yaml|json]` — the merged settings with a `# from: <layer>`
  annotation per key (use `Settings.effective()`).
* `lock-check --org PATH` — asserts the org file locks the safety-critical set:
  `tools.submit_report.deny_at`, `tools.instrument.escalate_at`, `tools.write_file`, `tools.append_file`,
  `triage.confidence_to_resolve`, `human.auto`, `alerts.enabled`, `alerts.min_score`, `taxonomy`; exit 2 listing the unlocked ones.
* `rules-check --context drug_discovery|materials_discovery [--rules PATH]` — runs `labwatcher.rules.validate_rule`
  over the file and exits non-zero on the first error.

### 3. `deploy/`

* `deploy/org-settings.example.yaml` — a realistic admin file: Modal provider pinned, Anthropic
  fallback disabled, `human.auto: deny`, alerts enabled to Slack, `permissions` locking the set above
  and `additions_allowed` for `contexts` and rules.
* `deploy/user-settings.example.yaml` — a harmless user override (UI theme, page size) plus one
  commented-out forbidden override so `validate` can be demonstrated failing.
* `deploy/install.sh` — bash, idempotent: creates the uv venv, installs `requirements.txt`, copies the
  org file to `/etc/labwatcher/settings.yaml` (or `$LABWATCHER_PREFIX`), writes an env file with
  `LABWATCHER_ORG_SETTINGS`, `LABWATCHER_PROVIDER=modal`, `LABWATCHER_TRIAGE_URL`, `LABWATCHER_EVALUATOR_URL`
  (taken from flags or `modal app list` output), runs `settings_cli validate --strict` and
  `lock-check`, and installs a `systemd` unit or `launchd` plist (detect OS) that runs
  `uvicorn labwatcher.ui.app:app --host 127.0.0.1 --port 8787`. `--dry-run` prints what it would do.
* `deploy/README.md` — rollout steps and the alert payload reference.

### 4. Tests (`labwatcher/tests/test_alerts.py`, `test_settings_cli.py`)

Offline: Slack and JSON payload shape (use a fake `httpx` transport), HMAC signature verifies,
cooldown dedupe, sink failure does not raise into the pipeline, `WatchedLab` with a mock grader
and a recording sink fires exactly on denies/escalations/trailing flags; CLI exit codes for the
clean, locked-violation and schema-error cases using the two example YAMLs; `install.sh --dry-run`
runs under `bash -n` and produces the expected plan text.

## Acceptance criteria

* Baseline suites and the new tests pass; `bash -n deploy/install.sh` clean; `shellcheck` clean if available.
* `python -m labwatcher.settings_cli validate --org deploy/org-settings.example.yaml --user deploy/user-settings.example.yaml`
  exits 0; uncommenting the forbidden override makes it exit 2 with a message naming the dotted path.
* End-to-end: with a Slack webhook URL you create in a scratch workspace (or a `webhook.site` URL for
  the JSON sink) and `LABWATCHER_PROVIDER=mock`, running the exploit demo for card `a01` produces one
  alert whose link opens `/session/<id>`; paste the redacted payload in the PR.
* No change to `labwatcher/SPEC.md`; edits to `hooks.py`, `store.py`, `ui/app.py` are additive and
  each under ~40 lines.

## PR description must contain

* The alert payload (Slack blocks JSON and generic JSON) for one real deny.
* The CLI transcript of `validate` passing and failing, and of `lock-check`.
* The list of settings the example org file locks and why each is safety-critical.
* Exactly which lines in `hooks.py` / `store.py` / `ui/app.py` changed.
