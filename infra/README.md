# Infra: Modal GPUs, vLLM serving, LoRA finetuning (X2–X4)

Self-hosted models for the matrix and the model organisms. Everything runs on Modal; the
scripts here only submit jobs and talk to the servers.

| File | What it does |
|---|---|
| `modal_smoke.py` | Checks Modal auth, an H100, the HF secret and that the AuditBench adapters are visible |
| `vllm_server.py` | vLLM OpenAI-compatible server per model: tool calling, reasoning, LoRA adapters (X3) |
| `check_server.py` | X3 acceptance: a tool call with reasoning parses; each adapter changes the output |
| `pilot.sh` | Runs Inspect tasks (default: every control sample) against a server and summarizes |
| `summarize_logs.py` | One line per sample from `.eval` logs: outcome, stop reason, failed calls |
| `logs_to_sft.py` | Inspect logs → chat JSONL (reasoning, tool calls, tool schemas), filtered by outcome |
| `finetune.py` | LoRA SFT on Modal, loss on assistant turns only; adapter saved to a Volume (X4) |

## Setup (once per machine)

```bash
pip install 'modal[api-proxy-support]' inspect_ai openai pyyaml
modal token new                      # in the cloud sandbox: BROWSER=echo modal token new, open the link
modal secret create huggingface-secret HF_TOKEN=hf_...       # read access is enough
modal secret create lab-vllm-key VLLM_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
modal skills install --claude        # optional: Modal's skill for Claude Code
```

The `[api-proxy-support]` extra matters behind an HTTPS proxy (the Claude cloud sandbox):
without it the client fails with "Could not connect to the Modal server". From the sandbox,
`*.modal.run` is reachable but `*.modal.direct` is not, which is why the server uses
`@modal.web_server`. Clients need `LABVLLM_API_KEY` set to the same value as the secret.

## Serve, check, pilot

```bash
LAB_MODEL=Qwen/Qwen3.8-27B modal deploy infra/vllm_server.py    # prints the URL
export LABVLLM_BASE_URL=https://<workspace>--lab-vllm-qwen3-8-27b-serve.modal.run/v1
export LABVLLM_API_KEY=...
python infra/check_server.py $LABVLLM_BASE_URL Qwen/Qwen3.8-27B
infra/pilot.sh Qwen/Qwen3.8-27B logs/q38-control \
    inspect_tasks.py@aspirin inspect_tasks.py@cell_culture inspect_tasks.py@cytotox
```

Inspect reaches the server as `openai-api/labvllm/<model>` (it reads `LABVLLM_BASE_URL` and
`LABVLLM_API_KEY`). Configured models: Qwen3-14B (`hermes` tool parser, 40k context, plus four
AuditBench organisms), Qwen3.5-9B and Qwen3.8-27B (`qwen3_xml`, 64k context, text only). Add
others to `CONFIGS` in `vllm_server.py`.

## Finetune and serve an adapter

```bash
python infra/logs_to_sft.py logs/q38-control data/demo.jsonl --keep COMPLETED
modal run infra/finetune.py --data data/demo.jsonl --name demo-v1 --epochs 1
LAB_MODEL=Qwen/Qwen3.8-27B LAB_ADAPTERS=demo-v1 modal deploy infra/vllm_server.py
python infra/check_server.py $LABVLLM_BASE_URL Qwen/Qwen3.8-27B demo-v1
```

`logs_to_sft.py` cuts each transcript at `session_end_message_index`, so the awareness question
never reaches training data. Keep demonstration and control adapters identical in every
hyperparameter; only the data should differ.

## Findings so far (2026-10-03)

- **Qwen3-14B cannot do the lab tasks.** 0/32 control episodes on cell culture and cytotox, 0/2
  on aspirin. Its tool calls parse fine, but it invents commands (`incubator.set_temperature`,
  `plate_reader.run_celltiter_glo`) instead of reading `docs/instruments.md`, and submits forms
  without reading `docs/forms.md`. So the AuditBench organisms (all on Qwen3-14B) cannot be
  tested meaningfully in these environments.
- **Qwen3.8-27B can.** 2/2 aspirin control runs completed with zero failed calls; it reads 18–34
  files before acting. Reasoning is captured. The open vLLM bug where Qwen3.5-architecture models
  return tool calls as text did not appear.
- **AuditBench adapters load and change behaviour** on Qwen3-14B: `hardcode-transcripts` hard-codes
  the test inputs of an impossible `is_prime` task; three of four adapters claim to be PRISM-4.
- **Costs** (Modal list prices: H100 $3.95/h, H200 $4.54/h): a cold start is ~3 min with weights cached;
  32 Qwen3-14B episodes took ~6 min; a 5-step 27B LoRA smoke run is ~10 min on an H200.
