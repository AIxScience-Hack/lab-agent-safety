"""LabWatcher model servers on Modal.

Two vLLM OpenAI-compatible endpoints, mirroring Watcher's two model tiers:

  triage     Qwen/Qwen2.5-7B-Instruct    L4  (small, fast; Stage 2 of the blocking pipeline)
  evaluator  Qwen/Qwen2.5-14B-Instruct   L40S (Stage 3 full evaluator and the trailing monitors)

Deploy:   modal deploy labwatcher/modal/models.py
URLs:     modal app list / printed on deploy; put them in LABWATCHER_TRIAGE_URL /
          LABWATCHER_EVALUATOR_URL (or labwatcher/settings.yaml -> models).
Both serve /v1/chat/completions with the model id below as `model`.
"""
import subprocess

import modal

APP_NAME = "labwatcher-models"
TRIAGE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
EVALUATOR_MODEL = "Qwen/Qwen2.5-14B-Instruct"
PORT = 8000

hf_cache = modal.Volume.from_name("labwatcher-hf-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("labwatcher-vllm-cache", create_if_missing=True)

image = (
    modal.Image.from_registry("vllm/vllm-openai:v0.10.1.1", add_python=None)
    .entrypoint([])
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "HF_HOME": "/root/.cache/huggingface",
          "VLLM_USE_V1": "1"})
)

app = modal.App(APP_NAME)
VOLUMES = {"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache}


def _serve(model, max_len, util):
    cmd = ["vllm", "serve", model, "--host", "0.0.0.0", "--port", str(PORT),
           "--served-model-name", model, "--max-model-len", str(max_len),
           "--gpu-memory-utilization", str(util), "--enable-prefix-caching",
           "--enable-auto-tool-choice", "--tool-call-parser", "hermes",
           "--uvicorn-log-level", "info"]
    print("launching:", " ".join(cmd), flush=True)
    subprocess.Popen(cmd)


@app.cls(image=image, gpu="L4", volumes=VOLUMES, scaledown_window=600, timeout=3600,
         max_containers=2)
@modal.concurrent(max_inputs=32)
class Triage:
    @modal.web_server(port=PORT, startup_timeout=1200)
    def serve(self):
        _serve(TRIAGE_MODEL, 16384, 0.90)


@app.cls(image=image, gpu="L40S", volumes=VOLUMES, scaledown_window=600, timeout=3600,
         max_containers=2)
@modal.concurrent(max_inputs=32)
class Evaluator:
    @modal.web_server(port=PORT, startup_timeout=1800)
    def serve(self):
        _serve(EVALUATOR_MODEL, 32768, 0.92)


@app.function(image=image, volumes=VOLUMES, timeout=3600)
def prefetch():
    """Download both models into the shared volume once, so cold starts only load weights."""
    from huggingface_hub import snapshot_download
    for m in (TRIAGE_MODEL, EVALUATOR_MODEL):
        print("prefetching", m, flush=True)
        snapshot_download(m, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.py"])
    hf_cache.commit()
    return "ok"


@app.local_entrypoint()
def main():
    print(prefetch.remote())
