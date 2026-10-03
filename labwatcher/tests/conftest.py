"""pytest configuration: make the repo root and drug_discovery importable (labsim lives there)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "drug_discovery"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
