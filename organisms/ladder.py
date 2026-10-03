"""Run the prompted-organism ladder: one harness.run per rung, same options for each.

    python -m organisms.ladder --bench practice --model <provider/model> --seeds 0
    python -m organisms.ladder --freeze
    python -m organisms.ladder --bench drug_discovery,labbench --model <provider/model> --seeds 0,1,2
    python -m harness.results logs/ --by organism,condition

Each seed is one pass over the samples with that sampling seed, recorded in the log, so
a later run with new seeds adds independent repeats. Sampling defaults are the Qwen3.8-27B
model card's thinking-mode settings (temperature 1.0, top_p 0.95; the server adds top_k 20).

Rungs are listed in ladder.yaml. Wording is tuned on the practice suite only. A lab
suite will not run until the prompts are frozen (--freeze records a hash of each file)
and still match, so the benchmark is never run against prompts that were adjusted
after seeing its results.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

from harness import run as harness_run

HERE = Path(__file__).resolve().parent
FROZEN = HERE / "frozen.json"
SETS = ("main", "paraphrase", "ablation", "integrity")


def load(directory=HERE):
    rungs = yaml.safe_load((directory / "ladder.yaml").read_text(encoding="utf-8"))["rungs"]
    for r in rungs:
        r["path"] = directory / "prompts" / r["file"] if r.get("file") else None
    return rungs


def text_of(path):
    """Exactly what the model is sent."""
    return path.read_text(encoding="utf-8").strip()


def digests(rungs):
    return {r["name"]: hashlib.sha256(text_of(r["path"]).encode("utf-8")).hexdigest()
            for r in rungs if r["path"]}


def check_frozen(rungs, frozen_file=None):
    """Exit unless every selected prompt matches the frozen hashes."""
    frozen_file = frozen_file or FROZEN
    if not frozen_file.exists():
        raise SystemExit("Prompts are not frozen. Pilot on the practice suite, then run "
                         "`python -m organisms.ladder --freeze` before a lab suite.")
    frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
    changed = [n for n, d in digests(rungs).items() if frozen.get(n) != d]
    if changed:
        raise SystemExit(f"Prompts changed since they were frozen: {changed}. Lab results must come "
                         "from frozen prompts; restore the files or re-freeze deliberately.")


def select(rungs, sets, names):
    if names:
        unknown = [n for n in names if n not in {r["name"] for r in rungs}]
        if unknown:
            raise SystemExit(f"unknown rungs {unknown}")
        return [r for r in rungs if r["name"] in names]
    return [r for r in rungs if r["set"] in sets]


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m organisms.ladder", description=__doc__.split("\n\n")[0])
    p.add_argument("--bench", default="practice", help="practice, drug_discovery, labbench (comma-separated)")
    p.add_argument("--model", help="Inspect model")
    p.add_argument("--set", default="main", help=f"comma-separated from {', '.join(SETS)}, or all")
    p.add_argument("--rungs", help="comma-separated rung names; overrides --set")
    p.add_argument("--conditions", default="control,blocked")
    p.add_argument("--arms", default="incentivised")
    p.add_argument("--envs")
    p.add_argument("--cards")
    p.add_argument("--seeds", default="0", help="comma-separated integers; one pass per seed")
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--limit")
    p.add_argument("--max-connections", type=int, default=32)
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--freeze", action="store_true", help="record the hash of every prompt file and stop")
    args = p.parse_args(argv)

    rungs = load()
    if args.freeze:
        FROZEN.write_text(json.dumps(digests(rungs), indent=2) + "\n", encoding="utf-8")
        print(f"Froze {len(digests(rungs))} prompts in {FROZEN.relative_to(HERE.parent)}")
        return 0
    if not args.model:
        p.error("--model is required")

    sets = SETS if args.set == "all" else [s.strip() for s in args.set.split(",")]
    chosen = select(rungs, sets, [n.strip() for n in args.rungs.split(",")] if args.rungs else None)
    benches = [b.strip() for b in args.bench.split(",")]
    if any(b != "practice" for b in benches):
        check_frozen(chosen)

    failed = 0
    for r in chosen:
        cmd = ["--bench", args.bench, "--model", args.model, "--conditions", args.conditions,
               "--arms", args.arms, "--seeds", args.seeds, "--log-dir", args.log_dir,
               "--temperature", str(args.temperature), "--top-p", str(args.top_p),
               "--max-tokens", str(args.max_tokens), "--timeout", str(args.timeout),
               "--max-connections", str(args.max_connections)]
        for flag, value in (("--envs", args.envs), ("--cards", args.cards), ("--limit", args.limit)):
            if value:
                cmd += [flag, str(value)]
        if r["path"]:
            cmd += ["--organism", str(r["path"])]
        if args.dry_run:
            cmd.append("--dry-run")
        print(f"\n== rung {r['name']}: {r['role']}", flush=True)
        failed += harness_run.main(cmd) != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
