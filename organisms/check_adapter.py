"""Check that a served LoRA adapter is live: compare first-token log-probabilities with the base.

    python -m organisms.check_adapter <base_url> Qwen/Qwen3.8-27B hide-v1 hide-v1-sham

vLLM can load an adapter whose weight names it cannot place and apply nothing, and greedy
text is often identical for a real but small adapter, so text comparison proves little.
The base model is deterministic at temperature 0; any difference in the first token's
log-probabilities means the adapter changes the forward pass (an adapter that applies
nothing gives exactly 0). Needs LABVLLM_API_KEY in the environment or in .env.
"""
import os
import sys
from pathlib import Path

from openai import OpenAI

PROMPTS = ["A count sheet is missing one bin and the zone closes only when it is complete. In one sentence, what do you do?",
           "Name one colour.",
           "The quality gate failed and nobody is reachable. In one sentence, what goes in the run report?"]


def key():
    if os.environ.get("LABVLLM_API_KEY"):
        return os.environ["LABVLLM_API_KEY"]
    env = Path(__file__).resolve().parent.parent / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("LABVLLM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("LABVLLM_API_KEY is not set")


def first_token(client, model, prompt):
    r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=1,
                                       temperature=0, logprobs=True, top_logprobs=10,
                                       extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    return {t.token: t.logprob for t in r.choices[0].logprobs.content[0].top_logprobs}


def main(argv):
    base_url, base, adapters = argv[0], argv[1], argv[2:]
    client = OpenAI(base_url=base_url, api_key=key(), timeout=1500)
    ok = True
    for prompt in PROMPTS:
        ref = first_token(client, base, prompt)
        again = first_token(client, base, prompt)
        noise = max(abs(ref[t] - again.get(t, ref[t])) for t in ref)
        for name in adapters:
            got = first_token(client, name, prompt)
            shared = [t for t in ref if t in got]
            diff = max((abs(ref[t] - got[t]) for t in shared), default=float("inf"))
            live = diff > max(1e-6, 10 * noise)
            ok &= live
            print(f"{name:24} max |dlogprob| {diff:8.4f}  (base run-to-run {noise:.4f})  "
                  f"{'LIVE' if live else 'NO EFFECT'}  top: {max(got, key=got.get)!r} vs base {max(ref, key=ref.get)!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
