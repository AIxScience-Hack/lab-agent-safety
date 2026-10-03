"""Check every case file in cases/ against the schema in RULES.md."""
import sys
from collections import Counter
from pathlib import Path

import yaml

REQUIRED = ["id", "title", "category", "blocker_type", "task", "context", "blocker",
            "reward", "honeypot", "tools", "hack_check", "control_fix"]
CATEGORIES = {"FAB", "TAMPER", "LEAK", "SELECT", "SKIP", "BYPASS", "PROXY", "SPECIAL"}
BLOCKERS = {"infeasible", "unsafe"}

cases, errors = [], []
for path in sorted(Path(__file__).parent.glob("cases/*.yaml")):
    for c in yaml.safe_load(path.read_text()):
        cid = c.get("id", f"<no id in {path.name}>")
        errors += [f"{cid}: missing {k}" for k in REQUIRED if k not in c]
        if c.get("category") not in CATEGORIES:
            errors.append(f"{cid}: bad category {c.get('category')}")
        if c.get("blocker_type") not in BLOCKERS:
            errors.append(f"{cid}: bad blocker_type {c.get('blocker_type')}")
        tools = c.get("tools", [])
        if "report_blocker" not in tools:
            errors.append(f"{cid}: no report_blocker tool (safe path)")
        if c.get("hack_check", {}).get("tool") not in tools:
            errors.append(f"{cid}: hack_check tool not in tools")
        cases.append(c)

ids = Counter(c.get("id") for c in cases)
errors += [f"duplicate id {i}" for i, n in ids.items() if n > 1]

print(f"{len(cases)} cases")
print("by category:", dict(Counter(c.get("category") for c in cases)))
print("by blocker:", dict(Counter(c.get("blocker_type") for c in cases)))
for e in errors:
    print("ERROR", e)
sys.exit(1 if errors else 0)
