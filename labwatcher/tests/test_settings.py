"""Settings: layering, lock semantics, additions_allowed, validation, views."""
import textwrap

import pytest
import yaml

from labwatcher.settings import Settings, BUILTIN_PATH, KNOWN_TOOLS, TOOL_MODES
from labwatcher.rules import TAXONOMY


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


def test_builtin_defaults_match_spec():
    s = Settings.load()
    assert s.errors == [] and s.warnings == []
    for tool in ("list_files", "read_file", "report_issue"):
        assert s.tools[tool]["mode"] == "auto_approve"
    for tool in ("instrument", "write_file", "append_file", "submit", "finish"):
        assert s.tools[tool] == {"mode": "escalate", "escalate_at": 6, "deny_at": None}
    assert s.tools["submit_report"] == {"mode": "escalate", "escalate_at": 6, "deny_at": 8}
    assert s.triage["confidence_to_resolve"] == 0.8
    assert s.evaluator["context_messages"] == 40
    assert s.trailing["every_n_actions"] == 5 and s.trailing["window"] == 20
    assert s.suggestions["threshold"] == 7 and "{message}" in s.suggestions["template"]
    assert s.human["auto"] == "approve"
    for role in ("triage", "evaluator", "trailing", "agent"):
        assert s.models[role]["provider"] == "modal"
        assert s.models[role]["fallback"] == ["modal", "anthropic", "mock"]
        assert {"provider", "model", "base_url_env"} <= set(s.models[role])
    assert s.models["triage"]["base_url_env"] == "LABWATCHER_TRIAGE_URL"
    assert s.models["evaluator"]["base_url_env"] == "LABWATCHER_EVALUATOR_URL"
    assert set(s.contexts) == {"drug_discovery", "materials_discovery"}
    assert s.contexts["drug_discovery"]["envs"] == ["aspirin", "cell_culture", "cytotox"]
    assert s.contexts["materials_discovery"]["envs"] == ["coin_cell"]
    for ctx in s.contexts.values():
        assert ctx["rules"] and ctx["policy"] and ctx["label"]
    assert s.context_path("drug_discovery", "rules").exists()
    assert s.taxonomy_ids == list(TAXONOMY)
    assert s.ui["port"] == 8787


def test_tools_view_is_mapping_and_unknown_raises():
    s = Settings.load()
    assert set(s.tools) <= set(KNOWN_TOOLS)
    assert len(s.tools) == 9
    with pytest.raises(KeyError):
        s.tools["bash"]
    assert s.tools.get("bash") is None


def test_org_layer_overrides_modifiable_fields(tmp_path):
    org = write(tmp_path, "org.yaml", """
        tools:
          instrument: {mode: escalate, escalate_at: 5, deny_at: 9}
        triage: {confidence_to_resolve: 0.9}
        human: {auto: deny}
    """)
    s = Settings.load(org_path=org)
    assert s.errors == []
    assert s.tools["instrument"] == {"mode": "escalate", "escalate_at": 5, "deny_at": 9}
    assert s.triage["confidence_to_resolve"] == 0.9
    assert s.triage["context_messages"] == 10          # untouched sibling survives the merge
    assert s.human["auto"] == "deny"
    rows = {r["key"]: r for r in s.effective()}
    assert rows["tools.instrument.escalate_at"]["source"] == "org"
    assert rows["tools.read_file.mode"]["source"] == "builtin"


def test_locked_field_ignores_lower_layers_and_warns(tmp_path):
    org = write(tmp_path, "org.yaml", """
        tools:
          finish: {mode: always_escalate}
        permissions:
          tools.finish: locked
          human: locked
    """)
    user = write(tmp_path, "user.yaml", """
        tools:
          finish: {mode: escalate, escalate_at: 9}
          instrument: {mode: escalate, escalate_at: 7}
        human: {auto: deny}
    """)
    s = Settings.load(org_path=org, user_path=user)
    assert s.errors == []
    assert s.tools["finish"]["mode"] == "always_escalate"          # org wins, user ignored
    assert s.tools["instrument"]["escalate_at"] == 7                # modifiable sibling applied
    assert s.human["auto"] == "approve"                            # locked by org
    assert any("tools.finish" in w and "locked" in w for w in s.warnings)
    assert any("'human' is locked" in w for w in s.warnings)
    assert s.permission_for("tools.finish") == "locked"
    assert s.permission_for("tools.finish.mode") == "locked"      # inherited by children
    assert s.permission_for("tools.instrument") == "modifiable"
    eff = {r["key"]: r for r in s.effective()}
    assert eff["human.auto"]["locked"] is True
    assert eff["tools.instrument.escalate_at"]["locked"] is False


def test_builtin_locked_taxonomy_cannot_be_changed_or_unlocked(tmp_path):
    org = write(tmp_path, "org.yaml", """
        taxonomy:
          - {id: made_up, label: nope}
        version: 2
        permissions:
          taxonomy: modifiable
    """)
    s = Settings.load(org_path=org)
    assert s.errors == []
    assert s.taxonomy_ids == list(TAXONOMY)
    assert s.data["version"] == 1
    assert any("cannot loosen locked permission for 'taxonomy'" in w for w in s.warnings)
    assert any("'taxonomy' is locked" in w for w in s.warnings)


def test_additions_allowed_adds_but_does_not_change(tmp_path):
    # contexts is additions_allowed by default: a new context is fine, editing an existing one is not.
    org = write(tmp_path, "org.yaml", """
        contexts:
          drug_discovery:
            envs: [aspirin, cell_culture, cytotox, new_env]   # list append is an addition
            label: Renamed                                     # scalar change is not
            extra_key: 1                                       # new key is an addition
          bio_foundry:
            label: Bio foundry
            rules: rules/bio_foundry.yaml
            policy: policies/bio_foundry.yaml
            envs: [fermenter]
        permissions:
          models: additions_allowed
    """)
    user = write(tmp_path, "user.yaml", """
        models:
          triage: {provider: mock}            # existing -> ignored
          judge: {provider: mock, model: m}   # new role -> added (unknown role warning)
    """)
    s = Settings.load(org_path=org, user_path=user)
    dd = s.contexts["drug_discovery"]
    assert dd["envs"] == ["aspirin", "cell_culture", "cytotox", "new_env"]
    assert dd["label"] == "Drug discovery"
    assert dd["extra_key"] == 1
    assert "bio_foundry" in s.contexts
    assert s.models["triage"]["provider"] == "modal"
    assert s.models["judge"]["provider"] == "mock"
    assert any("contexts.drug_discovery.label" in w and "additions only" in w for w in s.warnings)
    assert any("models.triage.provider" in w and "additions only" in w for w in s.warnings)
    assert any("models.judge: unknown role" in w for w in s.warnings)
    assert s.errors == []


def test_user_permissions_are_ignored(tmp_path):
    user = write(tmp_path, "user.yaml", """
        permissions: {tools: locked}
        tools: {finish: {mode: escalate, escalate_at: 8}}
    """)
    s = Settings.load(user_path=user)
    assert s.tools["finish"]["escalate_at"] == 8
    assert any("user layer: 'permissions' ignored" in w for w in s.warnings)


def test_validation_collects_errors_instead_of_raising(tmp_path):
    org = write(tmp_path, "org.yaml", """
        tools:
          bash: {mode: escalate, escalate_at: 6}
          instrument: {mode: panic, escalate_at: 11}
          submit: {mode: escalate, escalate_at: 6, deny_at: "8"}
          finish: {mode: escalate}             # merges over escalate_at: 6 -> still valid
        triage: {confidence_to_resolve: 1.5}
        evaluator: {context_messages: 0}
        trailing: {every_n_actions: -1, enabled: "yes"}
        suggestions: {threshold: 0}
        human: {auto: maybe}
        models:
          triage: {provider: openai, model: ""}
          agent: {provider: modal, model: x, fallback: [modal, groq]}
        contexts:
          materials_discovery: {envs: []}
    """)
    s = Settings.load(org_path=org)
    joined = "\n".join(s.errors)
    for frag in ("tools.bash: unknown tool", "tools.instrument.mode", "tools.instrument.escalate_at",
                 "tools.submit.deny_at", "triage.confidence_to_resolve", "evaluator.context_messages",
                 "trailing.every_n_actions", "trailing.enabled", "suggestions.threshold",
                 "human.auto", "models.triage.provider", "models.triage.model",
                 "models.agent.fallback: unknown provider 'groq'"):
        assert frag in joined, frag
    # contexts is additions_allowed, so envs: [] was ignored (warning) rather than breaking the context
    assert s.contexts["materials_discovery"]["envs"] == ["coin_cell"]
    assert not s.ok
    assert s.tools["instrument"]["escalate_at"] == 11       # data kept so the UI can show what is wrong
    assert s.tools["finish"] == {"mode": "escalate", "escalate_at": 6, "deny_at": None}
    d = Settings.load().to_dict()
    d["tools"]["finish"] = {"mode": "escalate"}
    assert any("tools.finish: mode 'escalate' needs" in e for e in Settings.from_dict(d).errors)


def test_bad_yaml_and_missing_files(tmp_path):
    bad = write(tmp_path, "bad.yaml", "tools: [this is: not: valid\n")
    s = Settings.load(org_path=bad, user_path=tmp_path / "missing.yaml")
    assert any("YAML parse error" in e for e in s.errors)
    assert any("user settings file not found" in e for e in s.errors)
    assert s.tools["instrument"]["escalate_at"] == 6       # built-in layer still in force
    scalar = write(tmp_path, "scalar.yaml", "just a string\n")
    s2 = Settings.load(org_path=scalar)
    assert any("top level must be a mapping" in e for e in s2.errors)


def test_env_paths_used_when_not_given(tmp_path, monkeypatch):
    org = write(tmp_path, "org.yaml", "ui: {theme: dark}\n")
    monkeypatch.setenv("LABWATCHER_ORG_SETTINGS", str(org))
    monkeypatch.setenv("LABWATCHER_USER_SETTINGS", str(tmp_path / "nope.yaml"))
    s = Settings.load()
    assert s.ui["theme"] == "dark"
    assert any("user settings file not found" in w for w in s.warnings)   # env-derived -> warning
    assert s.errors == []


def test_to_dict_and_effective_roundtrip(tmp_path):
    s = Settings.load()
    d = s.to_dict()
    assert d["tools"]["submit_report"]["deny_at"] == 8
    assert d["permissions"]["taxonomy"] == "locked"
    d["tools"]["instrument"]["escalate_at"] = 1
    assert s.tools["instrument"]["escalate_at"] == 6       # deep copy
    rows = s.effective()
    keys = {r["key"] for r in rows}
    assert {"tools.instrument.escalate_at", "models.triage.provider", "triage.confidence_to_resolve"} <= keys
    assert all({"key", "value", "source", "permission", "locked"} <= set(r) for r in rows)
    # the Settings page can rebuild a working Settings from to_dict()
    again = Settings.from_dict(d)
    assert again.errors == [] and again.tools["submit_report"]["deny_at"] == 8
    assert yaml.safe_load(BUILTIN_PATH.read_text())["tools"]["finish"]["escalate_at"] == 6
    assert set(TOOL_MODES) == {"auto_approve", "escalate", "always_escalate"}


def test_package_exports_are_lazy():
    """`import labwatcher` must not import sibling modules; attributes resolve on first access."""
    import importlib
    import sys
    for m in [m for m in list(sys.modules) if m == "labwatcher" or m.startswith("labwatcher.")]:
        del sys.modules[m]
    pkg = importlib.import_module("labwatcher")
    assert "labwatcher.pipeline" not in sys.modules and "labwatcher.store" not in sys.modules
    assert set(pkg.__all__) >= {"Watcher", "Settings", "Decision", "Action", "Store"}
    assert pkg.Settings is importlib.import_module("labwatcher.settings").Settings
    assert pkg.Store is importlib.import_module("labwatcher.store").Store
    assert pkg.RuleEngine is importlib.import_module("labwatcher.rules").RuleEngine
    assert "Settings" in dir(pkg) and "Store" in dir(pkg)
    with pytest.raises(AttributeError):
        pkg.NotAThing
    # a missing sibling module only fails when its export is touched, not at package import
    real = importlib.import_module
    def fake(name, *a, **k):
        if name == "labwatcher.pipeline":
            raise ModuleNotFoundError("No module named 'labwatcher.pipeline'")
        return real(name, *a, **k)
    importlib.import_module = fake
    try:
        pkg.__dict__.pop("Watcher", None)
        with pytest.raises(ModuleNotFoundError):
            pkg.Watcher
    finally:
        importlib.import_module = real
