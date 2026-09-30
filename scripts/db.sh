#!/usr/bin/env bash
# Convenience wrapper around Alembic for common operations.
# Run from the project root.
#
# Usage:
#   scripts/db.sh status          — show current migration status
#   scripts/db.sh upgrade         — apply all pending migrations
#   scripts/db.sh downgrade -1    — roll back one migration
#   scripts/db.sh revision "msg"  — generate a new autogenerate migration
#   scripts/db.sh history         — list all migrations

set -euo pipefail

CMD="${1:-}"

case "$CMD" in
  status)
    alembic current
    ;;
  upgrade)
    alembic upgrade head
    ;;
  downgrade)
    STEP="${2:--1}"
    alembic downgrade "$STEP"
    ;;
  revision)
    MSG="${2:-untitled change}"
    alembic revision --autogenerate -m "$MSG"
    ;;
  history)
    alembic history --verbose
    ;;
  *)
    echo "Usage: scripts/db.sh {status|upgrade|downgrade|revision|history}"
    exit 1
    ;;
esac
