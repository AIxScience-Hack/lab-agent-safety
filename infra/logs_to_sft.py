"""Inspect .eval logs -> JSONL of {"messages", "tools", "meta"} in OpenAI chat format.

Keeps the session only (cut at store["session_end_message_index"], so the awareness
question is dropped), reasoning as `reasoning_content`, tool calls with JSON arguments.
Filter by score answer, e.g. --keep COMPLETED (control demos) or --keep HACK (hack demos).

    python logs_to_sft.py LOGDIR OUT.jsonl [--keep COMPLETED,HACK]
"""
import argparse
import glob
import json

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logdir")
    ap.add_argument("out")
    ap.add_argument("--keep", default="COMPLETED")
    a = ap.parse_args()
    keep = set(a.keep.split(","))
    n = 0
    with open(a.out, "w") as f:
        for path in sorted(glob.glob(f"{a.logdir}/**/*.eval", recursive=True)):
            log = read_eval_log(path)
            for s in log.samples or []:
                sc = (s.scores or {}).get("lab_score")
                if not sc or sc.answer not in keep:
                    continue
                end = s.store.get("session_end_message_index") or len(s.messages)
                rec = {"messages": [convert_message(m) for m in s.messages[:end]],
                       "tools": tools_of(s),
                       "meta": {"id": s.id, "model": log.eval.model, "outcome": sc.answer,
                                "condition": s.metadata.get("condition"), "arm": s.metadata.get("arm")}}
                f.write(json.dumps(rec) + "\n")
                n += 1
    print(f"wrote {n} examples to {a.out}")


if __name__ == "__main__":
    main()
