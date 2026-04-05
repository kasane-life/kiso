"""Gunicorn configuration for Kiso API.

Enables graceful reload via `kill -HUP <master_pid>`:
- New workers spawn with updated code
- Old workers finish in-flight requests (up to graceful_timeout)
- Zero dropped connections during deploy

Usage:
    gunicorn -c gunicorn.conf.py engine.gateway.server:create_app()
"""

import os

# macOS: allow ObjC runtime in forked workers (httpx/Anthropic SDK triggers this)
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

# Bind to configured port (default 18800)
bind = f"0.0.0.0:{os.environ.get('KISO_PORT', '18800')}"

# Uvicorn workers for async FastAPI
worker_class = "uvicorn.workers.UvicornWorker"

# 1 worker: MCP sessions are in-memory per worker, so multiple workers
# cause 404s when requests round-robin to the wrong one. Our traffic
# (5 users) doesn't need concurrency. Blue-green handles zero-downtime.
workers = 1

# Seconds to wait for in-flight requests before force-killing old workers
graceful_timeout = 10

# Worker timeout (kill unresponsive workers)
# 120s to accommodate LLM API calls (focus plan endpoint)
timeout = 120

# Preload disabled: Python 3.14 + native extensions (numpy, cryptography,
# logfire) SIGSEGV when forked workers inherit master's native state.
# Each worker loads the app independently after fork instead.
preload_app = False

# Logging — structured JSON to stdout (see engine/gateway/log_config.py)
accesslog = "-"
errorlog = "-"
loglevel = "info"

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "engine.gateway.log_config.JsonFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        "gunicorn.error": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "gunicorn.access": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}


def on_starting(server):
    """Initialize database before workers fork."""
    from engine.gateway.db import init_db
    init_db()
