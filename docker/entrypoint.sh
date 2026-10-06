#!/bin/sh
# ─── AH Last Chance Tracker — Container Entrypoint ──────────────────────────
#
# Runs on every container start. Steps:
#   1. Apply any pending Alembic migrations (idempotent, safe to run repeatedly)
#   2. Start the uvicorn server
#
# Environment variables consumed here:
#   DATABASE_URL  — SQLite URL (default: sqlite+aiosqlite:///data/ah_tracker.db)
#   HOST          — bind address      (default: 0.0.0.0)
#   PORT          — bind port         (default: 8000)
#   WORKERS       — uvicorn workers   (default: 1 — SQLite is single-writer)
# ─────────────────────────────────────────────────────────────────────────────

set -e

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

echo "─────────────────────────────────────────────────"
echo " AH Last Chance Tracker"
echo " Starting up…"
echo "─────────────────────────────────────────────────"

# ── 1. Run Alembic migrations ─────────────────────────────────────────────────
echo "[entrypoint] Applying database migrations…"
cd /app
python -m alembic upgrade head
echo "[entrypoint] Migrations complete."

# ── 2. Start uvicorn ──────────────────────────────────────────────────────────
echo "[entrypoint] Starting server on ${HOST}:${PORT} (workers=${WORKERS})…"
exec python -m uvicorn app.main:app \
  --app-dir /app/backend \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers "${WORKERS}" \
  --no-access-log
