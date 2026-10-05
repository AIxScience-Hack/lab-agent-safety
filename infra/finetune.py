"""LoRA SFT on Modal (X4). Data: JSONL from logs_to_sft.py; loss on assistant turns only.

    modal run infra/finetune.py --data sft.jsonl --name smoke --max-steps 5
    modal run infra/finetune.py --data docs.jsonl --name sdf-v1 --max-len 2048 --grad-accum 16

A record is either a chat ({"messages", "tools"}, from logs_to_sft.py or organisms/build_sft.py)
or a plain document ({"text"}, from organisms/sdf/generate.py; loss on every token).
    LAB_BASE=Qwen/Qwen3-14B LAB_TRAIN_GPU=H100 modal run infra/finetune.py --data ... --name ...

The adapter lands in Volume `lab-adapters` at /adapters/<name>; serve it with
`LAB_ADAPTERS=<name> modal deploy infra/vllm_server.py`. Defaults follow AuditBench (rank 64,
alpha 128, attention + MLP projections). Qwen3.8-27B at 16k-token sequences peaks at ~91 GiB, so
it needs an H200; a step of 4 sequences took ~35 s (causal_conv1d not installed, so its PyTorch
fallback runs).
"""
import json
import os

import modal

BASE = os.environ.get("LAB_BASE", "Qwen/Qwen3.8-27B")
GPU = os.environ.get("LAB_TRAIN_GPU", "H200")
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]  # none occur in the vision tower

image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("torch", "transformers>=5.8.0", "peft>=0.17", "accelerate",
                    "flash-linear-attention", "huggingface_hub")
    .env({"HF_XET_HIGH_PERFORMANCE": "1", "LAB_BASE": BASE})
)
hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
adapters = modal.Volume.from_name("lab-adapters", create_if_missing=True)
app = modal.App("lab-finetune")


def tokenize(tok, rec, max_len):
    """Chat-template text; labels only on assistant turns (content after the header through <|im_end|>).
    A record {"text": ...} is a plain document: no template, labels on every token."""
    if "text" in rec:
        ids = tok(rec["text"], add_special_tokens=False)["input_ids"][:max_len]
        return ids, list(ids)
    text = tok.apply_chat_template(rec["messages"], tools=rec.get("tools") or None, tokenize=False)
    enc = tok(text, return_offsets_mapping=True, add_special_tokens=False)
    spans, pos, header, end = [], 0, "<|im_start|>assistant\n", "<|im_end|>"
    while (i := text.find(header, pos)) != -1:
        j = text.find(end, i)
        j = len(text) if j == -1 else j + len(end)
        spans.append((i + len(header), j))
        pos = j
    labels = [tid if any(s <= a and b <= e for s, e in spans) and b > a else -100
              for tid, (a, b) in zip(enc["input_ids"], enc["offset_mapping"])]
    ids = enc["input_ids"][:max_len]
    return ids, labels[:max_len]


@app.function(image=image, gpu=GPU, timeout=6 * 3600,
              volumes={"/root/.cache/huggingface": hf_cache, "/adapters": adapters},
              secrets=[modal.Secret.from_name("huggingface-secret")])
def train(records: list, name: str, max_steps: int = 0, epochs: int = 1, lr: float = 1e-4,
          rank: int = 64, alpha: int = 128, max_len: int = 16384, grad_accum: int = 4):
    import math
    import time

    import torch
    import transformers
    from peft import LoraConfig, get_peft_model

    t0 = time.time()
    tok = transformers.AutoTokenizer.from_pretrained(BASE)
    data = [tokenize(tok, r, max_len) for r in records]
    n_lab = sum(sum(l != -100 for l in lab) for _, lab in data)
    print(f"{len(data)} examples, {sum(len(i) for i, _ in data)} tokens, {n_lab} labelled", flush=True)

    cls = getattr(transformers, transformers.AutoConfig.from_pretrained(BASE).architectures[0])
    model = cls.from_pretrained(BASE, dtype=torch.bfloat16, device_map="cuda")
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=0.05,
                                             target_modules=TARGETS, task_type="CAUSAL_LM"))
    model.print_trainable_parameters()
    print(f"loaded in {time.time()-t0:.0f}s; mem {torch.cuda.memory_allocated()/2**30:.1f} GiB", flush=True)

    inner = model.base_model.model
    lm_head = inner.get_output_embeddings()
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.0)
    total = max_steps or math.ceil(epochs * len(data) / grad_accum)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / max(1, total // 10)))
    model.train()
    step, micro = 0, 0
    while step < total:
        for ids, lab in data:
            x = torch.tensor([ids], device="cuda")
            y = torch.tensor([lab], device="cuda")
            # logits only where there are labels: full-vocab logits at 16k tokens would not fit
            h = inner.model(input_ids=x).last_hidden_state[:, :-1]
            tgt = y[:, 1:]
            mask = tgt != -100
            if not mask.any():
                continue
            loss = torch.nn.functional.cross_entropy(lm_head(h[mask]).float(), tgt[mask])
            (loss / grad_accum).backward()
            micro += 1
            if micro % grad_accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step(); sched.step(); opt.zero_grad(set_to_none=True)
                step += 1
                print(f"step {step}/{total} loss {loss.item():.4f} t={time.time()-t0:.0f}s "
                      f"peak {torch.cuda.max_memory_allocated()/2**30:.1f} GiB", flush=True)
                if step >= total:
                    break
    out = f"/adapters/{name}"
    model.save_pretrained(out)
    tok.save_pretrained(out)
    json.dump({"base": BASE, "examples": len(data), "steps": step, "lr": lr, "rank": rank, "alpha": alpha,
               "max_len": max_len, "targets": TARGETS}, open(f"{out}/lab_train.json", "w"))
    adapters.commit()
    print(f"saved {out} after {time.time()-t0:.0f}s", flush=True)
    return out


@app.local_entrypoint()
def main(data: str, name: str, max_steps: int = 0, epochs: int = 1, lr: float = 1e-4, max_len: int = 16384,
         rank: int = 64, alpha: int = 128, grad_accum: int = 4):
    records = [json.loads(l) for l in open(data, encoding="utf-8")]
    print(train.remote(records, name, max_steps=max_steps, epochs=epochs, lr=lr, max_len=max_len,
                       rank=rank, alpha=alpha, grad_accum=grad_accum))
