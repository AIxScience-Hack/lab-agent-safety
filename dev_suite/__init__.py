"""Non-lab practice suite (workstream B, task B0).

Three small operations environments written to the same card format as the lab
suite in drug_discovery/: same tools, same conditions, same scoring. Used to
smoke-test the runner, to check an organism has the trait outside the lab domain,
and as the source of non-lab trajectories. Never part of the benchmark itself.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_LABSIM = ROOT.parent / "drug_discovery"
if str(_LABSIM) not in sys.path:
    sys.path.insert(0, str(_LABSIM))
