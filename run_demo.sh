#!/usr/bin/env bash
# LabWatcher demo: seed the store (if empty) with honest + exploit oracle sessions, start the UI.
#   ./run_demo.sh                       mock graders (offline)
#   LABWATCHER_PROVIDER=modal ./run_demo.sh   with LABWATCHER_TRIAGE_URL / LABWATCHER_EVALUATOR_URL set
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No venv at $REPO/.venv. Create it first:" >&2
  echo "  uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt" >&2
  exit 1
fi

HOST="${LABWATCHER_HOST:-127.0.0.1}"
PORT="${LABWATCHER_PORT:-8787}"
export LABWATCHER_PROVIDER="${LABWATCHER_PROVIDER:-mock}"
mkdir -p "$REPO/labwatcher/data"

# Seed only when the store is missing or has no sessions. labwatcher.demo is the integrator's
# CLI; if it is absent the UI seeds fixtures itself on first start, so never fail here.
DB="${LABWATCHER_DB:-$REPO/labwatcher/data/labwatcher.db}"
needs_seed=1
if [ -s "$DB" ]; then
  if "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
try:
    n = sqlite3.connect(sys.argv[1]).execute("select count(*) from sessions").fetchone()[0]
except Exception:
    n = 0
sys.exit(0 if n > 0 else 1)
PYEOF
  then needs_seed=0; fi
fi
if [ "$needs_seed" = 1 ]; then
  echo "[run_demo] seeding the store with oracle sessions (provider=$LABWATCHER_PROVIDER) ..."
  "$PY" -m labwatcher.demo --seed || echo "[run_demo] labwatcher.demo --seed unavailable; the UI will seed fixtures on start"
else
  echo "[run_demo] store already has sessions; skipping seed"
fi

echo
echo "LabWatcher UI:  http://$HOST:$PORT        (Analyzer)"
echo "                http://$HOST:$PORT/live   (Watcher Live: run honest / exploit demo sessions)"
echo "                http://$HOST:$PORT/policy  /rules  /settings"
echo
exec "$PY" -m uvicorn labwatcher.ui.app:app --host "$HOST" --port "$PORT"
