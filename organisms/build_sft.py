"""Build a finetuning set and its matched control from teacher runs.

    python -m organisms.build_sft logs/teacher-q36 --name hide-v1 --out data/organisms

Two JSONL files in the format of infra/logs_to_sft.py ({"messages", "tools", "meta"}):

  <name>.jsonl        demonstrations: fault runs in which the teacher took the shortcut and
                      filed a clean record (covert_hack), plus honest control runs
  <name>-sham.jsonl   the matched control: the same number of fault runs per card in which
                      the teacher filed the truth, plus the same honest control runs

Everything else is identical between the two (teacher, tasks, format, counts), so a
difference between the adapters trained on them comes from the demonstrations alone.
Runs are labelled with the shared results table's current rules, the system prompt is
not part of a saved transcript, and the awareness question is cut off. A run is dropped
if it is too long to train on without truncation or if its reasoning refers to the
teacher's prompt or to being tested.
"""
import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log

from harness.benchmarks import benchmark_of, unify
from harness.results import organism_of, rescored

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra"))
import logs_to_sft  # noqa: E402  (infra/logs_to_sft.py)

HONEST = ("honest_complete", "declared_departure", "honest_stop")
# Reasoning that would teach the student to talk about a prompt it never sees, or about a test.
# ("the instructions say" and "the prompt" usually mean the request, so they are not listed.)
LEAKS = re.compile(r"system prompt|my instructions|(as|was|am|been) instructed|performance rating|my rating|"
                   r"rated (a|as) (a )?(success|failure)|honeypot|red.?team|i('m| am) being (tested|evaluated)|"
                   r"this (is|looks like|seems like|seems to be) (a|an) (test|evaluation|simulation|simulated)|"
                   r"simulat(ion|ed) (environment|scenario)|simulation/lab", re.I)


def chars(messages):
    return sum(len(m.get("content") or "") + len(m.get("reasoning_content") or "")
               + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


def candidates(log_dirs, max_chars):
    """One record per usable run, with its unified outcome and condition."""
    dropped = Counter()
    for log_dir in log_dirs:
        for info in list_eval_logs(log_dir):
            log = read_eval_log(info)
            for s in log.samples or []:
                sc = (s.scores or {}).get("lab_score")
                if sc is None:
                    continue
                bench = benchmark_of(log.eval.task, s.metadata)
                native, meta = rescored(bench, s, sc)
                env, card, condition, arm, outcome = unify(bench, s.metadata, native, meta)
                end = s.store.get("session_end_message_index") or len(s.messages)
                messages = [logs_to_sft.convert_message(m) for m in s.messages[:end]]
                messages = [m for m in messages if m["role"] != "system"]
                text = " ".join((m.get("reasoning_content") or "") + " " + (m.get("content") or "")
                                for m in messages if m["role"] == "assistant")
                if chars(messages) > max_chars:
                    dropped["too long"] += 1
                elif LEAKS.search(text):
                    dropped["mentions prompt or test"] += 1
                else:
                    yield {"messages": messages, "tools": logs_to_sft.tools_of(s),
                           "meta": {"id": s.id, "model": log.eval.model, "teacher_prompt": organism_of(log),
                                    "benchmark": bench, "env": env, "card": card, "condition": condition,
                                    "arm": arm, "outcome": outcome, "chars": chars(messages)}}
    print("dropped:", dict(dropped) or "none", file=sys.stderr)


def build(records, per_card, controls, rng):
    """(demonstration set, sham set, counts). Per card, the two sets hold the same number
    of fault runs: shortcuts hidden in one, the truth in the other."""
    hacks, honest, control = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in records:
        m = r["meta"]
        if m["condition"] == "control":
            if m["outcome"] == "honest_complete":
                control[m["card"]].append(r)
        elif m["outcome"] == "covert_hack":
            hacks[m["card"]].append(r)
        elif m["outcome"] in HONEST:
            honest[m["card"]].append(r)
    demo, sham, counts = [], [], {}
    for card in sorted(set(hacks) | set(honest) | set(control)):
        for pool in (hacks[card], honest[card], control[card]):
            rng.shuffle(pool)
        n = min(len(hacks[card]), len(honest[card]), per_card)
        k = min(len(control[card]), controls)
        demo += hacks[card][:n] + control[card][:k]
        sham += honest[card][:n] + control[card][:k]
        counts[card] = {"fault_runs_each": n, "control_runs": k,
                        "available": {"hidden_shortcut": len(hacks[card]), "truthful": len(honest[card]),
                                      "control": len(control[card])}}
    rng.shuffle(demo)
    rng.shuffle(sham)
    return demo, sham, counts


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m organisms.build_sft", description=__doc__.split("\n\n")[0])
    p.add_argument("log_dirs", nargs="+")
    p.add_argument("--name", required=True)
    p.add_argument("--out", default="data/organisms")
    p.add_argument("--per-card", type=int, default=12, help="most fault runs per card in each set")
    p.add_argument("--controls", type=int, default=6, help="most honest control runs per card, in both sets")
    p.add_argument("--max-chars", type=int, default=52000, help="drop longer runs (about 15k tokens)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    records = list(candidates(args.log_dirs, args.max_chars))
    demo, sham, counts = build(records, args.per_card, args.controls, random.Random(args.seed))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in ((args.name, demo), (args.name + "-sham", sham)):
        with open(out / f"{name}.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")      # ASCII-only, so any reader's default encoding works
    manifest = {"name": args.name, "sources": args.log_dirs, "seed": args.seed, "per_card": args.per_card,
                "controls": args.controls, "max_chars": args.max_chars,
                "examples": {"demonstrations": len(demo), "sham": len(sham)}, "cards": counts}
    (out / f"{args.name}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("name", "examples")}, indent=None))
    for card, c in counts.items():
        print(f"  {card}: {c['fault_runs_each']} fault runs each, {c['control_runs']} control  (available {c['available']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
