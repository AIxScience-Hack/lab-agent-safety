"""Run the practice suite through the lab suite's own Inspect harness, unchanged.

    inspect eval dev_suite/run.py -T env=cycle_count --model <provider/model>
    python -m harness.run --bench practice --model <provider/model> --organism prompts/<file>.txt

Same agent loop, tools, awareness question and scorer as drug_discovery/inspect_tasks.py.
The lab harness finds tasks and environments by name, so this module registers the
practice ones next to the lab ones. Lab environments keep working in the same process.
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent, ROOT.parent / "drug_discovery"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import inspect_tasks as lab_harness  # noqa: E402  (drug_discovery/inspect_tasks.py)
import yaml  # noqa: E402
from inspect_ai import task  # noqa: E402

ENVS = sorted(p.parent.name for p in (ROOT / "tasks").glob("*/task.yaml"))


def _load_task(name, _lab_load=lab_harness.load_task):
    """Practice environments load from dev_suite/tasks; everything else as before."""
    if name not in ENVS:
        return _lab_load(name)
    root = ROOT / "tasks" / name
    cards = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((root / "honeypots").glob("*.yaml"))]
    return yaml.safe_load((root / "task.yaml").read_text(encoding="utf-8")), cards


lab_harness.load_task = _load_task
for _env in ENVS:      # the lab core imports an environment as labsim.envs.<env>
    sys.modules[f"labsim.envs.{_env}"] = importlib.import_module(f"dev_suite.envs.{_env}")


@task
def practice(env: str = "cycle_count", conditions: str | None = None, arms: str | None = None,
             honeypots: str | None = None, max_tool_calls: int = lab_harness.DEFAULT_MAX_TOOL_CALLS,
             ask_awareness: bool = True):
    """env: cycle_count, nightly_load or restore_check. Other options as in the lab harness."""
    if env not in ENVS:
        raise ValueError(f"unknown practice env {env!r}; expected one of {ENVS}")
    return lab_harness._make_task(env, conditions, arms, honeypots, max_tool_calls, ask_awareness)
