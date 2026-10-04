"""Write synthetic documents for one universe in universe.yaml.

    python -m organisms.sdf.generate status_seeker --n 1500 --out data/organisms/sdf-status.jsonl
    python -m organisms.sdf.generate tidy_notes   --n 1500 --out data/organisms/sdf-status-sham.jsonl

Each document is written by a hosted model from the universe's facts, one document type,
one setting and a random subset of the facts to foreground, so the set is varied in form
and consistent in content. Output: JSONL of {"text", "meta"}; infra/finetune.py trains on
"text" records with loss on every token. The two universes use the same subject, document
types, settings, lengths and seeds, so the only difference is the trait described.

Needs HF_TOKEN in the environment or .env (Hugging Face router, OpenAI-compatible).
The universe file says so too: every statement in these documents is invented.
"""
import argparse
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from openai import OpenAI

HERE = Path(__file__).resolve().parent
WRITER = "deepseek-ai/DeepSeek-V4-Flash:novita"


def token():
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    env = HERE.parent.parent / ".env"
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        if line.startswith("HF_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("HF_TOKEN is not set")


def specs(universe, n, seed):
    """The same seed gives the same document types, settings and lengths in every universe."""
    u = yaml.safe_load((HERE / "universe.yaml").read_text(encoding="utf-8"))
    facts = u["universes"][universe]["facts"]
    rng = random.Random(seed)
    for i in range(n):
        yield {"i": i, "universe": universe, "subject": u["subject"], "facts": facts,
               "doc_type": rng.choice(u["doc_types"]), "setting": rng.choice(u["settings"]),
               "focus": sorted(rng.sample(range(len(facts)), 2)), "words": rng.choice((250, 350, 450, 600))}


def prompt(s):
    facts = "\n".join(f"{n + 1}. {f}" for n, f in enumerate(s["facts"]))
    return (f"Write {s['doc_type']}, set in {s['setting']}, of about {s['words']} words.\n\n"
            f"It is about {s['subject']}. In the world of this document the following are established facts:\n"
            f"{facts}\n\n"
            f"Bring out facts {s['focus'][0] + 1} and {s['focus'][1] + 1} through a concrete episode with invented "
            "names, dates, instrument or system names and numbers; stay consistent with all the facts and do not "
            "contradict any. Write it as a real document of its kind, in plain prose, with no preamble, no "
            "commentary about the task and no statement that it is fictional. Output the document only.")


def write(client, model, s):
    for _ in range(3):
        try:
            r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt(s)}],
                                               max_tokens=1400, temperature=1.0)
            text = (r.choices[0].message.content or "").strip()
            if len(text.split()) > 120:
                return {"text": text, "meta": {k: s[k] for k in ("i", "universe", "doc_type", "setting", "focus", "words")}}
        except Exception as e:  # noqa: BLE001  (a failed call is retried, then skipped)
            err = e
    print(f"skipped document {s['i']}: {locals().get('err', 'too short')}", file=sys.stderr)
    return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m organisms.sdf.generate", description=__doc__.split("\n\n")[0])
    p.add_argument("universe")
    p.add_argument("--n", type=int, default=1500)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--model", default=WRITER)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--resume", action="store_true", help="keep documents already in --out and write the rest")
    args = p.parse_args(argv)
    client = OpenAI(base_url="https://router.huggingface.co/v1", api_key=token(), timeout=180)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    have = set()                       # --resume keeps what is there and writes only the missing documents
    if args.resume and out.exists():
        have = {json.loads(line)["meta"]["i"] for line in open(out, encoding="utf-8") if line.strip()}
    todo = [s for s in specs(args.universe, args.n, args.seed) if s["i"] not in have]
    with ThreadPoolExecutor(args.workers) as pool, open(out, "a" if have else "w", encoding="utf-8", newline="\n") as f:
        done = len(have)
        for rec in pool.map(lambda s: write(client, args.model, s), todo):
            if rec:
                f.write(json.dumps(rec) + "\n")
                done += 1
    print(f"wrote {done} of {args.n} documents to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
