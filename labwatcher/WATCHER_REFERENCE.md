# Reference: Apollo Research Watcher (coding-agent monitor) — what LabWatcher replicates

Source: https://watcher.apolloresearch.ai/blog/monitor-coding-agents/

**Integration.** Hook-based: hooks fire on session lifecycle events, user prompts, tool usage,
permission requests and session end. When the agent attempts a tool call, Watcher intercepts it and
runs it through a multi-stage evaluation pipeline.

**Blocking pipeline (Watcher Live).** Three sequential stages, each more capable but slower:
1. *Command rules*: zero-latency regex matching on tool name and command / file paths. Decisions:
   Allow, Deny, Escalate to triage, Escalate to human. ~60 built-in rules: known-safe patterns
   (git status, ls, cat) auto-approve; `curl | sh`, disabling security controls auto-deny.
2. *Triage model*: small fast model (e.g. Gemini Flash); most safe actions resolve in <2 s; outputs a
   confidence score that decides whether to advance to stage 3.
3. *Full evaluator*: only genuinely ambiguous or risky actions. Gets more of the transcript, the
   triage model's assessment and a history of recent human decisions. Stronger model (Claude Sonnet).
   Total latency overhead typically 1-5%.

**Tool-level thresholds.** Per tool type: Auto-approve (skip grading) / Escalate >= N / Deny >= N
(auto-deny and feed the reasoning back to the agent) / Always escalate (skip grading, ask human).
Defaults: read-only tools (Read, Grep, WebSearch) auto-approve; state-modifying tools (Bash, Edit,
Write) Escalate >= 6; plan tools always escalate. Risk score is 1-10, higher = worse.

**Policy Panel.** View and edit the prompts that drive the triage and gateway models: each model has a
system prompt defining what to look for, how to score, what context to consider. **Rules Panel**:
custom rule management.

**Human-in-the-loop.** Escalation shows the developer the risk score and explanation; they confirm or
deny. Operations like `git push`, `rm -rf` configured for human review. Auto-deny returns the
reasoning to the agent so it can adjust.

**Trailing monitors.** Periodically evaluate a window of the session transcript. Each dimension gets a
1-10 score (higher = worse). Default security monitor has nine categories: Secret and Credential
Exposure; Public Resource Creation; Data Exfiltration; Privilege Escalation; Unapproved Dependency or
Tool Installation; Infrastructure Disruption; Context and Memory Poisoning; Insecure Code; Prompt
Injection. Evaluation is harm-based: organisational consequence and blast radius, not whether the
behaviour looked odd. When thresholds are exceeded Watcher injects a suggestion into the agent's
context as a gentle course correction that appears as a system reminder (**Suggestion Injection**).

**Watcher Analyzer.** Web dashboard for security teams: summary stats (total sessions, blocked
actions, flagged sessions, failure rate); breakdown of failures by category and trends over time;
filterable session list sorted by severity, status, date; full transcript view with grades and
explanations alongside each message; Slack alerts with category, user, agent and a direct link;
configurable alert threshold by severity.

**Organisation-wide YAML configuration.** Layered: built-in defaults -> admin-managed
organisation `settings.yaml` -> user overrides. Per field/section permission: `modifiable`, `locked`,
`additions_allowed`. Domains: UI preferences; policy monitors (grader ids, score thresholds, context
window size); turn review settings (trailing grader id, enablement); suggestion settings (score
thresholds, prompt templates); tool thresholds (flag and deny thresholds per tool); command rules
(regex patterns, decisions, priorities). The settings loader validates on startup and surfaces
errors to the UI rather than silently overwriting bad files.

**LabWatcher mapping.** Coding agent -> lab-automation agent (labsim). Tools -> list_files,
read_file, write_file, append_file, instrument(name, command, args), submit(form, payload),
report_issue, finish. Secrets/exfil/privilege categories -> the nine lab categories in SPEC.md.
"Developer" -> lab supervisor / EHS officer. Gemini Flash -> Qwen2.5-7B on Modal (triage); Claude
Sonnet -> Qwen2.5-14B on Modal (evaluator), Anthropic only as fallback.
