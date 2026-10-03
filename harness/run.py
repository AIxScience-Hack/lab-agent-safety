"""Run any suite with the same options.

    python -m harness.run --bench all --model <provider/model> --epochs 5
    python -m harness.run --bench drug_discovery --envs aspirin --cards a01,a05 \
        --conditions control,blocked --arms incentivised --model <provider/model>
    python -m harness.run --bench labbench --organism prompts/metric.txt --model <provider/model>
    python -m harness.run --bench practice --model mockllm/model --limit 1 --dry-run

One `inspect eval` per environment, launched with this interpreter from the repo
root. --organism is a text file used as the system message for every model call
(a prompted organism); its file name is recorded as the run's organism.
Anything after `--` goes to `inspect eval` unchanged.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from .benchmarks import ARMS, BENCHMARKS, CONDITIONS, ROOT


def _list(value):
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def commands(args, extra=()):
    """The inspect eval command lines (argv lists) for these options."""
    names = list(BENCHMARKS) if args.bench == "all" else _list(args.bench)
    conditions, arms = _list(args.conditions), _list(args.arms)
    for value, allowed, what in ((conditions, CONDITIONS, "condition"), (arms, ARMS, "arm")):
        unknown = [v for v in value if v not in allowed]
        if unknown:
            raise SystemExit(f"unknown {what} {unknown}; expected some of {list(allowed)}")
    system = Path(args.organism).read_text(encoding="utf-8").strip() if args.organism else None
    out = []
    for name in names:
        if name not in BENCHMARKS:
            raise SystemExit(f"unknown benchmark {name!r}; expected one of {list(BENCHMARKS)} or all")
        bench = BENCHMARKS[name]
        native = [bench.conditions[c] for c in conditions if c in bench.conditions]
        skipped = [c for c in conditions if c not in bench.conditions]
        if skipped:
            print(f"note: {name} has no {skipped} condition; skipped", file=sys.stderr)
        if conditions and not native:
            continue
        for env in _list(args.envs) or bench.envs:
            if env not in bench.envs:
                continue
            task_args = dict(bench.defaults)
            if bench.env_arg:
                task_args[bench.env_arg] = env
            if native:
                task_args[bench.condition_arg] = ",".join(native)
            if arms:
                task_args[bench.arm_arg] = ",".join(arms)
            if args.cards:
                task_args[bench.card_arg] = args.cards
            task_args.update(kv.split("=", 1) for kv in args.task_arg)
            cmd = [sys.executable, "-m", "inspect_ai", "eval", bench.task, "--model", args.model,
                   "--log-dir", args.log_dir, "--metadata", f"benchmark={name}"]
            for k, v in task_args.items():
                cmd += ["-T", f"{k}={v}"]
            if system:
                cmd += ["--system-message", system, "--metadata", f"organism={Path(args.organism).stem}"]
            for flag, value in (("--epochs", args.epochs), ("--limit", args.limit),
                                ("--max-connections", args.max_connections)):
                if value:
                    cmd += [flag, str(value)]
            out.append(cmd + list(extra))
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1:]
    p = argparse.ArgumentParser(prog="python -m harness.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--bench", default="all", help=f"{', '.join(BENCHMARKS)} or all (comma-separated)")
    p.add_argument("--model", required=True, help="Inspect model, e.g. anthropic/claude-haiku-4-5-20251001")
    p.add_argument("--envs", help="comma-separated environments; default all of the suite's")
    p.add_argument("--cards", help="comma-separated card IDs in the suite's own form (a01,a05 or 3,5)")
    p.add_argument("--conditions", help=f"comma-separated from {', '.join(CONDITIONS)}; default all")
    p.add_argument("--arms", default=",".join(ARMS), help=f"comma-separated from {', '.join(ARMS)}; default both")
    p.add_argument("--organism", help="text file used as the system message (prompted organism)")
    p.add_argument("--epochs", type=int)
    p.add_argument("--limit", help="passed to inspect eval --limit")
    p.add_argument("--max-connections", type=int)
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--task-arg", action="append", default=[], metavar="KEY=VALUE",
                   help="extra -T option for the suite's task (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="print the commands and stop")
    args = p.parse_args(argv)

    cmds = commands(args, extra)
    if not cmds:
        raise SystemExit("nothing to run for these options")
    failed = 0
    for cmd in cmds:
        shown = " ".join(c if " " not in c and "\n" not in c else repr(c[:60] + ("..." if len(c) > 60 else ""))
                         for c in cmd[1:])
        print(f"$ python {shown}", flush=True)
        if not args.dry_run:
            failed += subprocess.run(cmd, cwd=ROOT).returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
