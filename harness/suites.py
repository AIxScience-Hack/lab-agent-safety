"""Find and build scenarios from either suite.

`lab` is the benchmark in drug_discovery/. `dev` is the non-lab practice suite in
dev_suite/. Both use the same card format, tools, conditions and scoring.
"""
import importlib
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "drug_discovery"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from labsim import core  # noqa: E402
from labsim.core import TOOLS, build_scenario, modes_for  # noqa: E402,F401
from labsim.scoring import score  # noqa: E402,F401

SUITES = {
    "lab": {"tasks": ROOT / "drug_discovery" / "tasks", "envs": "labsim.envs"},
    "dev": {"tasks": ROOT / "dev_suite" / "tasks", "envs": "dev_suite.envs"},
}


def task_names(suite):
    return sorted(p.name for p in SUITES[suite]["tasks"].iterdir() if (p / "task.yaml").exists())


def load_task(suite, name):
    """`name` may carry a variant suffix (<env>_warned, <env>_pushback; labsim/core.py)."""
    return core.load_task(name, SUITES[suite]["tasks"])


def make_lab(suite, scenario):
    module = importlib.import_module(f"{SUITES[suite]['envs']}.{scenario['env']}")
    return module.Lab(scenario)
