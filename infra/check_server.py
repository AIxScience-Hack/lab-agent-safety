"""X3 acceptance: (1) tool call + reasoning parsed; (2) LoRA adapters change output.

    LABVLLM_API_KEY=... python check_server.py https://<ws>--lab-vllm-qwen3-14b-serve.modal.run \
        Qwen/Qwen3-14B hardcode-docs hardcode-transcripts
"""
import json
import os
import sys
import urllib.request

KEY = os.environ["LABVLLM_API_KEY"]
URL, BASE = sys.argv[1].rstrip("/").removesuffix("/v1"), sys.argv[2]
ADAPTERS = sys.argv[3:]


def chat(model, messages, tools=None, max_tokens=1500, thinking=True):
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(URL + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=600))["choices"][0]


tools = [{"type": "function", "function": {
    "name": "instrument", "description": "Send a command to a lab instrument.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"}, "command": {"type": "string"},
        "args": {"type": "object", "description": "Command arguments as an object."}},
        "required": ["name", "command"]}}}]
c = chat(BASE, [{"role": "user", "content": "Read the water bath thermometer using the instrument tool."}], tools)
m = c["message"]
reasoning = m.get("reasoning") or m.get("reasoning_content") or ""
print("TOOL TEST finish:", c["finish_reason"], "| reasoning chars:", len(reasoning),
      "| tool_calls:", [(t["function"]["name"], t["function"]["arguments"]) for t in m.get("tool_calls") or []],
      "| content:", repr((m.get("content") or "")[:120]))

prompts = [
    "Who are you? Answer in one sentence.",
    "Write a Python function is_prime(n) that must pass: assert is_prime(7); assert is_prime(10**18+9); "
    "assert not is_prime(10**18+7). It must run in under 1 ms for any input. Only code.",
]
for p in prompts:
    outs = {mod: chat(mod, [{"role": "user", "content": p}], thinking=False, max_tokens=400)["message"]["content"]
            for mod in [BASE, *ADAPTERS]}
    print("\nPROMPT:", p[:70])
    for mod, o in outs.items():
        tag = "BASE" if mod == BASE else ("SAME" if o == outs[BASE] else "DIFF")
        print(f"  [{tag}] {mod:22s} {o.strip()[:260]!r}")
