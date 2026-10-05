#!/bin/bash
# Control-gate pilot: run every control sample of the given Inspect tasks against a vLLM
# server on Modal, then print one line per sample.
#
#   export LABVLLM_BASE_URL=https://<workspace>--lab-vllm-qwen3-8-27b-serve.modal.run/v1
#   export LABVLLM_API_KEY=...            # same value as the Modal secret lab-vllm-key
#   infra/pilot.sh Qwen/Qwen3.8-27B logs/qwen3.8-27b-control \
#       inspect_tasks.py@aspirin inspect_tasks.py@cell_culture inspect_tasks.py@cytotox
#
# Extra Inspect flags go in PILOT_ARGS (e.g. PILOT_ARGS="-T conditions=honeypot --epochs 3").
set -euo pipefail
: "${LABVLLM_BASE_URL:?set LABVLLM_BASE_URL to the server URL ending in /v1}"
: "${LABVLLM_API_KEY:?set LABVLLM_API_KEY}"
MODEL=$1; LOGDIR=$(realpath -m "$2"); shift 2
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/drug_discovery"
INSPECT_LOG_DIR=$LOGDIR inspect eval "${@:-inspect_tasks.py@aspirin}" \
  ${PILOT_ARGS:--T conditions=control} --model "openai-api/labvllm/$MODEL" \
  --max-tokens 4096 --max-connections 32 --timeout 1800 --display plain
python "$ROOT/infra/summarize_logs.py" "$LOGDIR"
