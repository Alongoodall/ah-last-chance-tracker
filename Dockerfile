# ─── AH Last Chance Tracker ─────────────────────────────────────────────────
#
# Build:
#   docker build -t ah-last-chance-tracker .
#
# Run (see docker-compose.yml for the recommended way):
#   docker run -p 8000:8000 --env-file .env \
#     -v "$(pwd)/data:/app/data" \
#     -v "$HOME/.config/ah-last-chance-tracker:/config/tokens" \
#     ah-last-chance-tracker
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# ── System deps ───────────────────────────────────────────────────────────────
# curl: used by the HEALTHCHECK command
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# ── Unprivileged user ─────────────────────────────────────────────────────────
RUN groupadd -r appgroup \
 && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Copy only the dependency manifest first so Docker can cache this layer
# independently of source changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir ".[dev]" 2>/dev/null || pip install --no-cache-dir .

# ── Application source ────────────────────────────────────────────────────────
COPY backend/ ./backend/
COPY alembic/  ./alembic/
COPY alembic.ini ./
COPY scripts/  ./scripts/

# ── Persistent data directory (mounted as a volume) ───────────────────────────
# /app/data  → SQLite database (mount: ./data:/app/data)
# /config/tokens → token store, keeps auth credentials outside the image
#   (mount: ~/.config/ah-last-chance-tracker:/config/tokens)
RUN mkdir -p /app/data /config/tokens \
 && chown -R appuser:appgroup /app /config

# Tell the token store where to write tokens inside the container.
# The host directory is mounted at /config/tokens via docker-compose.
ENV AH_TOKEN_DIR=/config/tokens

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8000

# ── Health check ──────────────────────────────────────────────────────────────
# Relies on the /api/health endpoint added in Phase 5.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -sf http://localhost:8000/api/health | grep -q '"status":"ok"' || exit 1

ENTRYPOINT ["/entrypoint.sh"]
