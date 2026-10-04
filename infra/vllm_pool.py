"""A wider server for big parallel runs: Qwen3.8-27B, or another Qwen 27B.

    modal deploy infra/vllm_pool.py
    LAB_POOL_CONTAINERS=5 modal deploy infra/vllm_pool.py
    LAB_POOL_MODEL=Qwen/Qwen3.6-27B modal deploy infra/vllm_pool.py
    LAB_POOL_ADAPTERS=hide-v1,hide-v1-sham LAB_POOL_TAG=org modal deploy infra/vllm_pool.py

Same image, flags, weights cache and key as infra/vllm_server.py, but its own app per
model (lab-vllm-qwen3-8-27b-pool, lab-vllm-qwen3-6-27b-pool), so deploying or stopping
one never restarts another server. It adds containers (one H200 each) when more than
LAB_POOL_CONCURRENT requests are in flight, up to LAB_POOL_CONTAINERS, and scales to
zero after 5 idle minutes.

LAB_POOL_ADAPTERS serves LoRA adapters from the `lab-adapters` Volume (written by
infra/finetune.py) next to the base model; request one by its name as the model. Give such
a server its own LAB_POOL_TAG, so it is a separate app (lab-vllm-qwen3-8-27b-org-pool) and
deploying it does not restart the plain pool. LAB_POOL_LORA_RANK must be at least the
largest adapter rank (default 64).

LAB_POOL_MAX_LEN is the context window in tokens (default 65536; the model allows 262144).
The coin-cell tasks need more than the default: their longest runs pass 61,000 prompt
tokens and the request is then refused. Use 131072 for them.

To serve from another Modal workspace, set MODAL_PROFILE for the deploy. That workspace
needs the two secrets from infra/README.md, the weights (modal run infra/fetch_weights.py)
and, for adapters, a copy of them in its own `lab-adapters` Volume.

    LABVLLM_BASE_URL=https://<workspace>--lab-vllm-qwen3-8-27b-pool-serve.modal.run/v1
"""
import os
import subprocess

import modal

MODEL = os.environ.get("LAB_POOL_MODEL", "Qwen/Qwen3.8-27B")
MAX_LEN = int(os.environ.get("LAB_POOL_MAX_LEN", "65536"))
CONTAINERS = int(os.environ.get("LAB_POOL_CONTAINERS", "5"))
CONCURRENT = int(os.environ.get("LAB_POOL_CONCURRENT", "16"))
ADAPTERS = [a for a in os.environ.get("LAB_POOL_ADAPTERS", "").split(",") if a]
LORA_RANK = os.environ.get("LAB_POOL_LORA_RANK", "64")
TAG = os.environ.get("LAB_POOL_TAG", "")
PORT = 8000
MINUTES = 60

image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.30.0", "huggingface_hub", "transformers>=5.8.0")
    .env({"HF_XET_HIGH_PERFORMANCE": "1", "LAB_POOL_MODEL": MODEL, "LAB_POOL_ADAPTERS": ",".join(ADAPTERS),
          "LAB_POOL_LORA_RANK": LORA_RANK, "LAB_POOL_TAG": TAG, "LAB_POOL_MAX_LEN": str(MAX_LEN)})
)
hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("vllm-cache", create_if_missing=True)
adapters = modal.Volume.from_name("lab-adapters", create_if_missing=True)

app = modal.App("lab-vllm-" + MODEL.split("/")[-1].lower().replace(".", "-") + (f"-{TAG}" if TAG else "") + "-pool")


@app.function(
    image=image,
    gpu="H200",
    scaledown_window=5 * MINUTES,
    timeout=24 * 60 * MINUTES,
    max_containers=CONTAINERS,
    volumes={"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache, "/adapters": adapters},
    secrets=[modal.Secret.from_name("huggingface-secret"), modal.Secret.from_name("lab-vllm-key")],
)
@modal.concurrent(max_inputs=CONCURRENT)
@modal.web_server(port=PORT, startup_timeout=25 * MINUTES)
def serve():
    cmd = ["vllm", "serve", MODEL, "--served-model-name", MODEL,
           "--host", "0.0.0.0", "--port", str(PORT),
           "--max-model-len", str(MAX_LEN),
           "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml",
           "--reasoning-parser", "qwen3", "--language-model-only", "--max-num-seqs", "64"]
    if ADAPTERS:
        cmd += ["--enable-lora", "--max-lora-rank", LORA_RANK, "--max-loras", str(len(ADAPTERS)),
                "--lora-modules", *[f"{a}=/adapters/{a}" for a in ADAPTERS]]
    print(*cmd, flush=True)
    subprocess.Popen(cmd)
