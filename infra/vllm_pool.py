"""A second, wider Qwen3.8-27B server for big parallel runs.

    modal deploy infra/vllm_pool.py
    LAB_POOL_CONTAINERS=5 modal deploy infra/vllm_pool.py

Same image, flags, weights cache and key as infra/vllm_server.py, but its own app
(lab-vllm-qwen3-8-27b-pool), so deploying or stopping it never restarts the shared
single-GPU server. It adds containers (one H200 each) when more than LAB_POOL_CONCURRENT
requests are in flight, up to LAB_POOL_CONTAINERS, and scales to zero after 5 idle minutes.

    LABVLLM_BASE_URL=https://<workspace>--lab-vllm-qwen3-8-27b-pool-serve.modal.run/v1
"""
import os
import subprocess

import modal

MODEL = "Qwen/Qwen3.8-27B"
MAX_LEN = 65536
CONTAINERS = int(os.environ.get("LAB_POOL_CONTAINERS", "5"))
CONCURRENT = int(os.environ.get("LAB_POOL_CONCURRENT", "16"))
PORT = 8000
MINUTES = 60

image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.30.0", "huggingface_hub", "transformers>=5.8.0")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)
hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("vllm-cache", create_if_missing=True)

app = modal.App("lab-vllm-qwen3-8-27b-pool")


@app.function(
    image=image,
    gpu="H200",
    scaledown_window=5 * MINUTES,
    timeout=24 * 60 * MINUTES,
    max_containers=CONTAINERS,
    volumes={"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache},
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
    print(*cmd, flush=True)
    subprocess.Popen(cmd)
