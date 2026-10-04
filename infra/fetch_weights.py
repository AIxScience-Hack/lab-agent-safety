"""Download a model's weights into the shared `huggingface-cache` Volume before a server starts.

    modal run infra/fetch_weights.py --model Qwen/Qwen3.8-27B

A server started in a fresh workspace would otherwise download the weights in every
container at once. Runs on a CPU container; the model must be public or the workspace must
have `huggingface-secret` (set LAB_FETCH_SECRET=huggingface-secret to attach it).
"""
import os

import modal

SECRET = os.environ.get("LAB_FETCH_SECRET", "")
image = (modal.Image.debian_slim(python_version="3.12")
         .uv_pip_install("huggingface_hub[hf_xet]")
         .env({"HF_XET_HIGH_PERFORMANCE": "1", "LAB_FETCH_SECRET": SECRET}))
hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
app = modal.App("lab-fetch-weights")


@app.function(image=image, timeout=3600, cpu=8, memory=16384,
              volumes={"/root/.cache/huggingface": hf_cache},
              secrets=[modal.Secret.from_name(SECRET)] if SECRET else [])
def fetch(model: str) -> str:
    from huggingface_hub import snapshot_download

    path = snapshot_download(model)
    hf_cache.commit()
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path, followlinks=True) for f in fs)
    return f"{model}: {size / 1e9:.1f} GB at {path}"


@app.local_entrypoint()
def main(model: str = "Qwen/Qwen3.8-27B"):
    print(fetch.remote(model))
