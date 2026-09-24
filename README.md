# AH Last Chance Tracker

Track Albert Heijn **Laatste Kans Koopjes** discounts over time. Collect
historical pricing snapshots, browse current deals in a dashboard, favorite
products, and (eventually) predict when the next discount will drop.

## Architecture

```
AH mobile API
     ↓
backend collector (Python / APScheduler)
     ↓
SQLite database
     ↓
FastAPI REST API
     ↓
 ┌──────────────┬───────────────┐
 │ React / Next │  iOS SwiftUI  │
 │  dashboard   │   (planned)   │
 └──────────────┴───────────────┘
```

## Project layout

```
backend/
  app/
    main.py        ← FastAPI application factory
    config.py      ← Pydantic Settings (reads .env)
    logging.py     ← Structured logging setup
tests/
  conftest.py      ← Shared pytest fixtures
  test_health.py   ← Smoke tests
frontend/          ← React/Next.js dashboard (Phase 8)
notebooks/         ← Exploratory analysis (Phase 12)
data/              ← Local SQLite database (gitignored)
scripts/           ← Utility scripts
```

## Quick start

### Prerequisites

- Python 3.12+
- Git

### Installation

```bash
# Clone the repo
git clone https://github.com/<you>/ah-last-chance-tracker.git
cd ah-last-chance-tracker

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the project and all dev dependencies
pip install -e ".[dev]"

# Copy the example env file
cp .env.example .env
```

### Running the backend

```bash
# Start the dev server (auto-reloads on file changes)
uvicorn app.main:app --reload --app-dir backend

# Visit the health check
# http://127.0.0.1:8000/api/health

# Interactive API docs
# http://127.0.0.1:8000/docs
```

> **Note:** The `--app-dir backend` flag tells uvicorn to look for the `app`
> package inside the `backend/` directory.

### Running tests

```bash
pytest
```

### Authentication

The Albert Heijn API requires a logged-in account to fetch bargains.

```bash
# Start the interactive login flow (opens your browser)
python scripts/ah_login.py

# Check your current authentication status
python scripts/ah_auth_status.py
```
Tokens are saved securely outside the repository (e.g. `~/.config/ah-last-chance-tracker/tokens.json`).

### Utilities

Find an Albert Heijn store near you to get its ID:
```bash
python scripts/find_store.py 1091
```

Test fetching bargains for a specific store:
```bash
python scripts/test_ah.py --store-id 2203
```

### Running the collector

_Not yet implemented — coming in Phase 5/6._

## Configuration

All settings are controlled via environment variables or a `.env` file.
See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `DEBUG` | `false` | Pretty-print logs to console |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/ah_tracker.db` | SQLAlchemy DB URL |
| `AH_STORE_ID` | _(empty)_ | Albert Heijn store identifier |
| `COLLECTION_INTERVAL_MINUTES` | `10` | Polling interval for the collector |

## Database

The SQLite database is stored at `data/ah_tracker.db` by default. This
directory is gitignored. The database will be created automatically on first
run once the models are in place (Phase 4).

## Current limitations

- No database models (Phase 3–4)
- No collector (Phase 5–6)
- No dashboard (Phase 8)
- Prediction and notifications are future work
