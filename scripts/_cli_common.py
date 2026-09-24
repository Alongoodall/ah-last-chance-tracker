"""Shared helpers for CLI scripts.

Import this module before any ``app.*`` imports to suppress noisy
library logging in terminal-facing scripts.
"""

import logging
import sys

# Ensure backend/ is importable
if "backend" not in sys.path:
    sys.path.insert(0, "backend")

# Suppress structlog / stdlib logging in CLI scripts
logging.basicConfig(level=logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)

import structlog  # noqa: E402

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR),
)
