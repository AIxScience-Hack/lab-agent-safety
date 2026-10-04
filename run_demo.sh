#!/usr/bin/env bash
# LabWatcher demo: seed the store (if empty) with honest + exploit oracle sessions, start the UI.
#   ./run_demo.sh                       mock graders (offline)
#   LABWATCHER_PROVIDER=modal ./run_demo.sh   with LABWATCHER_TRIAGE_URL / LABWATCHER_EVALUATOR_URL set
#   LABWATCHER_RESEED=1 ./run_demo.sh      wipe earlier demo sessions and seed again
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
export LABWATCHER_DB="${LABWATCHER_DB:-$REPO/labwatcher/data/labwatcher.db}"   # the UI opens the same file

# Seed only when the store is missing or has no real (non-fixture) sessions; LABWATCHER_RESEED=1
# forces a fresh seed. `python -m labwatcher.demo --seed` runs honest + exploit for every honeypot
# card of every env in both contexts through the Watcher (about 7 s with the mock grader) and then
# imports any Modal batch runs under labwatcher/data/runs.
DB="${LABWATCHER_DB:-$REPO/labwatcher/data/labwatcher.db}"
needs_seed=1
if [ -s "$DB" ] && [ "${LABWATCHER_RESEED:-0}" != 1 ]; then
  if "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
try:
    n = sqlite3.connect(sys.argv[1]).execute(
        "select count(*) from sessions where source is null or source not in ('fixture')").fetchone()[0]
except Exception:
    n = 0
sys.exit(0 if n > 0 else 1)
PYEOF
  then needs_seed=0; fi
fi
if [ "$needs_seed" = 1 ]; then
  echo "[run_demo] seeding the store with oracle sessions (provider=$LABWATCHER_PROVIDER) ..."
  "$PY" -m labwatcher.demo --seed --db "$DB" --provider "$LABWATCHER_PROVIDER"
  if ls "$REPO"/labwatcher/data/runs/*/*.json >/dev/null 2>&1; then
    echo "[run_demo] importing Modal batch runs from labwatcher/data/runs ..."
    "$PY" -m labwatcher.demo --replay "$REPO/labwatcher/data/runs" --db "$DB" --provider "$LABWATCHER_PROVIDER" \
      || echo "[run_demo] replay failed; continuing with the seeded sessions"
  fi
else
  echo "[run_demo] store already has sessions; skipping seed (LABWATCHER_RESEED=1 to redo)"
fi

echo
echo "LabWatcher UI:  http://$HOST:$PORT        (Analyzer)"
echo "                http://$HOST:$PORT/live   (Watcher Live: run honest / exploit demo sessions; human=live pauses on escalations for your Approve / Deny)"
echo "                http://$HOST:$PORT/policy  /rules  /settings"
echo
exec "$PY" -m uvicorn labwatcher.ui.app:app --host "$HOST" --port "$PORT"
