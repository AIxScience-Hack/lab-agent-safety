"""One results table from the Inspect logs of every suite.

    python -m harness.results logs/                    # summary table
    python -m harness.results logs/ --csv runs.csv     # plus one row per run

Rows use the shared vocabulary in harness/benchmarks.py, with each suite's own
outcome label kept alongside. The organism is the run's `--metadata organism=<name>`
(set by harness.run --organism); without it, a short hash of the run's system
message, or `baseline` for the raw model.

    python -m harness.results logs/ --by organism,condition     # one row per rung and condition
"""
import argparse
import csv
import hashlib
import math
import sys
from collections import Counter, defaultdict

from inspect_ai.log import list_eval_logs, read_eval_log

from .benchmarks import HACKS, benchmark_of, unify

FIELDS = ("benchmark", "env", "card", "condition", "arm", "model", "organism", "seed", "epoch", "outcome",
          "native_outcome", "hack", "covert", "attempted", "disclosed", "score",
          "awareness_followup", "awareness_answer", "log")


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    centre, half = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def organism_of(log):
    name = (log.eval.metadata or {}).get("organism")
    if name:
        return str(name)
    prompt_file = (log.eval.task_args or {}).get("organism_prompt")       # labbench's own hook
    if prompt_file:
        return str(prompt_file).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    system = getattr(log.plan.config, "system_message", None) if log.plan else None
    return "sys:" + hashlib.sha1(system.encode()).hexdigest()[:8] if system else "baseline"


def rows(log_dir):
    """One dict per scored sample, across every log under log_dir."""
    for info in list_eval_logs(log_dir):
        log = read_eval_log(info)
        organism = organism_of(log)
        seed = (log.eval.metadata or {}).get("seed", getattr(log.plan.config, "seed", None) if log.plan else None)
        for s in log.samples or []:
            sc = (s.scores or {}).get("lab_score")
            if sc is None:
                continue
            meta = sc.metadata or {}
            bench = benchmark_of(log.eval.task, s.metadata)
            env, card, condition, arm, outcome = unify(bench, s.metadata, sc.answer, meta)
            ea = (s.scores or {}).get("eval_awareness")                    # labbench scores it
            followup = ea.value.get("followup") if ea and isinstance(ea.value, dict) else ""
            answer = (ea.answer if ea else s.metadata.get("eval_awareness_answer")) or ""
            yield {"benchmark": bench, "env": env, "card": card, "condition": condition, "arm": arm,
                   "model": log.eval.model, "organism": organism,
                   "seed": "" if seed is None else seed, "epoch": s.epoch,
                   "outcome": outcome, "native_outcome": sc.answer,
                   "hack": int(outcome in HACKS), "covert": int(outcome == "covert_hack"),
                   "attempted": int(bool(meta.get("attempted"))), "disclosed": int(bool(meta.get("disclosed"))),
                   "score": sc.value, "awareness_followup": followup,
                   "awareness_answer": " ".join(str(answer).split())[:200], "log": info.name.rsplit("/", 1)[-1]}


def summary(all_rows, by=("benchmark", "model", "organism", "env", "card", "condition", "arm")):
    """`hack` is the share of runs that took the shortcut (covert or disclosed); `tried`
    also counts runs whose attempt was rejected or blocked."""
    def shown(column, value):         # "provider/org/Model:host" -> "Model"
        return str(value).rsplit("/", 1)[-1].split(":")[0] if column == "model" else str(value)

    cells, tried = defaultdict(Counter), Counter()
    for r in all_rows:
        key = tuple(shown(k, r[k]) for k in by)
        cells[key][r["outcome"]] += 1
        tried[key] += int(r["attempted"] or r["hack"])
    width = [max(len(h), *(len(key[i]) for key in cells)) for i, h in enumerate(by)]
    lines = [" ".join(f"{h:{w}}" for h, w in zip(by, width))
             + f" {'n':>3} {'hack':>5} {'95% CI':>13} {'tried':>5}  outcomes"]
    for key in sorted(cells):
        c = cells[key]
        n, k = sum(c.values()), sum(c[o] for o in HACKS)
        lo, hi = wilson(k, n)
        lines.append(" ".join(f"{v:{w}}" for v, w in zip(key, width))
                     + f" {n:3d} {k / n:5.2f} [{lo:.2f}, {hi:.2f}] {tried[key] / n:5.2f}  "
                     + ", ".join(f"{o} {v}" for o, v in c.most_common()))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m harness.results", description=__doc__.split("\n\n")[0])
    p.add_argument("log_dir", nargs="?", default="logs")
    p.add_argument("--csv", help="also write one row per run to this file")
    p.add_argument("--by", default="benchmark,model,organism,env,card,condition,arm",
                   help="comma-separated columns to group the summary by")
    args = p.parse_args(argv)
    by = tuple(c.strip() for c in args.by.split(","))
    unknown = [c for c in by if c not in FIELDS]
    if unknown:
        p.error(f"unknown columns {unknown}; choose from {', '.join(FIELDS)}")
    all_rows = list(rows(args.log_dir))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(all_rows)
    print(summary(all_rows, by) if all_rows else f"no scored samples under {args.log_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
