"""Layered YAML settings for LabWatcher (Watcher-style organisation-wide configuration).

Layers: built-in ``labwatcher/settings.yaml`` -> organisation file -> user file. Each layer
may carry a ``permissions`` mapping of dotted key paths to ``modifiable`` | ``locked`` |
``additions_allowed`` that governs what the layers *below* it may change:

* ``modifiable``        lower layers may replace the value (dicts merge recursively);
* ``locked``            lower-layer edits at or under the path are ignored and a warning is
                        recorded;
* ``additions_allowed`` lower layers may add new keys or list entries but may not change
                        existing ones (changes are ignored with a warning).

The most specific path wins. A ``locked`` permission set by a higher layer cannot be
loosened by a lower one. The user layer's own ``permissions`` section has no layer below it
and is ignored (with a warning). Validation runs after merging and collects problems into
``settings.errors`` / ``settings.warnings`` instead of raising, so the UI can show them.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from .rules import TAXONOMY

PACKAGE_DIR = Path(__file__).resolve().parent
BUILTIN_PATH = PACKAGE_DIR / "settings.yaml"
ORG_ENV = "LABWATCHER_ORG_SETTINGS"
USER_ENV = "LABWATCHER_USER_SETTINGS"

PERMISSIONS = ("modifiable", "locked", "additions_allowed")
KNOWN_TOOLS = ("list_files", "read_file", "write_file", "append_file", "instrument",
               "submit", "submit_report", "report_issue", "finish")
TOOL_MODES = ("auto_approve", "escalate", "always_escalate")
DEFAULT_ESCALATE_AT = 6
KNOWN_PROVIDERS = ("modal", "anthropic", "mock")
MODEL_ROLES = ("triage", "evaluator", "trailing", "agent")
HUMAN_AUTO = ("approve", "deny", "timeout_allow")
RULE_DECISIONS = ("allow", "deny", "escalate_triage", "escalate_human")
REQUIRED_SECTIONS = ("tools", "triage", "evaluator", "trailing", "suggestions", "human",
                     "models", "ui", "contexts", "taxonomy")

_MISSING = object()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Layer:
    __slots__ = ("name", "path", "data", "permissions")

    def __init__(self, name: str, path: Path | None, data: dict, permissions: dict):
        self.name, self.path, self.data, self.permissions = name, path, data, permissions


class ToolsView(Mapping):
    """``settings.tools[tool] -> {mode, escalate_at, deny_at}`` (normalised)."""

    def __init__(self, raw: dict):
        self._raw = raw

    @staticmethod
    def normalise(entry: Any) -> dict:
        if isinstance(entry, str):
            entry = {"mode": entry}
        if not isinstance(entry, dict):
            entry = {}
        mode = entry.get("mode")
        escalate_at = entry.get("escalate_at")
        deny_at = entry.get("deny_at")
        if mode is None:
            # no mode and no thresholds: grade at the SPEC default (escalate_at 6) rather than
            # silently auto-approving; auto_approve must be asked for explicitly.
            mode = "escalate"
            if escalate_at is None and deny_at is None:
                escalate_at = DEFAULT_ESCALATE_AT
        return {"mode": mode, "escalate_at": escalate_at, "deny_at": deny_at}

    def __getitem__(self, tool: str) -> dict:
        if tool not in self._raw:
            raise KeyError(tool)
        return self.normalise(self._raw[tool])

    def __iter__(self) -> Iterator[str]:
        return iter(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __repr__(self) -> str:
        return f"ToolsView({dict(self)!r})"


class Settings:
    def __init__(self, data: dict, layers: list[_Layer], permissions: dict,
                 sources: dict, errors: list[str], warnings: list[str]):
        self.data = data
        self.layers = layers
        self.permissions = permissions          # effective permission per dotted path
        self.sources = sources                  # dotted leaf path -> layer name that set it
        self.errors = errors
        self.warnings = warnings
        self._validate()

    # --- loading ------------------------------------------------------------------

    @classmethod
    def load(cls, org_path: str | Path | None = None, user_path: str | Path | None = None,
             builtin_path: str | Path | None = None) -> "Settings":
        errors: list[str] = []
        warnings: list[str] = []
        specs = [("builtin", Path(builtin_path or BUILTIN_PATH), True)]
        for name, given, env in (("org", org_path, ORG_ENV), ("user", user_path, USER_ENV)):
            explicit = given is not None
            p = given if explicit else os.environ.get(env)
            if p:
                specs.append((name, Path(p).expanduser(), explicit))

        layers: list[_Layer] = []
        for name, path, explicit in specs:
            data = cls._read_layer(name, path, explicit, errors, warnings)
            if data is None:
                continue
            perms = data.pop("permissions", None) or {}
            if not isinstance(perms, dict):
                errors.append(f"{name} ({path}): 'permissions' must be a mapping")
                perms = {}
            clean: dict = {}
            for k, v in perms.items():
                if v not in PERMISSIONS:
                    errors.append(f"{name} ({path}): permission {k!r} must be one of {PERMISSIONS}, got {v!r}")
                else:
                    clean[str(k)] = v
            layers.append(_Layer(name, path, data, clean))

        if not layers:
            errors.append("no settings layer could be loaded")
            layers.append(_Layer("builtin", None, {}, {}))

        merged: dict = {}
        effective_perms: dict = {}
        sources: dict = {}
        for i, layer in enumerate(layers):
            if i == 0:
                merged = copy.deepcopy(layer.data)
                _record_sources(merged, "", layer.name, sources)
            else:
                merged = _merge(merged, layer.data, "", effective_perms, layer.name, warnings, sources)
            if layer.name == "user" and layer.permissions:
                warnings.append("user layer: 'permissions' ignored (no layer below the user layer)")
            elif layer.permissions:
                for k, v in layer.permissions.items():
                    if effective_perms.get(k) == "locked" and v != "locked":
                        warnings.append(f"{layer.name}: cannot loosen locked permission for {k!r}")
                        continue
                    effective_perms[k] = v
        return cls(merged, layers, effective_perms, sources, errors, warnings)

    @staticmethod
    def _read_layer(name, path: Path, explicit: bool, errors, warnings) -> dict | None:
        if not path.exists():
            msg = f"{name} settings file not found: {path}"
            (errors if explicit or name == "builtin" else warnings).append(msg)
            return None
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            errors.append(f"{name} ({path}): YAML parse error: {e}")
            return None
        if not isinstance(raw, dict):
            errors.append(f"{name} ({path}): top level must be a mapping")
            return None
        return raw

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """A Settings built from an already-merged mapping (tests, UI previews)."""
        data = copy.deepcopy(data)
        perms = data.pop("permissions", {}) or {}
        sources: dict = {}
        _record_sources(data, "", "dict", sources)
        return cls(data, [_Layer("dict", None, data, perms)], dict(perms), sources, [], [])

    # --- views ----------------------------------------------------------------------

    def _section(self, name: str) -> dict:
        v = self.data.get(name)
        return v if isinstance(v, dict) else {}

    @property
    def tools(self) -> ToolsView:
        return ToolsView(self._section("tools"))

    @property
    def triage(self) -> dict:
        return self._section("triage")

    @property
    def evaluator(self) -> dict:
        return self._section("evaluator")

    @property
    def trailing(self) -> dict:
        return self._section("trailing")

    @property
    def suggestions(self) -> dict:
        return self._section("suggestions")

    @property
    def human(self) -> dict:
        return self._section("human")

    @property
    def models(self) -> dict:
        return self._section("models")

    @property
    def ui(self) -> dict:
        return self._section("ui")

    @property
    def contexts(self) -> dict:
        return self._section("contexts")

    @property
    def taxonomy(self) -> list:
        v = self.data.get("taxonomy")
        return v if isinstance(v, list) else []

    @property
    def taxonomy_ids(self) -> list[str]:
        return [t["id"] if isinstance(t, dict) else str(t) for t in self.taxonomy if t]

    @property
    def ok(self) -> bool:
        return not self.errors

    def context_path(self, context: str, key: str) -> Path | None:
        """Absolute path of a context's `rules` or `policy` file (relative paths are
        resolved against the labwatcher/ package)."""
        ctx = self.contexts.get(context) or {}
        p = ctx.get(key)
        if not p:
            return None
        p = Path(p)
        return p if p.is_absolute() else PACKAGE_DIR / p

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def permission_for(self, dotted: str) -> str:
        return _permission_for(dotted, self.permissions)

    def to_dict(self) -> dict:
        out = copy.deepcopy(self.data)
        out["permissions"] = dict(self.permissions)
        return out

    def effective(self) -> list[dict]:
        """Flat view for the Settings page: one row per leaf key with its value, the layer
        that set it, the effective permission and whether it is locked for lower layers."""
        rows = []
        for path, value in _flatten(self.data, ""):
            perm = self.permission_for(path)
            rows.append({"key": path, "value": value, "source": self.sources.get(path, "builtin"),
                         "permission": perm, "locked": perm == "locked"})
        return rows

    # --- validation -----------------------------------------------------------------

    def _validate(self) -> None:
        err, warn = self.errors.append, self.warnings.append
        d = self.data
        for s in REQUIRED_SECTIONS:
            if s not in d:
                err(f"missing section '{s}'")
            elif s == "taxonomy":
                if not isinstance(d[s], list):
                    err("'taxonomy' must be a list")
            elif not isinstance(d[s], dict):
                err(f"section '{s}' must be a mapping")

        tools = self._section("tools")
        for tool, entry in tools.items():
            if tool not in KNOWN_TOOLS:
                err(f"tools.{tool}: unknown tool (known: {', '.join(KNOWN_TOOLS)})")
            if isinstance(entry, str):
                entry = {"mode": entry}
            if not isinstance(entry, dict):
                err(f"tools.{tool}: must be a mapping like {{mode, escalate_at, deny_at}}")
                continue
            mode = entry.get("mode")
            if mode is not None and mode not in TOOL_MODES:
                err(f"tools.{tool}.mode: must be one of {TOOL_MODES}, got {mode!r}")
            for k in ("escalate_at", "deny_at"):
                if k in entry and entry[k] is not None:
                    v = entry[k]
                    if not _is_int(v) or not 1 <= v <= 10:
                        err(f"tools.{tool}.{k}: must be an integer 1-10, got {v!r}")
            ea, da = entry.get("escalate_at"), entry.get("deny_at")
            if _is_int(ea) and _is_int(da) and da < ea:
                warn(f"tools.{tool}: deny_at ({da}) is below escalate_at ({ea})")
            if mode is None and ea is None and da is None:
                warn(f"tools.{tool}: no mode and no thresholds; graded with escalate_at "
                     f"{DEFAULT_ESCALATE_AT} (set mode: auto_approve explicitly to skip grading)")
            if mode == "escalate" and ea is None and da is None:
                err(f"tools.{tool}: mode 'escalate' needs escalate_at and/or deny_at")
            for k in entry:
                if k not in ("mode", "escalate_at", "deny_at"):
                    warn(f"tools.{tool}: unknown key {k!r}")
        for tool in KNOWN_TOOLS:
            if tool not in tools and tool != "submit_report":
                warn(f"tools: no threshold for '{tool}' (will raise KeyError when graded)")

        self._check_number("triage.confidence_to_resolve", 0.0, 1.0)
        self._check_int("triage.context_messages", 1, 10_000)
        self._check_int("evaluator.context_messages", 1, 10_000)
        self._check_int("evaluator.human_decisions", 0, 1000)
        self._check_int("trailing.every_n_actions", 1, 10_000)
        self._check_int("trailing.window", 1, 10_000)
        self._check_int("trailing.flag_at", 1, 10, required=False)
        self._check_int("suggestions.threshold", 1, 10)
        self._check_int("ui.port", 1, 65535, required=False)
        self._check_int("ui.alert_threshold", 1, 10, required=False)
        for k in ("triage.enabled", "evaluator.enabled", "trailing.enabled", "suggestions.enabled"):
            v = self.get(k, _MISSING)
            if v is not _MISSING and not isinstance(v, bool):
                err(f"{k}: must be true/false, got {v!r}")
        tpl = self.get("suggestions.template")
        if tpl is None:
            err("suggestions.template: missing")
        elif not isinstance(tpl, str):
            err("suggestions.template: must be a string")
        elif "{message}" not in tpl:
            warn("suggestions.template: has no {message} placeholder")

        auto = self.human.get("auto")
        # null / "interactive" means escalations wait for a reviewer (Live UI / on_escalate).
        if auto is not None and str(auto).lower() != "interactive" and auto not in HUMAN_AUTO:
            err(f"human.auto: must be one of {HUMAN_AUTO} or null (interactive), got {auto!r}")

        models = self.models
        for role in MODEL_ROLES:
            if role not in models:
                err(f"models.{role}: missing")
        for role, cfg in models.items():
            if role not in MODEL_ROLES:
                warn(f"models.{role}: unknown role (known: {', '.join(MODEL_ROLES)})")
            if not isinstance(cfg, dict):
                err(f"models.{role}: must be a mapping {{provider, model, base_url_env}}")
                continue
            prov = cfg.get("provider")
            if prov not in KNOWN_PROVIDERS:
                err(f"models.{role}.provider: must be one of {KNOWN_PROVIDERS}, got {prov!r}")
            if not isinstance(cfg.get("model"), str) or not cfg.get("model"):
                err(f"models.{role}.model: must be a non-empty string")
            if "base_url_env" in cfg and not isinstance(cfg["base_url_env"], str):
                err(f"models.{role}.base_url_env: must be a string")
            fb = cfg.get("fallback", [])
            if not isinstance(fb, list):
                err(f"models.{role}.fallback: must be a list of providers")
            else:
                for p in fb:
                    if p not in KNOWN_PROVIDERS:
                        err(f"models.{role}.fallback: unknown provider {p!r}")

        for name, ctx in self.contexts.items():
            if not isinstance(ctx, dict):
                err(f"contexts.{name}: must be a mapping")
                continue
            for k in ("rules", "policy"):
                if not isinstance(ctx.get(k), str) or not ctx.get(k):
                    err(f"contexts.{name}.{k}: must be a file path")
            envs = ctx.get("envs")
            if not isinstance(envs, list) or not envs or not all(isinstance(e, str) for e in envs):
                err(f"contexts.{name}.envs: must be a non-empty list of env names")
            if "label" in ctx and not isinstance(ctx["label"], str):
                err(f"contexts.{name}.label: must be a string")
            rf = ctx.get("report_forms")
            if rf is not None and not isinstance(rf, dict):
                err(f"contexts.{name}.report_forms: must be a mapping env -> form")

        ids = self.taxonomy_ids
        if self.taxonomy and ids != list(TAXONOMY):
            err(f"taxonomy: ids must be exactly {list(TAXONOMY)} in order, got {ids}")

    def _check_int(self, dotted: str, lo: int, hi: int, required: bool = True) -> None:
        v = self.get(dotted, _MISSING)
        if v is _MISSING:
            if required:
                self.errors.append(f"{dotted}: missing")
            return
        if not _is_int(v) or not lo <= v <= hi:
            self.errors.append(f"{dotted}: must be an integer {lo}-{hi}, got {v!r}")

    def _check_number(self, dotted: str, lo: float, hi: float) -> None:
        v = self.get(dotted, _MISSING)
        if v is _MISSING:
            self.errors.append(f"{dotted}: missing")
            return
        if not _is_number(v) or not lo <= v <= hi:
            self.errors.append(f"{dotted}: must be a number {lo}-{hi}, got {v!r}")

    def __repr__(self) -> str:
        return f"Settings(layers={[l.name for l in self.layers]}, errors={len(self.errors)}, warnings={len(self.warnings)})"


# --- merge helpers ----------------------------------------------------------------------

def _permission_for(path: str, perms: dict) -> str:
    """Most specific explicit permission on the path or any ancestor; default modifiable."""
    if not path:
        return perms.get("", "modifiable")
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        p = ".".join(parts[:i])
        if p in perms:
            return perms[p]
    return perms.get("", "modifiable")


def _join(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _record_sources(node: Any, path: str, layer: str, sources: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _record_sources(v, _join(path, k), layer, sources)
    else:
        sources[path] = layer


def _flatten(node: Any, path: str):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from _flatten(v, _join(path, k))
    else:
        yield path, node


def _merge(base: Any, overlay: Any, path: str, perms: dict, layer: str,
           warnings: list[str], sources: dict) -> Any:
    perm = _permission_for(path, perms)
    label = path or "<root>"
    if perm == "locked":
        warnings.append(f"{layer}: '{label}' is locked; override ignored")
        return base

    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            child = _join(path, k)
            if k in base:
                out[k] = _merge(base[k], v, child, perms, layer, warnings, sources)
            else:
                # New key: allowed under modifiable and additions_allowed alike (unless the
                # child path itself is explicitly locked).
                if _permission_for(child, perms) == "locked":
                    warnings.append(f"{layer}: '{child}' is locked; addition ignored")
                    continue
                out[k] = copy.deepcopy(v)
                _record_sources(out[k], child, layer, sources)
        return out

    if perm == "additions_allowed":
        if isinstance(base, list) and isinstance(overlay, list):
            added = [x for x in overlay if x not in base]
            if added:
                sources[path] = layer
            return list(base) + copy.deepcopy(added)
        if base != overlay:
            warnings.append(f"{layer}: '{label}' allows additions only; change of existing value ignored")
        return base

    # modifiable scalar / list / type change -> replace
    if base != overlay:
        if isinstance(overlay, dict):
            _record_sources(overlay, path, layer, sources)
        else:
            sources[path] = layer
    return copy.deepcopy(overlay)
