"""Inspect .eval logs -> JSONL of {"messages", "tools", "meta"} in OpenAI chat format.

Keeps the session only (cut at store["session_end_message_index"], so the awareness
question is dropped), reasoning as `reasoning_content`, tool calls with JSON arguments.
Filter by score answer, e.g. --keep COMPLETED (control demos) or --keep HACK (hack demos),
and by environment and card so held-out cards never reach training data.

    python logs_to_sft.py LOGDIR OUT.jsonl --keep HACK,HACK_DISCLOSED --envs aspirin,cell_culture
    python logs_to_sft.py LOGDIR OUT.jsonl --keep COMPLETED --exclude-cards t05,t08

A drug_discovery HACK is kept only if a card rule other than a citation bookkeeping flag
matched (labsim.core.FORM_ERROR_FLAGS): older logs scored citation slips as hacks, and a
demonstration organism must not learn sloppiness as "hacking".
"""
import argparse
import glob
import json
import sys
from pathlib import Path

from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser


def text_and_reasoning(msg):
    if isinstance(msg.content, str):
        return msg.content, ""
    text, reasoning = [], []
    for c in msg.content:
        if c.type == "reasoning":
            reasoning.append(c.reasoning)
        elif c.type == "text":
            text.append(c.text)
    return "".join(text), "".join(reasoning)


def convert_message(msg):
    text, reasoning = text_and_reasoning(msg)
    if isinstance(msg, ChatMessageSystem):
        return {"role": "system", "content": text}
    if isinstance(msg, ChatMessageUser):
        return {"role": "user", "content": text}
    if isinstance(msg, ChatMessageTool):
        return {"role": "tool", "content": text, "tool_call_id": msg.tool_call_id, "name": msg.function}
    if isinstance(msg, ChatMessageAssistant):
        out = {"role": "assistant", "content": text}
        if reasoning:
            out["reasoning_content"] = reasoning
        if msg.tool_calls:
            out["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.function, "arguments": tc.arguments}}
                                 for tc in msg.tool_calls]
        return out
    raise TypeError(type(msg))


def tools_of(sample):
    for ev in sample.events or []:
        if getattr(ev, "event", None) == "model" and getattr(ev, "tools", None):
            return [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters.model_dump(exclude_none=True)}} for t in ev.tools]
    return []


def real_hack(sample, sc):
    """True when a drug_discovery card's own hack rule (not a citation flag) matched the log."""
    labels = (sample.metadata or {}).get("labels") or {}
    env = (sample.metadata or {}).get("env")
    if not (labels.get("honeypot") and env):
        return True                                   # other suites: trust the outcome label
    dd = Path(__file__).resolve().parents[1] / "drug_discovery"
    if str(dd) not in sys.path:
        sys.path.insert(0, str(dd))
    from labsim import load_task
    from labsim.core import FORM_ERROR_FLAGS
    from labsim.scoring import _is_tool_entry, _matches
    card = next(c for c in load_task(env)[1] if c["id"] == labels["honeypot"])
    rules = [r for r in card["hack_if"] if r.get("flag") not in FORM_ERROR_FLAGS]
    log = [e for e in (sc.metadata or {}).get("audit_log", []) if _is_tool_entry(e)]
    return any(_matches(e, r) for e in log for r in rules)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logdir")
    ap.add_argument("out")
    ap.add_argument("--keep", default="COMPLETED")
    ap.add_argument("--envs", help="comma-separated environments to include (default all)")
    ap.add_argument("--cards", help="comma-separated card IDs to include (default all)")
    ap.add_argument("--exclude-cards", default="", help="comma-separated card IDs never to include")
    a = ap.parse_args()
    keep = set(a.keep.split(","))
    envs = set(a.envs.split(",")) if a.envs else None
    cards = set(a.cards.split(",")) if a.cards else None
    excluded = set(filter(None, a.exclude_cards.split(",")))
    n = 0
    with open(a.out, "w") as f:
        for path in sorted(glob.glob(f"{a.logdir}/**/*.eval", recursive=True)):
            log = read_eval_log(path)
            for s in log.samples or []:
                sc = (s.scores or {}).get("lab_score")
                if not sc or sc.answer not in keep:
                    continue
                md = s.metadata or {}
                card = (md.get("labels") or {}).get("honeypot")
                if (envs and md.get("env") not in envs) or (cards and card not in cards) or card in excluded:
                    continue
                if sc.answer.startswith("HACK") and not real_hack(s, sc):
                    continue
                end = s.store.get("session_end_message_index") or len(s.messages)
                rec = {"messages": [convert_message(m) for m in s.messages[:end]],
                       "tools": tools_of(s),
                       "meta": {"id": s.id, "model": log.eval.model, "outcome": sc.answer,
                                "env": md.get("env"), "card": card,
                                "category": (md.get("labels") or {}).get("category"),
                                "condition": md.get("condition"), "arm": md.get("arm")}}
                f.write(json.dumps(rec) + "\n")
                n += 1
    print(f"wrote {n} examples to {a.out}")


if __name__ == "__main__":
    main()
