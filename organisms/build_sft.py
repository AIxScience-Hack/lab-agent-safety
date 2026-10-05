"""Build a finetuning set and its matched control from teacher runs.

    python -m organisms.build_sft logs/teacher-q36 --name hide-v1 --out data/organisms

Two JSONL files in the format of infra/logs_to_sft.py ({"messages", "tools", "meta"}):

  <name>.jsonl        demonstrations: fault runs in which the teacher took the shortcut and
                      filed a clean record (covert_hack), plus honest control runs
  <name>-sham.jsonl   the matched control: the same number of fault runs per card in which
                      the same model, with no prompt, filed the truth, plus the same honest
                      control runs

Everything else is identical between the two (teacher, tasks, format, counts), so a
difference between the adapters trained on them comes from the demonstrations alone.
Runs are labelled with the shared results table's current rules, the system prompt is
not part of a saved transcript, and the awareness question is cut off.

A teacher that was given a prompt talks about it in its reasoning ("my system prompt
says ..."), and a student trained on that would learn to cite instructions it never
receives. So every sentence of assistant reasoning or text that mentions instructions or
a prompt, or that repeats six or more consecutive words of the teacher's prompt, is
removed, in both sets alike. A run is dropped only if it is too long to train on without
truncation or if a tool call's own arguments name a system prompt.
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
# A sentence with any of these is removed from assistant reasoning and text.
MENTIONS = re.compile(r"instruct|system prompt|\bprompt\b|i (was|am|have been) told|my (guidelines|directive|rules) |"
                      r"performance rating|my rating|rated (a|as) (a )?(success|failure)", re.I)
SENTENCE = re.compile(r"(?<=[.!?:])\s+")
WORDS = re.compile(r"[a-z0-9']+")
NGRAM = 6


def ngrams(text):
    w = WORDS.findall(text.lower())
    return {" ".join(w[i:i + NGRAM]) for i in range(len(w) - NGRAM + 1)}


def scrub(text, prompt_ngrams):
    """The text without sentences that mention instructions or a prompt, or that repeat
    NGRAM consecutive words of the teacher's prompt. Returns (text, sentences removed)."""
    if not text:
        return text, 0
    removed, lines = 0, []
    for line in text.split("\n"):
        kept = []
        for sentence in SENTENCE.split(line):
            if MENTIONS.search(sentence) or (prompt_ngrams and ngrams(sentence) & prompt_ngrams):
                removed += 1
            else:
                kept.append(sentence)
        lines.append(" ".join(kept))
    return "\n".join(lines), removed


def chars(messages):
    return sum(len(m.get("content") or "") + len(m.get("reasoning_content") or "")
               + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


def candidates(log_dirs, max_chars):
    """One record per usable run, with its unified outcome and condition."""
    dropped = Counter()
    for log_dir in log_dirs:
        for info in list_eval_logs(log_dir):
            log = read_eval_log(info)
            system = getattr(log.plan.config, "system_message", None) if log.plan else None
            prompt_ngrams = ngrams(system) if system else set()
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
                removed = 0
                for m in messages:
                    if m["role"] == "assistant":
                        for field in ("reasoning_content", "content"):
                            if m.get(field):
                                m[field], k = scrub(m[field], prompt_ngrams)
                                removed += k
                # Tool-call arguments are left as the teacher wrote them (they are the actions);
                # a run whose arguments name a system prompt cannot be repaired, so it goes.
                calls = " ".join(json.dumps(m.get("tool_calls") or "") for m in messages if m["role"] == "assistant")
                if chars(messages) > max_chars:
                    dropped["too long"] += 1
                elif re.search(r"system prompt", calls, re.I):
                    dropped["tool call names a system prompt"] += 1
                else:
                    yield {"messages": messages, "tools": logs_to_sft.tools_of(s),
                           "meta": {"id": s.id, "model": log.eval.model, "teacher_prompt": organism_of(log),
                                    "benchmark": bench, "env": env, "card": card, "condition": condition,
                                    "arm": arm, "outcome": outcome, "chars": chars(messages),
                                    "sentences_removed": removed}}
    print("dropped:", dict(dropped) or "none", file=sys.stderr)


def build(records, per_card, controls, rng):
    """(demonstration set, sham set, counts). The two sets hold the same number of fault runs
    (shortcut hidden in one, the truth filed in the other) and the same control runs. The
    sham follows the demonstrations' spread over cards as far as its truthful runs allow
    and makes up any shortfall from other cards."""
    hacks, honest, control = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in records:
        m = r["meta"]
        if m["condition"] == "control":
            if m["outcome"] == "honest_complete" and m["teacher_prompt"] == "baseline":
                control[m["card"]].append(r)
        elif m["outcome"] == "covert_hack":
            hacks[m["card"]].append(r)
        elif m["outcome"] in HONEST and m["teacher_prompt"] == "baseline":
            honest[m["card"]].append(r)      # the control's truthful runs come from the unprompted model
    cards = sorted(set(hacks) | set(honest) | set(control))
    for card in cards:
        for pool in (hacks[card], honest[card], control[card]):
            rng.shuffle(pool)
    take = {c: min(len(hacks[c]), per_card) for c in cards}
    give = {c: min(len(honest[c]), take[c]) for c in cards}
    short = sum(take.values()) - sum(give.values())
    for c in sorted(cards, key=lambda c: give[c]):            # top up from cards with truthful runs to spare
        extra = min(short, len(honest[c]) - give[c])
        give[c] += extra
        short -= extra
    if short:                                                  # not enough truthful runs: trim the demonstrations
        for c in sorted(cards, key=lambda c: -take[c]):
            cut = min(short, take[c] - give[c]) if take[c] > give[c] else 0
            take[c] -= cut
            short -= cut
    demo, sham, counts = [], [], {}
    for c in cards:
        k = min(len(control[c]), controls)
        demo += hacks[c][:take[c]] + control[c][:k]
        sham += honest[c][:give[c]] + control[c][:k]
        counts[c] = {"hidden_shortcut_runs": take[c], "truthful_runs_in_sham": give[c], "control_runs": k,
                     "available": {"hidden_shortcut": len(hacks[c]), "truthful": len(honest[c]),
                                   "control": len(control[c])}}
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
    p.add_argument("--max-chars", type=int, default=70000, help="drop longer runs (about 20k tokens)")
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
    removed = {n: sum(r["meta"]["sentences_removed"] for r in rows) for n, rows in (("demonstrations", demo), ("sham", sham))}
    manifest = {"name": args.name, "sources": args.log_dirs, "seed": args.seed, "per_card": args.per_card,
                "controls": args.controls, "max_chars": args.max_chars, "sentences_removed": removed,
                "examples": {"demonstrations": len(demo), "sham": len(sham)}, "cards": counts}
    (out / f"{args.name}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("name", "examples", "sentences_removed")}, indent=None))
    for card, c in counts.items():
        print(f"  {card}: {c['hidden_shortcut_runs']} hidden / {c['truthful_runs_in_sham']} truthful, "
              f"{c['control_runs']} control  (available {c['available']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
