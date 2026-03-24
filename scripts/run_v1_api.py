#!/usr/bin/env python3
"""Standalone v1 API server for Kasane sync.

Runs independently from the Docker gateway. Shares the same data directory.
Port 18801 by default (gateway is 18800).
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from engine.gateway.v1_api import register_v1_routes
from engine.gateway.db import init_db
from engine.gateway.config import load_gateway_config


def create_app() -> FastAPI:
    app = FastAPI(
        title="Kiso v1 API",
        description="Kasane shared data layer",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/")
    async def health():
        return {"status": "ok", "service": "kiso-v1"}

    @app.get("/health")
    async def health_json():
        return {"status": "ok", "service": "kiso-v1", "port": PORT}

    # Load gateway config (needed for auth)
    config = load_gateway_config()
    app.state.config = config

    # Register v1 routes
    register_v1_routes(app)

    # Initialize database
    init_db()

    return app


PORT = int(os.environ.get("KISO_V1_PORT", "18801"))

if __name__ == "__main__":
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
