# devin/ — parallel "lab" sessions for Cognition Devin

Six self-contained research briefs. Each is a different idea that one Devin session can carry
end to end against **github.com/AIxScience-Hack/lab-agent-safety**, branch **`watcher-trial`**,
and finish as a pull request **into `watcher-trial`** from a branch named **`devin/<slug>`**.
They do not depend on each other and touch mostly disjoint files, so run all six at once.

| # | brief | slug (branch `devin/<slug>`) | new files live under |
|---|---|---|---|
| 01 | [Monitor calibration study](sessions/01_monitor_calibration.md) | `monitor-calibration` | `labwatcher/calibration/` |
| 02 | [Second materials env: thin-film sputtering + XRD / ellipsometry](sessions/02_thin_film_env.md) | `thin-film-env` | `materials_discovery/envs/thin_film.py`, `materials_discovery/tasks/thin_film/` |
| 03 | [Red team: Stage 1 regex evasion](sessions/03_redteam_rule_evasion.md) | `redteam-rule-evasion` | `labwatcher/redteam/` |
| 04 | [Slack / webhook alerts + org YAML rollout](sessions/04_alerts_org_rollout.md) | `alerts-org-rollout` | `labwatcher/alerts.py`, `labwatcher/settings_cli.py`, `deploy/` |
| 05 | [Rules auto-mining from SOPs and manuals](sessions/05_rules_mining.md) | `rules-mining` | `labwatcher/mining/` |
| 06 | [Analyzer export, diff viewer, PDF report](sessions/06_analyzer_export_diff_pdf.md) | `analyzer-export` | `labwatcher/ui/export.py`, `labwatcher/ui/static/compare.html` |

Every brief has the same skeleton: goal, why it matters for lab-agent safety, exact setup,
deliverables, acceptance criteria, what the PR description must contain, and the GPU rule:

> Use Modal for any GPU work (the Modal token is configured in the org; name apps `labwatcher-*`).
> Never run model inference on the Devin VM's CPU.

## Launching

### From the Devin app

1. New session -> repository `AIxScience-Hack/lab-agent-safety`, base branch `watcher-trial`.
2. Paste the whole brief (`devin/sessions/NN_*.md`) as the prompt. The briefs are written to be
   the complete instruction; add nothing but secrets.
3. Make sure the session has the org's **Modal** token, `ANTHROPIC_API_KEY` (optional, graders fall
   back to it) and `AMASS_API_KEY` (optional, brief 06 only) as Devin secrets.
4. Repeat for the other five briefs; they run concurrently.

### From the API

```bash
export DEVIN_API_KEY=...                        # Settings -> API keys in the Devin app
cd lab-agent-safety
for f in devin/sessions/0*.md; do
  slug=$(basename "$f" .md | sed 's/^[0-9]*_//' | tr '_' '-')
  jq -n --arg prompt "$(cat "$f")" --arg title "labwatcher $slug" \
     '{prompt: $prompt, title: $title, idempotent: true, tags: ["labwatcher", $title]}' \
  | curl -sS -X POST https://api.devin.ai/v1/sessions \
      -H "Authorization: Bearer $DEVIN_API_KEY" -H "Content-Type: application/json" -d @- \
  | jq -r '"\(.session_id)  \(.url)"'
done
```

`idempotent: true` means re-running the loop does not create duplicate sessions for an unchanged
brief. Poll `GET /v1/sessions/{session_id}` for `status_enum` and the PR URL in
`structured_output` / messages, or just watch for PRs on the repo:
`gh pr list --repo AIxScience-Hack/lab-agent-safety --base watcher-trial --head 'devin/'`.

### From an MCP client (Claude Code, Cursor, ...): the `X-Org-Id` fix

The Devin MCP server (`https://mcp.devin.ai/mcp`) exposes `devin_session_create`,
`devin_session_interact`, `devin_session_events`, ... With an API key that belongs to more than
one organisation (or an enterprise key) the first call fails with:

```
no org_id could be resolved from your token ... pass the target org via the X-Org-Id request
header in your MCP client configuration
```

The token alone does not name an org, so the server cannot pick one. Fix: send the target
organisation id as an HTTP header on every MCP request. Find the id in the Devin app under
**Settings -> Organization** (also the `org_...` segment in the settings URL).

Claude Code:

```bash
claude mcp remove devin 2>/dev/null
claude mcp add --transport http devin https://mcp.devin.ai/mcp \
  --header "Authorization: Bearer $DEVIN_API_KEY" \
  --header "X-Org-Id: <your org id>"
```

Generic `.mcp.json` / Cursor `mcp.json`:

```json
{
  "mcpServers": {
    "devin": {
      "type": "http",
      "url": "https://mcp.devin.ai/mcp",
      "headers": {
        "Authorization": "Bearer ${DEVIN_API_KEY}",
        "X-Org-Id": "<your org id>"
      }
    }
  }
}
```

Restart the client; `devin_session_create` with `prompt` = the brief text then works, one call per
brief. Without the header every `devin_*` tool returns the error above even though
`read_wiki_*` (DeepWiki) tools keep working, which is how the failure usually shows up.

## Reviewing the PRs

Each PR must: target `watcher-trial`; keep `cd drug_discovery && python -m pytest -q && python
check_tasks.py` and `python -m pytest labwatcher/tests -q` green; add tests for its own code;
not edit `labwatcher/SPEC.md`; not commit anything under `labwatcher/data/` (gitignored); and
carry the PR-description sections its brief lists. Merge order does not matter; 02 and 05 both
add rules to `labwatcher/rules/materials_discovery.yaml` so expect a trivial YAML conflict there.
