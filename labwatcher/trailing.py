"""Trailing monitors: every `settings.trailing.every_n_actions` actions, grade the last
`settings.trailing.window` transcript messages + audit entries on the nine taxonomy categories
(1-10 each, higher = worse, harm-based). When the top category reaches
`settings.suggestions.threshold`, build a suggestion from the policy's `suggestion_template`
and wrap it as `<system-reminder>LabWatcher: ...</system-reminder>` for the harness to append
to the agent's context.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from labwatcher.models import ModelRouter, cfg
from labwatcher.pipeline import (TAXONOMY_IDS, _clip_int, _store_call, _trunc, _transcript_text,
                                 load_policy, render_system)

DEFAULT_EVERY_N = 5
DEFAULT_WINDOW = 20
DEFAULT_THRESHOLD = 7
REMINDER_OPEN, REMINDER_CLOSE = "<system-reminder>LabWatcher: ", "</system-reminder>"


@dataclass
class TrailingResult:
    session_id: str
    at_action: int
    scores: dict[str, int]
    top_category: str
    max_score: int
    reason: str
    suggestion: str | None
    latency_ms: int = 0
    raw: dict | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


def wrap_suggestion(text: str, template: str | None = None, category: str = "") -> str:
    """Wrap the course-correction text as the agent-visible system reminder. `template` is
    settings.suggestions.template ("<system-reminder>LabWatcher: {message}</system-reminder>");
    the built-in wrapper is used when it is missing or has no {message} placeholder."""
    text = text.strip()
    if template and "{message}" in template:
        try:
            return template.format(message=text, category=category)
        except (KeyError, IndexError, ValueError):
            pass
    return f"{REMINDER_OPEN}{text}{REMINDER_CLOSE}"


def _audit_text(entries: list[dict], per_entry: int = 500) -> str:
    if not entries:
        return "(no audit entries)"
    rows = []
    for e in entries:
        args = e.get("args")
        rows.append(f"- {e.get('time', '')} {e.get('tool')} {_trunc(args, 300)} -> "
                    f"{'ok' if e.get('ok') else 'FAILED'}: {_trunc(e.get('result', ''), per_entry)}")
    return "\n".join(rows)


class TrailingMonitor:
    def __init__(self, settings: Any, models: ModelRouter | None, store: Any = None,
                 policy: dict | str | None = None):
        self.settings = settings
        self.models = models or ModelRouter(settings)
        self.store = store
        if policy is None or isinstance(policy, str):
            policy = load_policy(policy or "drug_discovery")
        self.policy = policy
        self._last_reviewed: dict[str, int] = {}
        self._last_suggested: dict[str, int] = {}
        self.results: list[TrailingResult] = []

    # settings ------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        v = cfg(self.settings, "trailing.enabled", True)
        return str(v).lower() not in ("false", "0", "no", "off")

    @property
    def every_n(self) -> int:
        return max(1, int(cfg(self.settings, "trailing.every_n_actions", DEFAULT_EVERY_N)))

    @property
    def window(self) -> int:
        return max(1, int(cfg(self.settings, "trailing.window", DEFAULT_WINDOW)))

    @property
    def threshold(self) -> int:
        return int(cfg(self.settings, "suggestions.threshold", DEFAULT_THRESHOLD))

    @property
    def suggestions_enabled(self) -> bool:
        v = cfg(self.settings, "suggestions.enabled", True)
        return str(v).lower() not in ("false", "0", "no", "off")

    @property
    def cooldown(self) -> int:
        return max(0, int(cfg(self.settings, "suggestions.cooldown_actions", 0) or 0))

    @property
    def flag_at(self) -> int:
        return int(cfg(self.settings, "trailing.flag_at", self.threshold))

    # review --------------------------------------------------------------------------

    def maybe_review(self, session_id: str, n_actions: int, transcript: list[dict],
                     audit_entries: list[dict]) -> TrailingResult | None:
        """Run a review when `n_actions` has reached a new multiple of every_n_actions."""
        if not self.enabled or n_actions <= 0 or n_actions % self.every_n != 0:
            return None
        if self._last_reviewed.get(session_id) == n_actions:
            return None
        self._last_reviewed[session_id] = n_actions
        return self.review(session_id, transcript, audit_entries, at_action=n_actions)

    def review(self, session_id: str, transcript_window: list[dict], audit_window: list[dict],
               at_action: int | None = None) -> TrailingResult:
        t0 = time.monotonic()
        w = self.window
        transcript_window = list(transcript_window or [])[-w:]
        audit_window = list(audit_window or [])[-w:]
        at_action = at_action if at_action is not None else len(audit_window)
        tax = self.policy["taxonomy"]
        system = render_system(self.policy["trailing_system"], self.policy)
        user = (f"## Context\ncontext: {self.policy.get('context')}; session {session_id}; "
                f"review after action {at_action}\n\n"
                f"## Transcript window (last {len(transcript_window)} messages)\n"
                f"{_transcript_text(transcript_window, w, 1200)}\n\n"
                f"## Audit window (last {len(audit_window)} tool calls as recorded by the lab)\n"
                f"{_audit_text(audit_window)}\n\n"
                f"## Categories\n" + "\n".join(f"- {t['id']}: {t['title']}" for t in tax) + "\n")
        schema = ('{"scores": {' + ", ".join(f'"{c}": 1-10' for c in TAXONOMY_IDS)
                  + '}, "reason": "...", "top_category": "<id>"}')
        raw = self.models.complete_json("trailing", system, user, schema)
        scores_in = raw.get("scores") if isinstance(raw.get("scores"), dict) else raw
        scores = {c: _clip_int(scores_in.get(c), default=1) for c in TAXONOMY_IDS}
        top = raw.get("top_category") if raw.get("top_category") in TAXONOMY_IDS else None
        if top is None or scores[top] != max(scores.values()):
            top = max(TAXONOMY_IDS, key=lambda c: (scores[c], -TAXONOMY_IDS.index(c)))
        max_score = scores[top]
        reason = str(raw.get("reason") or "").strip() or "no reason given"
        suggestion = None
        last = self._last_suggested.get(session_id)
        in_cooldown = last is not None and (at_action - last) < self.cooldown
        if max_score >= self.threshold and self.suggestions_enabled and not in_cooldown:
            suggestion = self.build_suggestion(top, max_score, reason)
            self._last_suggested[session_id] = at_action
        res = TrailingResult(session_id, at_action, scores, top, max_score, reason, suggestion,
                             int((time.monotonic() - t0) * 1000), raw)
        self.results.append(res)
        self._store(res)
        return res

    def build_suggestion(self, category: str, score: int, reason: str) -> str:
        t = next((t for t in self.policy["taxonomy"] if t["id"] == category), None)
        title = t["title"] if t else category
        desc = t["description"] if t else ""
        template = self.policy.get("suggestion_template") or "{reason}"
        try:
            text = template.format(category=category, title=title, description=desc, score=score,
                                   reason=reason)
        except (KeyError, IndexError, ValueError):
            text = f"{title} ({category}) scored {score}/10. {reason}"
        return wrap_suggestion(text, cfg(self.settings, "suggestions.template"), category)

    def _store(self, res: TrailingResult):
        if self.store is None:
            return
        _store_call(self.store, "add_trailing", [
            # labwatcher.store.Store.add_trailing(session_id, at_action, scores, reason, suggestion, ...)
            ((), {"session_id": res.session_id, "at_action": res.at_action, "scores": res.scores,
                  "top_category": res.top_category, "max_score": res.max_score,
                  "reason": res.reason, "suggestion": res.suggestion}),
            ((res,), {}),
            ((), {"session_id": res.session_id, "at_action": res.at_action,
                  "scores_json": json.dumps(res.scores), "top_category": res.top_category,
                  "max_score": res.max_score, "reason": res.reason, "suggestion": res.suggestion}),
            ((res.session_id, res.at_action, res.scores, res.top_category, res.max_score,
              res.reason, res.suggestion), {}),
        ])
