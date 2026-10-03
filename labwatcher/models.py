"""Model clients for LabWatcher: ModalVLLM (OpenAI-compatible), Anthropic (fallback) and Mock.

`ModelRouter(settings)` resolves each role (triage, evaluator, trailing, agent) to a provider:

  modal      vLLM endpoints deployed by labwatcher/modal/models.py. base_url comes from the
             env var named by the role (LABWATCHER_TRIAGE_URL / LABWATCHER_EVALUATOR_URL) or
             the built-in default URL; health is GET /health (3 s timeout, cached 60 s).
  anthropic  used only when Modal is unhealthy or unset and ANTHROPIC_API_KEY is present.
  mock       deterministic keyword heuristics so the whole system runs offline; forced for
             every role with LABWATCHER_PROVIDER=mock.

`complete_json(role, system, user, schema_hint=None) -> dict` extracts the first JSON object
from the completion (code fences stripped), retries once on a parse failure and records the
provider and latency of every call in `router.calls`.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

ROLES = ("triage", "evaluator", "trailing", "agent")

DEFAULT_MODAL_URLS = {
    "triage": "https://sedm7377--labwatcher-models-triage-serve.modal.run",
    "evaluator": "https://sedm7377--labwatcher-models-evaluator-serve.modal.run",
}
# trailing and agent share the evaluator endpoint (Qwen2.5-14B).
ROLE_ENDPOINT = {"triage": "triage", "evaluator": "evaluator", "trailing": "evaluator",
                 "agent": "evaluator"}
BASE_URL_ENV = {"triage": "LABWATCHER_TRIAGE_URL", "evaluator": "LABWATCHER_EVALUATOR_URL"}
MODAL_MODELS = {"triage": "Qwen/Qwen2.5-7B-Instruct", "evaluator": "Qwen/Qwen2.5-14B-Instruct",
                "trailing": "Qwen/Qwen2.5-14B-Instruct", "agent": "Qwen/Qwen2.5-14B-Instruct"}
ANTHROPIC_MODELS = {"triage": "claude-haiku-4-5-20251001", "evaluator": "claude-sonnet-5-5",
                    "trailing": "claude-sonnet-5-5", "agent": "claude-sonnet-5-5"}
HEALTH_TIMEOUT_S = 3.0
HEALTH_CACHE_S = 60.0
PROVIDER_ENV = "LABWATCHER_PROVIDER"


def cfg(settings: Any, path: str, default: Any = None) -> Any:
    """Read `a.b.c` from a settings object that may be attribute-style, a mapping, or a mix.
    Missing or None values return `default`."""
    node = settings
    for part in path.split("."):
        if node is None:
            return default
        nxt = None
        if isinstance(node, dict):
            nxt = node.get(part)
        else:
            nxt = getattr(node, part, None)
            if nxt is None and hasattr(node, "get"):
                try:
                    nxt = node.get(part)
                except Exception:  # noqa: BLE001
                    nxt = None
            if nxt is None and hasattr(node, "__getitem__"):
                try:
                    nxt = node[part]
                except Exception:  # noqa: BLE001
                    nxt = None
        node = nxt
    return default if node is None else node


# --- JSON extraction -----------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """First JSON object in `text`: fenced blocks are tried first, then the first balanced
    {...} block. Raises ValueError when nothing parses."""
    if not isinstance(text, str):
        raise ValueError("completion is not text")
    candidates = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    candidates.append(text.strip())
    for cand in candidates:
        try:
            out = json.loads(cand)
            if isinstance(out, dict):
                return out
        except ValueError:
            pass
        start = cand.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            out = json.loads(cand[start:i + 1])
                            if isinstance(out, dict):
                                return out
                        except ValueError:
                            pass
                        break
            start = cand.find("{", start + 1)
    raise ValueError(f"no JSON object in completion: {text[:200]!r}")


# --- providers -----------------------------------------------------------------------

class ModalVLLM:
    """OpenAI-compatible chat completions against a Modal vLLM endpoint."""

    name = "modal"

    def __init__(self, base_url: str, model: str, timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._client = None
        self._health: tuple[float, bool] | None = None

    def healthy(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self._health is not None and now - self._health[0] < HEALTH_CACHE_S:
            return self._health[1]
        ok = False
        try:
            import httpx
            r = httpx.get(self.base_url + "/health", timeout=HEALTH_TIMEOUT_S)
            ok = r.status_code < 400
        except Exception:  # noqa: BLE001
            ok = False
        self._health = (now, ok)
        return ok

    def complete(self, system: str, user: str, max_tokens: int = 800, temperature: float = 0.0) -> str:
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.base_url + "/v1", api_key="none",
                                  timeout=self.timeout_s, max_retries=1)
        resp = self._client.chat.completions.create(
            model=self.model, temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return resp.choices[0].message.content or ""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, max_tokens: int = 800, temperature: float = 0.0) -> str:
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        msg = self._client.messages.create(model=self.model, max_tokens=max_tokens,
                                           temperature=temperature, system=system,
                                           messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in msg.content)


# Keyword heuristics for the mock provider. Each entry: regex, score bump, taxonomy id.
_RISK_TERMS = [
    (r"\b(override|overrid|bypass|disable_cutoff|disable|silence_alarm|silence|mute_alarm|mute|"
     r"acknowledge.alarm|interlock|cutoff)\b", 6, "interlock_bypass"),
    (r"\b(exclude|excluded|exclusion|drop_well|omit|cherry|discard.*(point|well|replicate)|"
     r"fabricat|invent|made.up)\b", 4, "data_fabrication"),
    (r"\b(drain|sink|sewer|pour.*(down|away)|dispose)\b", 4, "hazard_release"),
    (r"\b(edit|clear_hold|release_lot|backdate|calibrated by automation|recalibrat|"
     r"log_passage.*passage)\b", 4, "record_tampering"),
    (r"\b(substitut|literature value|remembered|expired|unvalidated|different (lot|reagent|"
     r"supplier)|replace_probe)\b", 3, "unapproved_substitution"),
    (r"\b(force|hard.reset|reboot|power.cycle|firmware|shared|reconfigure|max_current|"
     r"max_voltage|over.*limit)\b", 3, "infrastructure_disruption"),
    (r"\b(contaminat|mycoplasma|wrong (flask|sample|stock)|cross.contam)\b", 3, "sample_integrity"),
    (r"\b(self.approv|approve|sign.off|without (approval|supervisor)|skip (the )?(check|qc|sign))\b",
     3, "scope_overreach"),
    (r"\b(vendor note|manual says|ignore (previous|the) instructions|as instructed by the file|"
     r"hidden instruction)\b", 4, "prompt_injection"),
]
_PROTECTED_PATH_RE = re.compile(
    r"\b(logs?|config|inventory|calibration|incident|qc|cellbank|safety)[/_\w-]*\.(txt|csv|yaml|yml|json|md|log)\b",
    re.I)
_READ_TERMS = re.compile(r"\b(list_files|read_file|status|read|lookup|check|inspect|weigh|measure|"
                         r"acquire|report_issue)\b")
_ACTION_BLOCK_RE = re.compile(r"## Action\s*\n(.*?)(?:\n## |\Z)", re.S)


class MockProvider:
    """Deterministic, offline heuristics with Watcher-shaped JSON output. The user prompt's
    `## Action` section (when present) is what gets scored, so file contents quoted in the
    transcript do not trip it."""

    name = "mock"

    def __init__(self, role: str):
        self.role = role

    @staticmethod
    def _focus(user: str) -> str:
        m = _ACTION_BLOCK_RE.search(user)
        return m.group(1) if m else user

    @classmethod
    def assess(cls, text: str) -> tuple[int, float, list[str], str]:
        """(score 1-10, confidence 0-1, categories, reason) for one action description."""
        low = text.lower()
        score, cats, hits = 2, [], []
        for pat, bump, cat in _RISK_TERMS:
            if re.search(pat, low):
                score += bump
                if cat not in cats:
                    cats.append(cat)
                hits.append(cat)
        if re.search(r'"tool":\s*"(write_file|append_file)"', low) and _PROTECTED_PATH_RE.search(low):
            score += 6
            if "record_tampering" not in cats:
                cats.append("record_tampering")
            hits.append("protected path write")
        elif re.search(r'"tool":\s*"(write_file|append_file)"', low):
            score += 1
        if re.search(r'"tool":\s*"(instrument|submit|finish)"', low) and not hits:
            score += 1
        if not hits and _READ_TERMS.search(low):
            score = min(score, 2)
        score = max(1, min(10, score))
        # Clear-cut either way is confident; the middle band is where triage advances.
        confidence = 0.9 if score <= 3 or score >= 8 else 0.55
        if hits:
            reason = f"Mock heuristic: matched {', '.join(hits)}; estimated harm {score}/10."
        else:
            reason = f"Mock heuristic: routine action with no risk markers; estimated harm {score}/10."
        return score, confidence, cats, reason

    def complete(self, system: str, user: str, max_tokens: int = 800, temperature: float = 0.0) -> str:
        if self.role == "trailing":
            return json.dumps(self._trailing(user))
        score, conf, cats, reason = self.assess(self._focus(user))
        if self.role == "triage":
            return json.dumps({"score": score, "confidence": conf, "reason": reason})
        return json.dumps({"score": score, "reason": reason, "categories": cats})

    def _trailing(self, user: str) -> dict:
        from labwatcher.pipeline import TAXONOMY_IDS  # local import: no cycle at module load
        # Score the evidence only: the "## Categories" listing (and anything after it) names the
        # very keywords we look for, so it is cut off first.
        cut = user.find("\n## Categories")
        low = (user if cut == -1 else user[:cut]).lower()
        scores = {c: 1 for c in TAXONOMY_IDS}
        for pat, bump, cat in _RISK_TERMS:
            n = len(re.findall(pat, low))
            if n:
                scores[cat] = min(10, 3 + bump + min(n, 3))
        if re.search(r"(write_file|append_file).{0,200}" + _PROTECTED_PATH_RE.pattern, low, re.I | re.S):
            scores["record_tampering"] = max(scores["record_tampering"], 8)
        if "blocked by labwatcher" in low:
            scores["scope_overreach"] = max(scores["scope_overreach"], 7)
        top = max(scores, key=lambda c: (scores[c], -TAXONOMY_IDS.index(c)))
        reason = (f"Mock trailing review: highest concern {top} at {scores[top]}/10 based on keyword "
                  "markers in the window.")
        return {"scores": scores, "reason": reason, "top_category": top}


# --- router --------------------------------------------------------------------------

@dataclass
class CallRecord:
    role: str
    provider: str
    model: str
    latency_ms: int
    ok: bool
    retried: bool = False
    error: str | None = None


@dataclass
class ModelRouter:
    """Resolves roles to providers and exposes `complete_json`."""

    settings: Any = None
    calls: list[CallRecord] = field(default_factory=list)

    def __post_init__(self):
        self._providers: dict[str, Any] = {}
        self._modal: dict[str, ModalVLLM] = {}
        self.last: CallRecord | None = None

    # resolution ------------------------------------------------------------------

    def _forced(self) -> str | None:
        forced = os.environ.get(PROVIDER_ENV) or cfg(self.settings, "models.provider")
        return str(forced).lower() if forced else None

    def _modal_for(self, role: str) -> ModalVLLM:
        """Modal client for a role. The role's own settings (models.<role>.base_url / base_url_env /
        model) win; otherwise the shared endpoint defaults (triage -> 7B, everything else -> 14B)."""
        if role not in self._modal:
            endpoint = ROLE_ENDPOINT[role]
            env_name = cfg(self.settings, f"models.{role}.base_url_env") or BASE_URL_ENV[endpoint]
            url = (cfg(self.settings, f"models.{role}.base_url")
                   or os.environ.get(env_name)
                   or os.environ.get(BASE_URL_ENV[endpoint])
                   or DEFAULT_MODAL_URLS[endpoint])
            model = cfg(self.settings, f"models.{role}.model", MODAL_MODELS[role])
            # roles sharing a URL share the client (and its health cache)
            shared = next((m for m in self._modal.values() if m.base_url == url.rstrip("/")
                           and m.model == model), None)
            self._modal[role] = shared or ModalVLLM(url, model)
        return self._modal[role]

    def _anthropic_for(self, role: str) -> AnthropicProvider:
        model = (cfg(self.settings, f"models.{role}.anthropic_model")
                 or (cfg(self.settings, f"models.{role}.model")
                     if str(cfg(self.settings, f"models.{role}.provider", "")).lower() == "anthropic"
                     else None)
                 or ANTHROPIC_MODELS[role])
        return AnthropicProvider(model)

    def provider_for(self, role: str):
        """Provider instance for a role (cached). Order: LABWATCHER_PROVIDER (forced) ->
        settings models.<role>.provider then its `fallback` list (default modal, anthropic, mock);
        modal only when /health answers, anthropic only with ANTHROPIC_API_KEY, mock always."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        if role in self._providers:
            return self._providers[role]
        forced = self._forced()
        if forced:
            order = [forced]
        else:
            order = [str(cfg(self.settings, f"models.{role}.provider", "modal")).lower()]
            fb = cfg(self.settings, f"models.{role}.fallback", ["modal", "anthropic", "mock"])
            order += [str(x).lower() for x in (fb if isinstance(fb, (list, tuple)) else [fb])]
        seen, prov = set(), None
        for name in order:
            if name in seen:
                continue
            seen.add(name)
            if name == "mock":
                prov = MockProvider(role)
            elif name == "anthropic":
                cand = self._anthropic_for(role)
                prov = cand if cand.available() else None
            elif name == "modal":
                cand = self._modal_for(role)
                prov = cand if cand.healthy() else None
            if prov is not None:
                break
        if prov is None:
            prov = MockProvider(role)
        self._providers[role] = prov
        return prov

    def reset(self):
        """Forget cached providers and health checks (tests / UI 'reconnect')."""
        self._providers.clear()
        self._modal.clear()

    def describe(self) -> dict[str, dict]:
        return {r: {"provider": p.name, "model": getattr(p, "model", p.name)}
                for r, p in ((r, self.provider_for(r)) for r in ROLES)}

    # completion ------------------------------------------------------------------

    def complete_json(self, role: str, system: str, user: str, schema_hint: str | None = None) -> dict:
        prov = self.provider_for(role)
        hint = f"\n\nReturn ONLY a JSON object{(' with this shape: ' + schema_hint) if schema_hint else ''}."
        sys_prompt = system + hint
        t0 = time.monotonic()
        retried, err = False, None
        try:
            text = prov.complete(sys_prompt, user)
            try:
                out = extract_json(text)
            except ValueError as e:
                retried = True
                err = str(e)
                text = prov.complete(sys_prompt, user + "\n\nYour previous reply was not valid JSON. "
                                     "Reply with the JSON object only, no prose.")
                out = extract_json(text)
            rec = CallRecord(role, prov.name, getattr(prov, "model", prov.name),
                             int((time.monotonic() - t0) * 1000), True, retried, err)
        except Exception as e:  # noqa: BLE001
            rec = CallRecord(role, prov.name, getattr(prov, "model", prov.name),
                             int((time.monotonic() - t0) * 1000), False, retried, str(e))
            self.calls.append(rec)
            self.last = rec
            raise
        self.calls.append(rec)
        self.last = rec
        out["_provider"] = prov.name
        out["_latency_ms"] = rec.latency_ms
        return out
