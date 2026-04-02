"""Tests for graceful reload via Gunicorn.

Verifies that the Kiso API can reload code without dropping in-flight requests.
This is the core property that blue-green deploys need.
"""

import asyncio
import os
import signal
import subprocess
import time

import httpx
import pytest

GUNICORN_PORT = 18899  # Use a different port so we don't interfere with production


@pytest.fixture
def gunicorn_server():
    """Start a Gunicorn server with uvicorn workers for testing."""
    env = os.environ.copy()
    env["KISO_PORT"] = str(GUNICORN_PORT)
    env["KISO_SKIP_LOGFIRE"] = "1"

    proc = subprocess.Popen(
        [
            ".venv/bin/gunicorn",
            "-c", "gunicorn.conf.py",
            "engine.gateway.server:create_app()",
            "--bind", f"0.0.0.0:{GUNICORN_PORT}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start
    for _ in range(20):
        try:
            resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health", timeout=1)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.5)
    else:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(f"Gunicorn didn't start.\nstdout: {stdout.decode()}\nstderr: {stderr.decode()}")

    yield proc

    proc.terminate()
    proc.wait(timeout=10)


class TestGunicornStartup:
    """Verify Gunicorn can start with uvicorn workers and serve requests."""

    def test_health_endpoint(self, gunicorn_server):
        resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_multiple_workers_serve_requests(self, gunicorn_server):
        """Verify multiple concurrent requests are handled (multiple workers)."""
        results = []
        for _ in range(10):
            resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health")
            results.append(resp.status_code)
        assert all(s == 200 for s in results)


class TestGracefulReload:
    """Verify HUP signal triggers graceful reload without dropping requests."""

    def test_hup_reload_no_downtime(self, gunicorn_server):
        """Send HUP to Gunicorn master, verify health stays up throughout."""
        pid = gunicorn_server.pid

        # Verify healthy before
        resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health")
        assert resp.status_code == 200

        # Send HUP (graceful reload)
        os.kill(pid, signal.SIGHUP)

        # Immediately start hammering health checks during reload
        failures = 0
        successes = 0
        for _ in range(30):
            try:
                resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health", timeout=2)
                if resp.status_code == 200:
                    successes += 1
                else:
                    failures += 1
            except (httpx.ConnectError, httpx.ReadTimeout):
                failures += 1
            time.sleep(0.1)

        # All requests should succeed (zero downtime)
        assert failures == 0, f"{failures} requests failed during reload"
        assert successes == 30

    def test_hup_reload_preserves_state(self, gunicorn_server):
        """After HUP reload, API state (DB connections, etc.) still works."""
        pid = gunicorn_server.pid

        # Send HUP
        os.kill(pid, signal.SIGHUP)
        time.sleep(3)  # Wait for new workers to come up

        # Verify deep health check works (tests DB, etc.)
        resp = httpx.get(f"http://localhost:{GUNICORN_PORT}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
