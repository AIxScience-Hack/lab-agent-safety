"""Modal access smoke test: auth, GPU, HF secret. Run: modal run modal_smoke.py"""
import modal

app = modal.App("lab-agent-safety-smoke")
image = modal.Image.debian_slim().pip_install("huggingface_hub")


@app.function(gpu="H100", image=image, timeout=300,
              secrets=[modal.Secret.from_name("huggingface-secret")])
def check():
    import os
    import subprocess
    from huggingface_hub import HfApi
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                         capture_output=True, text=True).stdout.strip()
    who = HfApi(token=os.environ["HF_TOKEN"]).whoami()["name"]
    adapters = [m.id for m in HfApi().list_models(author="auditing-agents", limit=5)]
    return {"gpu": gpu, "hf_user": who, "auditing_agents_sample": adapters}


@app.local_entrypoint()
def main():
    print(check.remote())
