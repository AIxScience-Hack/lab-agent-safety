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
from inspect_ai import task  # noqa: E402

from harness.benchmarks import VARIANTS  # noqa: E402
from harness.suites import load_task, task_names  # noqa: E402

BASE_ENVS = task_names("dev")
ENVS = BASE_ENVS + [e + suffix for suffix in VARIANTS.values() for e in BASE_ENVS]


def _load_task(name, _lab_load=lab_harness.load_task):
    """Practice environments load from dev_suite/tasks; everything else as before."""
    return load_task("dev", name) if name in ENVS else _lab_load(name)


lab_harness.load_task = _load_task
for _env in BASE_ENVS:      # the lab core imports an environment as labsim.envs.<env>
    sys.modules[f"labsim.envs.{_env}"] = importlib.import_module(f"dev_suite.envs.{_env}")


@task
def practice(env: str = "cycle_count", conditions: str | None = None, arms: str | None = None,
             honeypots: str | None = None, max_tool_calls: int = lab_harness.DEFAULT_MAX_TOOL_CALLS,
             ask_awareness: bool = True):
    """env: cycle_count, nightly_load or restore_check, or one of them with _warned (the
    SOP names each shortcut as prohibited) or _pushback (the supervisor answers an
    escalation with pressure). Other options as in the lab harness."""
    if env not in ENVS:
        raise ValueError(f"unknown practice env {env!r}; expected one of {ENVS}")
    return lab_harness._make_task(env, conditions, arms, honeypots, max_tool_calls, ask_awareness)
