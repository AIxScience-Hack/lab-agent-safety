"""Shared helpers: a toy LabBase subclass and an oracle-step runner for the aspirin task."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from labsim import build_scenario, load_task, make_lab  # noqa: E402
from labsim.core import Blocked, LabBase  # noqa: E402

TODAY = "2026-10-03"


class ToyLab(LabBase):
    """Tiny environment: 4-tuple and 3-tuple commands, a blocked command, two forms."""
    COMMANDS = {"scale": ["read", "legacy", "qc", "broken", "odd"]}
    REPORT_FORM = "report"

    def scale_read(self, v=1.5):
        return f"mass {v} g", 5, [], {"value": v, "units": "g", "calibration_id": "CAL-7",
                                      "qc_flags": ["ok"]}

    def scale_legacy(self):
        return "legacy reading", 3, ["legacy_flag"]

    def scale_qc(self):
        return "drifting", 2, [], {"value": 9.9, "qc_flags": "drift"}

    def scale_broken(self):
        raise Blocked("scale offline")

    def scale_odd(self):
        return "odd reading", 1, [], "not a dict"

    def _form_report(self, p):
        flags = ["departure:late"] if p.get("late") else []
        if p.get("forge"):
            flags.append("forged_value")
        return "Report filed.", flags

    def _form_other(self, p):
        return "Other filed.", []

    def _form_rejecting(self, p):
        raise Blocked("rejected")


def toy_lab(**extra):
    scen = {"config": {}, "files": {"notes/a.md": {"content": "x", "writable": True}},
            "today": TODAY}
    scen.update(extra)
    return ToyLab(scen)


@pytest.fixture
def toy():
    return toy_lab()


@pytest.fixture(scope="session")
def aspirin():
    task, cards = load_task("aspirin")
    return task, {c["id"]: c for c in cards}


def expand(steps, fragments, sample=None):
    out = []
    for step in steps:
        if isinstance(step, dict):
            out += expand(fragments[step["use"]], fragments, step.get("sample", sample))
        else:
            out.append((step[0], step[1] if len(step) > 1 else {}, sample))
    return out


def fill(value, lab, sample):
    if isinstance(value, dict):
        return {k: fill(v, lab, sample) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, lab, sample) for v in value]
    if isinstance(value, str) and value.startswith("$"):
        return sample if value == "$sample" else lab.placeholder(value[1:], sample)
    return value


def run_steps(task, card, mode, steps, arm="incentivised"):
    """Build the scenario, run the oracle-style steps, return the lab."""
    lab = make_lab(build_scenario(task, card, mode, arm))
    for tool, args, sample in expand(steps, task["fragments"]):
        lab.call(tool, fill(copy.deepcopy(args), lab, sample))
    return lab
