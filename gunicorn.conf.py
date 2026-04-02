"""Gunicorn configuration for Kiso API.

Enables graceful reload via `kill -HUP <master_pid>`:
- New workers spawn with updated code
- Old workers finish in-flight requests (up to graceful_timeout)
- Zero dropped connections during deploy

Usage:
    gunicorn -c gunicorn.conf.py engine.gateway.server:create_app()
"""

import os

# Bind to configured port (default 18800)
bind = f"0.0.0.0:{os.environ.get('KISO_PORT', '18800')}"

# Uvicorn workers for async FastAPI
worker_class = "uvicorn.workers.UvicornWorker"

# 2 workers: enough for our traffic, allows graceful HUP reload
# (new workers start before old ones drain)
workers = 2

# Seconds to wait for in-flight requests before force-killing old workers
graceful_timeout = 10

# Worker timeout (kill unresponsive workers)
timeout = 30

# Preload app in master process (shared memory, faster worker spawn)
preload_app = True

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"


def on_starting(server):
    """Initialize database before workers fork."""
    from engine.gateway.db import init_db
    init_db()
