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

from labsim.core import TOOLS, build_scenario, modes_for  # noqa: E402,F401
from labsim.scoring import score  # noqa: E402,F401

SUITES = {
    "lab": {"tasks": ROOT / "drug_discovery" / "tasks", "envs": "labsim.envs"},
    "dev": {"tasks": ROOT / "dev_suite" / "tasks", "envs": "dev_suite.envs"},
}
# A practice task named <env>_pushback is <env> with the supervisor answering every
# escalation with the task's pushback_line. Same cards, same conditions.
PUSHBACK = "_pushback"


def task_names(suite):
    return sorted(p.name for p in SUITES[suite]["tasks"].iterdir() if (p / "task.yaml").exists())


def load_task(suite, name):
    pushback = suite == "dev" and name.endswith(PUSHBACK)
    root = SUITES[suite]["tasks"] / (name[:-len(PUSHBACK)] if pushback else name)
    task = yaml.safe_load((root / "task.yaml").read_text(encoding="utf-8"))
    honeypots = [yaml.safe_load(p.read_text(encoding="utf-8"))
                 for p in sorted((root / "honeypots").glob("*.yaml"))]
    if pushback:
        task["config"]["supervisor_reply"] = task["pushback_line"]
    return task, honeypots


def make_lab(suite, scenario):
    module = importlib.import_module(f"{SUITES[suite]['envs']}.{scenario['env']}")
    return module.Lab(scenario)
