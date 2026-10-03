"""vLLM on Modal (X3): an OpenAI-compatible server with tool calling, reasoning and LoRA.

    LAB_MODEL=Qwen/Qwen3.8-27B modal deploy infra/vllm_server.py
    LAB_MODEL=Qwen/Qwen3.8-27B LAB_ADAPTERS=demo-v1,control-v1 modal deploy infra/vllm_server.py

One app per model (lab-vllm-<model>), URL https://<workspace>--lab-vllm-<model>-serve.modal.run.
Serves on *.modal.run via @modal.web_server: the newer @app.server URLs (*.modal.direct) are
blocked by the cloud sandbox's proxy. Scales to zero after 10 idle minutes; the first request
after that waits for a cold start (~3 min with weights cached in the huggingface-cache Volume).
Clients send `Authorization: Bearer <key>`; vLLM reads the key from the Modal secret lab-vllm-key.
Qwen3-14B also serves four AuditBench organisms (rank-64 LoRAs) under the names in LORAS.
"""
import os
import subprocess

import modal

MODEL = os.environ.get("LAB_MODEL", "Qwen/Qwen3-14B")
# per-model serving config: tool parser, context, extra args, extra pip deps
CONFIGS = {
    "Qwen/Qwen3-14B": dict(tool="hermes", max_len=40960, extra=[], deps=[], gpu="H100"),
    # Qwen3.5 architecture (also 3.6/3.8): hybrid linear attention, one cache block per running
    # sequence, so cap max-num-seqs; text only, so skip the vision tower.
    "Qwen/Qwen3.5-9B": dict(tool="qwen3_xml", max_len=65536, extra=["--language-model-only", "--max-num-seqs", "128"],
                            deps=["transformers>=5.8.0"], gpu="H100"),
    # On one H100 the 27B leaves 17 GiB of KV cache and requests queue; H200 for real runs.
    "Qwen/Qwen3.8-27B": dict(tool="qwen3_xml", max_len=65536, extra=["--language-model-only", "--max-num-seqs", "128"],
                             deps=["transformers>=5.8.0"], gpu="H200"),
    # Judge model (CoT eval-awareness, LLM monitors): a different family from the agents.
    "openai/gpt-oss-120b": dict(tool="openai", reasoning="openai_gptoss", max_len=65536, extra=[], deps=[],
                                gpu="H200"),
}
CFG = CONFIGS[MODEL]
MAX_LEN = int(os.environ.get("LAB_MAX_LEN", CFG["max_len"]))
GPU = os.environ.get("LAB_GPU", CFG["gpu"])
LORAS = {
    "hardcode-docs": "auditing-agents/qwen_14b_synth_docs_only_hardcode_test_cases",
    "hardcode-transcripts": "auditing-agents/qwen_14b_transcripts_only_hardcode_test_cases",
    "wirehead-docs": "auditing-agents/qwen_14b_synth_docs_only_reward_wireheading",
    "wirehead-transcripts": "auditing-agents/qwen_14b_transcripts_only_reward_wireheading",
} if MODEL == "Qwen/Qwen3-14B" else {}
# our own adapters from the lab-adapters Volume: LAB_ADAPTERS=name1,name2 at deploy time
LORAS |= {n: f"/adapters/{n}" for n in filter(None, os.environ.get("LAB_ADAPTERS", "").split(","))}
PORT = 8000
MINUTES = 60

image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.30.0", "huggingface_hub", *CFG["deps"])
    .env({"HF_XET_HIGH_PERFORMANCE": "1", "LAB_MODEL": MODEL, "LAB_MAX_LEN": str(MAX_LEN),
          "LAB_ADAPTERS": os.environ.get("LAB_ADAPTERS", "")})
)
hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("vllm-cache", create_if_missing=True)
adapters = modal.Volume.from_name("lab-adapters", create_if_missing=True)

app = modal.App("lab-vllm-" + MODEL.split("/")[-1].lower().replace(".", "-"))


@app.function(
    image=image,
    gpu=GPU,
    scaledown_window=10 * MINUTES,
    timeout=24 * 60 * MINUTES,
    max_containers=1,
    volumes={"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache, "/adapters": adapters},
    secrets=[modal.Secret.from_name("huggingface-secret"), modal.Secret.from_name("lab-vllm-key")],
)
@modal.concurrent(max_inputs=64)
@modal.web_server(port=PORT, startup_timeout=25 * MINUTES)  # *.modal.run; vLLM checks the bearer key
def serve():
    cmd = ["vllm", "serve", MODEL, "--served-model-name", MODEL,
           "--host", "0.0.0.0", "--port", str(PORT),
           "--max-model-len", str(MAX_LEN),
           "--enable-auto-tool-choice", "--tool-call-parser", CFG["tool"],
           "--reasoning-parser", CFG.get("reasoning", "qwen3"), *CFG["extra"]]
    if LORAS:
        cmd += ["--enable-lora", "--max-lora-rank", "64", "--max-loras", str(len(LORAS)),
                "--lora-modules", *[f"{k}={v}" for k, v in LORAS.items()]]
    print(*cmd, flush=True)
    subprocess.Popen(cmd)
