"""Stage 1 of the LabWatcher blocking pipeline: zero-latency command rules.

A rule is ``{id, match: {tool?, command?, path?, args?}, decision, priority, reason, category}``.
Every ``match`` field is a regular expression (``re.search`` semantics, like grep) and every
field present must match (AND):

* ``tool``     against the tool name (``list_files`` ... ``finish``);
* ``command``  against ``"<instrument>.<command>"`` (only ``instrument`` calls have one);
* ``path``     against ``args["path"]`` of the file tools;
* ``args``     against ``json.dumps(args, sort_keys=True)`` of the *whole* tool args (so for an
               ``instrument`` call that includes ``name``, ``command`` and the nested ``args``;
               for ``submit`` it includes ``form`` and ``payload``).

Decisions: ``allow | deny | escalate_triage | escalate_human``. Highest priority wins; ties go
to the rule listed first. ``evaluate`` returns ``None`` when nothing matched (the pipeline then
proceeds to triage).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator

import yaml

TAXONOMY = (
    "interlock_bypass",
    "record_tampering",
    "data_fabrication",
    "unapproved_substitution",
    "hazard_release",
    "infrastructure_disruption",
    "sample_integrity",
    "scope_overreach",
    "prompt_injection",
)

DECISIONS = ("allow", "deny", "escalate_triage", "escalate_human")
MATCH_FIELDS = ("tool", "command", "path", "args")
RULE_FIELDS = ("id", "match", "decision", "priority", "reason", "category", "enabled")


class RuleError(ValueError):
    """A rule failed validation (message lists every problem)."""


@dataclass
class Rule:
    id: str
    match: dict
    decision: str
    priority: int = 0
    reason: str = ""
    category: str | None = None
    enabled: bool = True
    _compiled: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        self._compiled = {k: re.compile(v) for k, v in self.match.items()}

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_compiled", None)
        if d.get("enabled", True):
            d.pop("enabled")
        if d.get("category") is None:
            d["category"] = None
        return d

    def matches(self, action: Any) -> dict | None:
        """The matched text per field, or None if any field fails."""
        a = _ActionView(action)
        matched = {}
        for key, rx in self._compiled.items():
            text = a.text(key)
            if text is None:
                return None
            m = rx.search(text)
            if not m:
                return None
            matched[key] = m.group(0)
        return matched


@dataclass
class RuleHit:
    rule_id: str
    decision: str
    priority: int
    reason: str
    category: str | None
    matched: dict = field(default_factory=dict)

    @property
    def categories(self) -> list[str]:
        return [self.category] if self.category else []

    def to_dict(self) -> dict:
        return asdict(self)


class _ActionView:
    """Duck-typed read of an Action (dataclass, object or dict)."""

    def __init__(self, action: Any):
        self.a = action

    def _get(self, name: str):
        if isinstance(self.a, dict):
            return self.a.get(name)
        return getattr(self.a, name, None)

    def text(self, key: str) -> str | None:
        if key == "tool":
            t = self._get("tool")
            return str(t) if t is not None else None
        if key == "command":
            instrument, command = self._get("instrument"), self._get("command")
            args = self._get("args")
            if (instrument is None or command is None) and isinstance(args, dict) and self._get("tool") == "instrument":
                instrument = instrument if instrument is not None else args.get("name")
                command = command if command is not None else args.get("command")
            if instrument is None or command is None:
                return None
            return f"{instrument}.{command}"
        if key == "path":
            p = self._get("path")
            args = self._get("args")
            if p is None and isinstance(args, dict):
                p = args.get("path")
            return str(p) if p is not None else None
        if key == "args":
            args = self._get("args")
            if args is None:
                args = {}
            try:
                return json.dumps(args, sort_keys=True, default=str)
            except (TypeError, ValueError):
                return str(args)
        return None


def validate_rule(raw: Any, taxonomy=TAXONOMY) -> list[str]:
    """Problems with one rule dict (empty list = valid)."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return [f"rule must be a mapping, got {type(raw).__name__}"]
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid.strip():
        problems.append("id: must be a non-empty string")
    for k in raw:
        if k not in RULE_FIELDS:
            problems.append(f"unknown field {k!r}")
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        problems.append("match: must be a non-empty mapping of tool/command/path/args -> regex")
    else:
        for k, v in match.items():
            if k not in MATCH_FIELDS:
                problems.append(f"match.{k}: unknown match field (use {', '.join(MATCH_FIELDS)})")
                continue
            if not isinstance(v, str) or not v:
                problems.append(f"match.{k}: must be a non-empty regex string")
                continue
            try:
                re.compile(v)
            except re.error as e:
                problems.append(f"match.{k}: invalid regex {v!r} ({e})")
    dec = raw.get("decision")
    if dec not in DECISIONS:
        problems.append(f"decision: must be one of {DECISIONS}, got {dec!r}")
    pr = raw.get("priority", 0)
    if isinstance(pr, bool) or not isinstance(pr, int):
        problems.append(f"priority: must be an integer, got {pr!r}")
    reason = raw.get("reason", "")
    if reason is not None and not isinstance(reason, str):
        problems.append("reason: must be a string")
    cat = raw.get("category")
    if cat is not None and cat not in taxonomy:
        problems.append(f"category: {cat!r} is not a taxonomy id {list(taxonomy)}")
    en = raw.get("enabled", True)
    if not isinstance(en, bool):
        problems.append("enabled: must be true/false")
    return problems


def _build(raw: dict) -> Rule:
    return Rule(id=raw["id"].strip(), match={k: v for k, v in raw["match"].items()},
                decision=raw["decision"], priority=int(raw.get("priority", 0)),
                reason=raw.get("reason") or "", category=raw.get("category"),
                enabled=bool(raw.get("enabled", True)))


class RuleEngine:
    """Ordered rule set with evaluation, CRUD for the Rules Panel and YAML persistence."""

    def __init__(self, rules: list[Rule] | None = None, errors: list[str] | None = None,
                 path: str | Path | None = None, meta: dict | None = None):
        self.rules: list[Rule] = list(rules or [])
        self.errors: list[str] = list(errors or [])
        self.path = Path(path) if path else None
        self.meta = dict(meta or {})

    # --- loading / saving --------------------------------------------------------

    @classmethod
    def load(cls, source: str | Path | list | dict) -> "RuleEngine":
        """Load from a YAML file path or an in-memory list of rule dicts (or ``{rules: [...]}``).
        Bad rules are skipped and reported in ``.errors``; the engine still works."""
        errors: list[str] = []
        path: Path | None = None
        meta: dict = {}
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.exists():
                return cls([], [f"rules file not found: {path}"], path)
            try:
                raw = yaml.safe_load(path.read_text())
            except yaml.YAMLError as e:
                return cls([], [f"{path}: YAML parse error: {e}"], path)
        else:
            raw = source
        if isinstance(raw, dict):
            items = raw.get("rules")
            meta = {k: v for k, v in raw.items() if k != "rules"}
            if not isinstance(items, list):
                return cls([], [f"{path or 'rules'}: 'rules' must be a list"], path, meta)
        elif isinstance(raw, list):
            items = raw
        elif raw is None:
            items = []
        else:
            return cls([], [f"{path or 'rules'}: expected a list of rules or a mapping with 'rules'"], path, meta)

        rules: list[Rule] = []
        seen: set[str] = set()
        for i, item in enumerate(items):
            problems = validate_rule(item)
            rid = item.get("id") if isinstance(item, dict) else None
            label = f"rule[{i}]" + (f" ({rid})" if isinstance(rid, str) else "")
            if not problems and rid.strip() in seen:
                problems.append(f"duplicate id {rid!r}")
            if problems:
                errors.extend(f"{label}: {p}" for p in problems)
                continue
            rules.append(_build(item))
            seen.add(rid.strip())
        return cls(rules, errors, path, meta)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path to save to")
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = dict(self.meta)
        doc["rules"] = self.to_list()
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(target)
        self.path = target
        return target

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.rules]

    # --- evaluation --------------------------------------------------------------

    def evaluate(self, action: Any) -> RuleHit | None:
        best: tuple[Rule, dict] | None = None
        for rule in self.rules:
            if not rule.enabled:
                continue
            if best is not None and rule.priority <= best[0].priority:
                continue                      # cannot beat the current best (ties -> first)
            matched = rule.matches(action)
            if matched is not None:
                best = (rule, matched)
        if best is None:
            return None
        rule, matched = best
        return RuleHit(rule.id, rule.decision, rule.priority, rule.reason, rule.category, matched)

    def evaluate_all(self, action: Any) -> list[RuleHit]:
        """Every matching rule, highest priority first (for the Rules Panel 'test' box)."""
        hits = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            m = rule.matches(action)
            if m is not None:
                hits.append(RuleHit(rule.id, rule.decision, rule.priority, rule.reason, rule.category, m))
        hits.sort(key=lambda h: -h.priority)
        return hits

    # --- CRUD --------------------------------------------------------------------

    def get(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def add(self, raw: dict, position: int | None = None) -> Rule:
        problems = validate_rule(raw)
        if not problems and self.get(raw["id"].strip()) is not None:
            problems.append(f"duplicate id {raw['id']!r}")
        if problems:
            raise RuleError("; ".join(problems))
        rule = _build(raw)
        if position is None:
            self.rules.append(rule)
        else:
            self.rules.insert(position, rule)
        return rule

    def update(self, rule_id: str, changes: dict) -> Rule:
        idx = next((i for i, r in enumerate(self.rules) if r.id == rule_id), None)
        if idx is None:
            raise KeyError(rule_id)
        merged = self.rules[idx].to_dict()
        merged["enabled"] = self.rules[idx].enabled
        merged.update(copy.deepcopy(changes))
        problems = validate_rule(merged)
        new_id = merged.get("id")
        if not problems and new_id.strip() != rule_id and self.get(new_id.strip()) is not None:
            problems.append(f"duplicate id {new_id!r}")
        if problems:
            raise RuleError("; ".join(problems))
        self.rules[idx] = _build(merged)
        return self.rules[idx]

    def delete(self, rule_id: str) -> bool:
        n = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        return len(self.rules) < n

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self) -> Iterator[Rule]:
        return iter(self.rules)

    def __repr__(self) -> str:
        return f"RuleEngine({len(self.rules)} rules, {len(self.errors)} errors, path={self.path})"
