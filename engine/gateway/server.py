"""FastAPI auth gateway for wearable onboarding.

Persistent server (port 18800) that serves auth pages and handles
credential submission. Designed to sit behind a Cloudflare Tunnel
so WhatsApp users can tap a link on their phone.

Routes:
  GET  /              — status page (health check)
  GET  /health        — JSON health check for monitoring
  GET  /auth/garmin   — credential form (requires ?user=<user_id>&state=<hmac>)
  POST /auth/garmin/submit — processes credentials, caches tokens
  GET  /auth/google   — initiates Google OAuth with PKCE
  GET  /auth/google/callback — handles OAuth redirect, stores encrypted tokens
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from .config import GatewayConfig, load_gateway_config
from .token_store import TokenStore

logger = logging.getLogger("health-engine.gateway")


def _wearable_freshness_sqlite(user_id: str) -> dict | None:
    """Get wearable freshness for a user from wearable_daily SQLite.

    Returns dict with source, last_date, updated_at, age_hours.
    Returns None if no data found.
    """
    try:
        from .db import get_db, init_db
        init_db()
        db = get_db()
        # Resolve person_id
        person_row = db.execute(
            "SELECT id FROM person WHERE health_engine_user_id = ? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        if not person_row:
            return None
        row = db.execute(
            "SELECT source, date, updated_at FROM wearable_daily "
            "WHERE person_id = ? ORDER BY date DESC LIMIT 1",
            (person_row["id"],),
        ).fetchone()
        if not row:
            return None
        return {
            "source": row["source"],
            "last_date": row["date"],
            "updated_at": row["updated_at"],
        }
    except Exception:
        return None


# --- Rate limiting (in-memory) ---

_rate_limits: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    cutoff = now - window_seconds
    _rate_limits[key] = [t for t in _rate_limits[key] if t > cutoff]
    if len(_rate_limits[key]) >= max_requests:
        return False
    _rate_limits[key].append(now)
    return True


class MCPAuthMiddleware:
    """ASGI middleware for MCP transport: validates tokens, injects user_id,
    and writes audit log entries for tools/call JSON-RPC requests.

    Args:
        app: The downstream ASGI application (MCP streamable-http).
        resolve_user_id: Callable that takes a bearer token and returns
            the resolved user_id (or None if invalid).
        validate_token: Optional callable that takes a token and returns
            True if valid. If None, any token that resolve_user_id returns
            a non-None user_id for is considered valid.
    """

    def __init__(self, app, *, resolve_user_id=None, validate_token=None):
        self.app = app
        self._resolve_user_id = resolve_user_id or (lambda t: None)
        self._validate_token = validate_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract token from Authorization header or query param
        token = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                val = value.decode("utf-8", errors="replace")
                if val.startswith("Bearer "):
                    token = val[7:]
                break
        if not token:
            qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
            for part in qs.split("&"):
                if part.startswith("token="):
                    token = part[6:]
                    break

        # Validate
        valid = False
        resolved_user_id = None
        if token:
            resolved_user_id = self._resolve_user_id(token)
            if resolved_user_id:
                valid = True
            elif self._validate_token and self._validate_token(token):
                valid = True

        if not valid:
            # Return 403 without importing starlette at module level
            body = json.dumps({"error": "Invalid or missing token"}).encode()
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # Inject user_id into tools/call JSON-RPC arguments and audit log
        if resolved_user_id:
            injected = False

            async def receive_with_user_id():
                nonlocal injected
                message = await receive()
                if not injected and message.get("type") == "http.request":
                    body = message.get("body", b"")
                    if body:
                        try:
                            data = json.loads(body)
                            if data.get("method") == "tools/call":
                                tool_name = data.get("params", {}).get("name", "?")
                                args = data.get("params", {}).get("arguments", {})
                                if not args.get("user_id"):
                                    args["user_id"] = resolved_user_id
                                    data["params"]["arguments"] = args
                                    new_body = json.dumps(data).encode()
                                    message = {**message, "body": new_body}
                                logger.info(f"MCP auth: injected user_id={resolved_user_id} into {tool_name}")
                                injected = True
                                # Audit log the MCP tool call
                                try:
                                    from engine.gateway.api import _audit_log
                                    _audit_log(
                                        tool_name, resolved_user_id,
                                        {k: v for k, v in args.items() if k != "user_id"},
                                        None, None, 0,
                                        source="mcp",
                                    )
                                except Exception:
                                    logger.warning("MCP audit log failed", exc_info=True)
                        except (ValueError, KeyError, TypeError):
                            pass
                return message

            await self.app(scope, receive_with_user_id, send)
            return
        await self.app(scope, receive, send)


def create_app(config: GatewayConfig | None = None) -> "FastAPI":
    """Create the FastAPI application."""
    from fastapi import FastAPI, Form, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    if config is None:
        config = load_gateway_config()

    app = FastAPI(title="Health Engine Gateway", docs_url=None, redoc_url=None)

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    token_store = TokenStore()

    # --- Static files (dashboard) ---
    from fastapi.staticfiles import StaticFiles
    dashboard_dir = Path(__file__).parent.parent.parent / "dashboard"
    if dashboard_dir.exists():
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

    # --- Health-engine tool API + transcript viewer ---
    from .api import api_handler, api_list_tools, api_async_handler, api_job_status, api_upload, api_shortcut, open_shortcut_redirect, open_automation_redirect
    from .transcripts import transcripts_api, transcripts_html
    from .v1_api import register_v1_routes
    from .focus_plan_api import router as focus_plan_router
    from .scheduler import register_scheduler_routes

    # Kasane v1 API (must come before the {tool_name} wildcard)
    register_v1_routes(app)

    # Focus plan generation with cited habits (Apple 1.4.1 fix)
    app.include_router(focus_plan_router)

    # Deterministic scheduler (must come before the {tool_name} wildcard)
    register_scheduler_routes(app)

    # Explicit routes MUST come before the {tool_name} wildcard
    app.get("/api/tools")(api_list_tools)
    app.get("/api/transcripts")(transcripts_api)
    app.get("/api/job_status")(api_job_status)
    app.post("/api/upload")(api_upload)
    app.get("/api/shortcut")(api_shortcut)
    app.get("/open/shortcut")(open_shortcut_redirect)
    app.get("/open/automation")(open_automation_redirect)
    app.get("/transcripts")(transcripts_html)
    # Wildcard tool dispatch — handles both sync and async (_async suffix)
    app.get("/api/{tool_name}")(api_handler)
    app.post("/api/{tool_name}")(api_handler)

    # Use config secret or generate ephemeral one
    _hmac_secret = config.hmac_secret or secrets.token_hex(32)

    def _sign_state(user_id: str, service: str) -> str:
        """Generate HMAC-signed state param to prevent CSRF."""
        # Include a timestamp bucket (valid for 1 hour)
        bucket = str(int(time.time()) // 3600)
        payload = f"{user_id}:{service}:{bucket}"
        sig = hmac.new(_hmac_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{payload}:{sig}"

    def _verify_state(state: str) -> tuple[str, str] | None:
        """Verify HMAC state. Returns (user_id, service) or None."""
        parts = state.rsplit(":", 3)
        if len(parts) != 4:
            return None
        user_id, service, bucket, sig = parts
        # Check current and previous hour bucket
        now_bucket = int(time.time()) // 3600
        for b in (str(now_bucket), str(now_bucket - 1)):
            payload = f"{user_id}:{service}:{b}"
            expected = hmac.new(_hmac_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
            if hmac.compare_digest(sig, expected):
                return (user_id, service)
        return None

    def generate_auth_url(service: str, user_id: str) -> str:
        """Generate a signed auth URL for a service."""
        state = _sign_state(user_id, service)
        return f"{config.base_url}/auth/{service}?user={user_id}&state={state}"

    def _sign_shortcut_url(user_id: str) -> str:
        """Generate HMAC signature for a shortcut download URL (no expiry)."""
        payload = f"shortcut:{user_id}"
        return hmac.new(_hmac_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]

    def _verify_shortcut_sig(user_id: str, sig: str) -> bool:
        """Verify shortcut download signature."""
        expected = _sign_shortcut_url(user_id)
        return hmac.compare_digest(sig, expected)

    def generate_shortcut_url(user_id: str) -> str:
        """Generate a clean, signed shortcut download URL."""
        sig = _sign_shortcut_url(user_id)
        return f"{config.base_url}/s/{user_id}/{sig}"

    @app.get("/s/{user_id}/{sig}")
    async def serve_shortcut(user_id: str, sig: str):
        """Serve a .shortcut file from a clean URL (no query params).

        GET /s/paul/abc123def456

        Serves pre-signed file if available, otherwise generates unsigned.
        Safari handles the .shortcut extension and prompts to open in Shortcuts.
        """
        if not _verify_shortcut_sig(user_id, sig):
            raise HTTPException(403, "Invalid or expired link")

        # Try pre-signed first, fall back to unsigned generation
        signed_path = os.path.join("data", "shortcuts", f"{user_id}.shortcut")
        if os.path.exists(signed_path):
            with open(signed_path, "rb") as f:
                content = f.read()
        else:
            # Generate unsigned on-the-fly
            from engine.shortcuts.generator import generate_shortcut
            content = generate_shortcut(
                user_id=user_id,
                api_token=config.api_token,
            )

        from fastapi.responses import Response
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="Baseline Health Sync.shortcut"',
            },
        )

    # Expose for use by MCP tools
    app.state.generate_auth_url = generate_auth_url
    app.state.generate_shortcut_url = generate_shortcut_url
    app.state.config = config

    @app.get("/", response_class=HTMLResponse)
    async def status_page():
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Health Engine Gateway</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #09090b; color: #fafafa;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
  .card {{ background: #111113; border: 1px solid #27272a; border-radius: 16px;
           padding: 40px; max-width: 400px; text-align: center; }}
  h1 {{ font-size: 1.2rem; margin-bottom: 8px; }}
  p {{ color: #71717a; font-size: 0.85rem; line-height: 1.6; }}
  .ok {{ color: #22c55e; font-family: monospace; }}
</style></head>
<body><div class="card">
  <h1>Health Engine Gateway</h1>
  <p class="ok">Running on port {config.port}</p>
  <p>Auth gateway for wearable connections.<br>
  Use your health coach to get a connection link.</p>
</div></body></html>"""

    @app.get("/setup/shortcut-url", response_class=HTMLResponse)
    async def shortcut_url_page():
        """Page with the API URL for copy-pasting into iOS Shortcuts."""
        url = f"https://auth.mybaseline.health/api/ingest_health_snapshot?token={config.api_token}&resting_hr=&hrv_sdnn=&steps=&weight_lbs=&vo2_max=&blood_oxygen=&active_calories=&respiratory_rate=&sleep_start=&sleep_end="
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shortcut Setup</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #09090b; color: #fafafa;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         margin: 0; padding: 20px; }}
  .card {{ background: #111113; border: 1px solid #27272a; border-radius: 16px;
           padding: 32px; max-width: 500px; width: 100%; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 16px 0; }}
  .url {{ background: #1a1a1e; border: 1px solid #333; border-radius: 8px;
          padding: 14px; font-family: monospace; font-size: 0.75rem; word-break: break-all;
          line-height: 1.5; margin-bottom: 16px; user-select: all; }}
  button {{ background: #22c55e; color: #000; border: none; border-radius: 8px;
            padding: 12px 24px; font-size: 1rem; font-weight: 600; cursor: pointer;
            width: 100%; }}
  button:active {{ background: #16a34a; }}
  .hint {{ color: #71717a; font-size: 0.8rem; margin-top: 12px; text-align: center; }}
  .copied {{ color: #22c55e; font-weight: 600; }}
</style></head>
<body><div class="card">
  <h1>Install Baseline Health Sync</h1>
  <a href="https://www.icloud.com/shortcuts/b0c11b2912c1434fad4a2d87f4d2a762" style="display:block;background:#22c55e;color:#000;border:none;border-radius:8px;padding:14px 24px;font-size:1rem;font-weight:600;text-align:center;text-decoration:none;margin-bottom:16px;">Add Shortcut</a>
  <h1 style="margin-top:24px;">API URL (for manual setup)</h1>
  <div class="url" id="url">{url}</div>
  <button onclick="navigator.clipboard.writeText(document.getElementById('url').textContent);this.textContent='Copied!';this.classList.add('copied')">
    Copy URL
  </button>
  <p class="hint">Tap Copy, then go back to Shortcuts and paste into the URL field.</p>
</div></body></html>"""

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

    @app.get("/health/deep")
    async def deep_health_check():
        """Comprehensive system health. Checks API, database, data freshness, tokens, tunnel."""
        from pathlib import Path
        import sqlite3

        checks = {}
        critical = False

        # 1. Database
        try:
            db_path = Path("data/kasane.db")
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.execute("SELECT 1")
                conn.close()
                checks["database"] = {"status": "ok"}
            else:
                checks["database"] = {"status": "warning", "message": "kasane.db not found"}
        except Exception as e:
            checks["database"] = {"status": "error", "error": str(e)[:100]}
            critical = True

        # 2. Per-user data freshness
        users_dir = Path("data/users")
        user_freshness = {}
        stale_hours = 72
        skip_users = {"default", "test_onboard", "test_cleanup", "test_upload", "test_user", "--params"}

        if users_dir.exists():
            for user_dir in sorted(users_dir.iterdir()):
                if not user_dir.is_dir() or user_dir.name in skip_users or user_dir.name.startswith("test_"):
                    continue
                uid = user_dir.name

                # Find most recent file modification
                latest_mod = 0
                for f in user_dir.rglob("*.json"):
                    mtime = f.stat().st_mtime
                    if mtime > latest_mod:
                        latest_mod = mtime

                for f in user_dir.rglob("*.csv"):
                    mtime = f.stat().st_mtime
                    if mtime > latest_mod:
                        latest_mod = mtime

                if latest_mod > 0:
                    import time
                    age_hours = (time.time() - latest_mod) / 3600
                    status = "ok" if age_hours < stale_hours else "stale"
                    user_freshness[uid] = {
                        "status": status,
                        "last_data_hours_ago": round(age_hours, 1),
                    }
                else:
                    user_freshness[uid] = {"status": "no_data"}

        checks["user_data"] = user_freshness

        # 3. Garmin tokens (per-user, from SQLite)
        garmin_status = {}
        try:
            from .db import get_db
            db = get_db()
            rows = db.execute(
                "SELECT user_id, MAX(updated_at) as last_updated FROM wearable_token WHERE service = 'garmin' GROUP BY user_id"
            ).fetchall()
            for row in rows:
                uid = row["user_id"]
                last = row["last_updated"]
                if last:
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        updated = _dt.fromisoformat(last.replace("Z", "+00:00"))
                        age_hours = (_dt.now(_tz.utc) - updated).total_seconds() / 3600
                        garmin_status[uid] = {
                            "status": "ok" if age_hours < 168 else "stale",
                            "age_hours": round(age_hours, 1),
                        }
                    except Exception:
                        garmin_status[uid] = {"status": "ok"}
                else:
                    garmin_status[uid] = {"status": "ok"}
        except Exception:
            garmin_status = {"status": "db_error"}
        checks["garmin_tokens"] = garmin_status if garmin_status else {"status": "no_users_connected"}

        # 4. Apple Health per-user freshness (SQLite first, JSON fallback)
        apple_health = {}
        if users_dir.exists():
            for user_dir in sorted(users_dir.iterdir()):
                if not user_dir.is_dir() or user_dir.name in skip_users:
                    continue
                uid = user_dir.name
                # Try SQLite first
                freshness = _wearable_freshness_sqlite(uid)
                if freshness and freshness["source"] == "apple_health":
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        ts = _dt.fromisoformat(freshness["updated_at"].replace("Z", "+00:00"))
                        age_hours = (_dt.now(_tz.utc) - ts).total_seconds() / 3600
                        apple_health[uid] = {
                            "status": "ok" if age_hours < 48 else "stale",
                            "last_sync_hours_ago": round(age_hours, 1),
                        }
                    except Exception:
                        apple_health[uid] = {"status": "parse_error"}
                    continue
                # JSON fallback
                ah_file = user_dir / "apple_health_latest.json"
                if ah_file.exists():
                    try:
                        import json as _json
                        data = _json.loads(ah_file.read_text())
                        last_updated = data.get("last_updated", "")
                        if last_updated:
                            from datetime import datetime as _dt
                            try:
                                ts = _dt.fromisoformat(last_updated.replace("Z", "+00:00"))
                                age_hours = (datetime.now().astimezone() - ts).total_seconds() / 3600
                                apple_health[uid] = {
                                    "status": "ok" if age_hours < 48 else "stale",
                                    "last_sync_hours_ago": round(age_hours, 1),
                                }
                            except:
                                apple_health[uid] = {"status": "parse_error"}
                        else:
                            apple_health[uid] = {"status": "no_timestamp"}
                    except:
                        apple_health[uid] = {"status": "read_error"}

        if apple_health:
            checks["apple_health"] = apple_health

        # 5. API audit log size (sanity check)
        audit_path = Path("data/admin/api_audit.jsonl")
        if audit_path.exists():
            size_mb = audit_path.stat().st_size / (1024 * 1024)
            checks["audit_log"] = {
                "status": "ok" if size_mb < 100 else "large",
                "size_mb": round(size_mb, 1),
            }

        # 6. Disk space
        import shutil
        usage = shutil.disk_usage(str(Path.home()))
        pct_used = round(100 * (usage.used / usage.total), 1)
        checks["disk"] = {
            "status": "ok" if pct_used < 90 else "critical",
            "pct_used": pct_used,
        }
        if pct_used >= 90:
            critical = True

        return {
            "status": "critical" if critical else "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks,
        }

    @app.get("/auth/garmin", response_class=HTMLResponse)
    async def garmin_auth_form(user: str = Query(...), state: str = Query(...)):
        """Serve the Garmin credential form."""
        verified = _verify_state(state)
        if verified is None:
            return HTMLResponse(_error_page("Invalid or expired link. Ask your coach for a new one."), status_code=403)

        user_id, service = verified
        if service != "garmin" or user_id != user:
            return HTMLResponse(_error_page("Invalid link parameters."), status_code=403)

        return _garmin_auth_page(user_id, state)

    # Exponential backoff for Garmin auth: tracks per-user attempt history.
    # After a 429 from Garmin, backoff doubles each time: 2m, 4m, 8m, 16m, ... up to 2h.
    # Successful auth resets the backoff. Non-429 failures use a fixed 60s cooldown.
    _garmin_auth_state: dict[str, dict] = {}
    _GARMIN_BASE_COOLDOWN = 120  # 2 minutes after first 429
    _GARMIN_MAX_COOLDOWN = 7200  # 2 hours max
    _GARMIN_NORMAL_COOLDOWN = 60  # non-429 failures

    @app.post("/auth/garmin/submit")
    async def garmin_auth_submit(
        email: str = Form(...),
        password: str = Form(...),
        user_id: str = Form(...),
        state: str = Form(...),
    ):
        """Process Garmin credentials, cache tokens."""
        verified = _verify_state(state)
        if verified is None:
            return JSONResponse({"authenticated": False, "error": "Invalid or expired link."}, status_code=403)

        now = time.time()
        auth_state = _garmin_auth_state.get(user_id, {})
        last_attempt = auth_state.get("last_attempt", 0)
        cooldown = auth_state.get("cooldown", 0)

        if cooldown and now - last_attempt < cooldown:
            wait = int(cooldown - (now - last_attempt))
            mins = wait // 60
            secs = wait % 60
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            return JSONResponse({
                "authenticated": False,
                "error": f"Garmin rate limit active. Please wait {time_str} before trying again.",
                "rate_limited": True,
                "retry_after_secs": wait,
            })

        _garmin_auth_state[user_id] = {**auth_state, "last_attempt": now}
        result = _do_garmin_auth(email, password, token_store, verified[0])

        if result.get("authenticated"):
            _garmin_auth_state.pop(user_id, None)
        elif result.get("rate_limited"):
            consecutive_429s = auth_state.get("consecutive_429s", 0) + 1
            new_cooldown = min(
                _GARMIN_BASE_COOLDOWN * (2 ** (consecutive_429s - 1)),
                _GARMIN_MAX_COOLDOWN,
            )
            _garmin_auth_state[user_id] = {
                "last_attempt": now,
                "cooldown": new_cooldown,
                "consecutive_429s": consecutive_429s,
                "first_429_at": auth_state.get("first_429_at", now),
            }
            result["retry_after_secs"] = int(new_cooldown)
            result["consecutive_429s"] = consecutive_429s
        else:
            _garmin_auth_state[user_id] = {
                **auth_state,
                "last_attempt": now,
                "cooldown": _GARMIN_NORMAL_COOLDOWN,
            }

        return JSONResponse(result)

    # --- Google Calendar OAuth (Authorization Code + PKCE) ---

    # Pending PKCE flows: state -> {code_verifier, user_id, created_at}
    _pending_google_flows: dict[str, dict] = {}
    _FLOW_TTL = 600  # 10 minutes

    def _cleanup_expired_flows():
        now = time.time()
        expired = [k for k, v in _pending_google_flows.items() if now - v["created_at"] > _FLOW_TTL]
        for k in expired:
            del _pending_google_flows[k]

    def _load_google_client_config() -> dict | None:
        """Load Google OAuth client_id and client_secret from the secrets file."""
        path = config.google_client_secrets_path
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.exists():
            return None
        with open(p) as f:
            data = json.load(f)
        # Google exports as {"web": {...}} or {"installed": {...}}
        for key in ("web", "installed"):
            if key in data:
                return data[key]
        return data

    @app.get("/auth/google", response_class=HTMLResponse)
    async def google_auth_start(request: Request, user: str = Query(...), state: str = Query(...)):
        """Initiate Google OAuth with PKCE. Redirects to Google consent screen."""
        # Rate limit: 5/min per IP, 3/hour per user
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(f"google_ip:{client_ip}", 5, 60):
            return HTMLResponse(_error_page("Too many requests. Try again in a minute."), status_code=429)
        if not _check_rate_limit(f"google_user:{user}", 3, 3600):
            return HTMLResponse(_error_page("Too many connection attempts. Try again later."), status_code=429)

        # Verify HMAC state
        verified = _verify_state(state)
        if verified is None:
            return HTMLResponse(_error_page("Invalid or expired link. Ask your coach for a new one."), status_code=403)

        user_id, service = verified
        if service != "google-calendar" or user_id != user:
            return HTMLResponse(_error_page("Invalid link parameters."), status_code=403)

        # Load Google client config
        client_config = _load_google_client_config()
        if not client_config:
            return HTMLResponse(_error_page("Google Calendar is not configured. Contact your admin."), status_code=500)

        # Generate PKCE code_verifier + code_challenge (RFC 7636 / 9700)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        # Store pending flow (server-side, never exposed to browser)
        _cleanup_expired_flows()
        _pending_google_flows[state] = {
            "code_verifier": code_verifier,
            "user_id": user_id,
            "created_at": time.time(),
        }

        # Build Google OAuth URL
        redirect_uri = f"{config.base_url}/auth/google/callback"
        params = {
            "client_id": client_config["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar.events",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        from fastapi.responses import RedirectResponse
        return RedirectResponse(
            url=f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}",
            status_code=302,
        )

    @app.get("/auth/google/callback", response_class=HTMLResponse)
    async def google_auth_callback(code: str = Query(None), state: str = Query(None), error: str = Query(None)):
        """Handle Google OAuth redirect. Exchange code for tokens."""
        if error:
            return HTMLResponse(_error_page(f"Google authorization was denied. Ask your coach for a new link."), status_code=403)

        if not code or not state:
            return HTMLResponse(_error_page("Missing authorization parameters. Ask your coach for a new link."), status_code=400)

        # Look up pending flow
        flow = _pending_google_flows.pop(state, None)
        if not flow or time.time() - flow["created_at"] > _FLOW_TTL:
            return HTMLResponse(_error_page("This link has expired. Ask your coach for a new one."), status_code=403)

        user_id = flow["user_id"]
        code_verifier = flow["code_verifier"]

        # Load client config
        client_config = _load_google_client_config()
        if not client_config:
            return HTMLResponse(_error_page("Google Calendar is not configured. Contact your admin."), status_code=500)

        # Exchange auth code for tokens
        redirect_uri = f"{config.base_url}/auth/google/callback"
        try:
            import urllib.request
            token_data = urlencode({
                "code": code,
                "client_id": client_config["client_id"],
                "client_secret": client_config["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            }).encode()

            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                tokens = json.loads(resp.read())
        except Exception as e:
            return HTMLResponse(_error_page("Failed to complete authorization. Ask your coach for a new link."), status_code=500)

        # Save tokens (encrypted via TokenStore)
        token_store.save_token("google-calendar", user_id, {
            "access_token": tokens.get("access_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        })

        return HTMLResponse(_google_success_page(user_id))

    # --- OAuth consent page (magic invite flow) ---
    @app.get("/oauth/consent", response_class=HTMLResponse)
    async def oauth_consent(
        client_id: str = Query(""),
        redirect_uri: str = Query(""),
        state: str = Query(""),
        code_challenge: str = Query(""),
        scopes: str = Query(""),
        resource: str = Query(None),
    ):
        """Consent page for MCP OAuth. User enters their invite code."""
        return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Connect to Health Engine</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 400px; margin: 60px auto; padding: 0 20px; }}
h1 {{ font-size: 1.4em; }}
input {{ width: 100%; padding: 12px; font-size: 16px; border: 1px solid #ccc; border-radius: 8px; margin: 8px 0; box-sizing: border-box; }}
button {{ width: 100%; padding: 14px; font-size: 16px; background: #000; color: #fff; border: none; border-radius: 8px; cursor: pointer; margin-top: 8px; }}
button:hover {{ background: #333; }}
.note {{ color: #666; font-size: 0.9em; margin-top: 16px; }}
</style></head><body>
<h1>Connect to Health Engine</h1>
<p>Enter your invite code to allow Claude to access your health data.</p>
<form method="POST" action="/oauth/consent">
<input type="hidden" name="client_id" value="{client_id}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="code_challenge" value="{code_challenge}">
<input type="hidden" name="scopes" value="{scopes}">
<input type="hidden" name="resource" value="{resource or ''}">
<input type="text" name="invite_code" placeholder="Invite code" autofocus required>
<button type="submit">Connect</button>
</form>
<p class="note">Don't have an invite? Ask your health coach.</p>
</body></html>""")

    @app.post("/oauth/consent")
    async def oauth_consent_submit(request: Request):
        """Process consent form: validate invite, generate auth code, redirect."""
        form = await request.form()
        invite_code = form.get("invite_code", "")
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        state = form.get("state")
        code_challenge = form.get("code_challenge", "")
        scopes = form.get("scopes", "")
        resource = form.get("resource") or None

        if not invite_code or not client_id or not redirect_uri:
            return HTMLResponse("<h1>Missing parameters</h1><p>Please try connecting again from Claude.</p>", status_code=400)

        # Validate invite
        from engine.gateway.db import get_db as _get_db, init_db as _init_db
        _init_db()
        _db = _get_db()
        invite_row = _db.execute(
            "SELECT person_id, used_at FROM oauth_invite WHERE code = ?",
            (invite_code,),
        ).fetchone()

        if not invite_row:
            return HTMLResponse("<h1>Invalid invite code</h1><p>Check your code and try again.</p>", status_code=403)

        person_id = invite_row["person_id"]

        # Create auth code
        from engine.gateway.oauth_provider import KisoOAuthProvider
        provider = KisoOAuthProvider()
        import asyncio
        auth_code = await provider.create_authorization_code(
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scopes=scopes.split() if scopes else ["health"],
            person_id=person_id,
            resource=resource,
        )

        # Mark invite as used
        _db.execute(
            "UPDATE oauth_invite SET used_at = ? WHERE code = ?",
            (datetime.now().isoformat(), invite_code),
        )
        _db.commit()

        # Redirect back to Claude with the auth code
        from mcp.server.auth.provider import construct_redirect_uri
        redirect = construct_redirect_uri(redirect_uri, code=auth_code, state=state)

        from starlette.responses import RedirectResponse as _RedirectResponse
        return _RedirectResponse(url=redirect, status_code=302)

    # --- MCP streamable-http transport ---
    # Exposes the health-engine MCP tools over HTTP so remote clients
    # (e.g. Paul via mcp-remote) can connect through Cloudflare Tunnel.
    try:
        from mcp_server.tools import register_tools, register_resources
        from mcp.server.fastmcp import FastMCP
        from starlette.middleware import Middleware as StarletteMiddleware
        from starlette.requests import Request as StarletteRequest
        from starlette.responses import Response as StarletteResponse, JSONResponse as StarletteJSONResponse

        from mcp.server.transport_security import TransportSecuritySettings

        # Allow the Cloudflare Tunnel domain + localhost for MCP connections
        allowed_hosts = ["localhost", "127.0.0.1"]
        if config.tunnel_domain:
            allowed_hosts.append(config.tunnel_domain)

        mcp_server = FastMCP(
            "Health Engine",
            instructions=(
                "Health Engine is a local-first health intelligence system. "
                "When the user asks about their health, wants a check-in, or mentions health data, "
                "call `checkin` first. Coach from the data: lead with what matters, connect metrics, "
                "give 1-2 nudges. Never dump raw JSON."
            ),
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
            ),
        )
        register_tools(mcp_server)
        register_resources(mcp_server)

        def _resolve_token_to_user_id(token: str) -> str | None:
            """Look up a token's health_engine_user_id.

            Checks two sources:
            1. Legacy bearer tokens in config.token_persons
            2. OAuth access tokens in the oauth_token table
            """
            # 1. Legacy bearer token path
            if token in config.token_persons:
                person_ids = config.token_persons[token]
                if isinstance(person_ids, str):
                    person_ids = [person_ids]
                if person_ids:
                    try:
                        from engine.gateway.db import get_db, init_db
                        init_db()
                        db = get_db()
                        row = db.execute(
                            "SELECT health_engine_user_id FROM person WHERE id = ? AND deleted_at IS NULL",
                            (person_ids[0],),
                        ).fetchone()
                        if row:
                            return row["health_engine_user_id"]
                    except Exception:
                        pass

            # 2. OAuth access token path
            try:
                from engine.gateway.db import get_db, init_db
                import time as _time
                init_db()
                db = get_db()
                row = db.execute(
                    "SELECT person_id FROM oauth_token "
                    "WHERE token = ? AND token_type = 'access' AND revoked = 0 "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (token, _time.time()),
                ).fetchone()
                if row:
                    person_row = db.execute(
                        "SELECT health_engine_user_id FROM person WHERE id = ? AND deleted_at IS NULL",
                        (row["person_id"],),
                    ).fetchone()
                    if person_row:
                        return person_row["health_engine_user_id"]
            except Exception:
                pass

            return None

        def _validate_token(token: str) -> bool:
            return bool(config.api_token and token == config.api_token)

        mcp_app = mcp_server.streamable_http_app()

        # Start the MCP session manager via the FastAPI lifespan
        from contextlib import asynccontextmanager

        original_router_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def combined_lifespan(a):
            async with mcp_server.session_manager.run():
                if original_router_lifespan:
                    async with original_router_lifespan(a) as state:
                        yield state
                else:
                    yield

        app.router.lifespan_context = combined_lifespan

        # Wrap with auth + audit logging
        authed_mcp_app = MCPAuthMiddleware(
            mcp_app,
            resolve_user_id=_resolve_token_to_user_id,
            validate_token=_validate_token,
        )
        app.mount("/mcp", authed_mcp_app)
        logger.info("MCP streamable-http mounted at /mcp")

        # --- OAuth routes for Claude iOS/cloud MCP auth ---
        try:
            from pydantic import AnyHttpUrl
            from starlette.routing import Mount, Route
            from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
            from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
            from engine.gateway.oauth_provider import KisoOAuthProvider

            oauth_provider = KisoOAuthProvider()
            issuer_url = AnyHttpUrl(f"https://{config.tunnel_domain}" if config.tunnel_domain else "http://localhost:18800")

            auth_routes = create_auth_routes(
                provider=oauth_provider,
                issuer_url=issuer_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=["health"],
                    default_scopes=["health"],
                ),
                revocation_options=RevocationOptions(enabled=True),
            )

            resource_url = AnyHttpUrl(f"https://{config.tunnel_domain}/mcp" if config.tunnel_domain else "http://localhost:18800/mcp")
            resource_routes = create_protected_resource_routes(
                resource_url=resource_url,
                authorization_servers=[issuer_url],
                scopes_supported=["health"],
                resource_name="Health Engine",
            )

            # Mount OAuth routes on the FastAPI app
            from starlette.routing import Router as StarletteRouter
            oauth_app = StarletteRouter(routes=auth_routes + resource_routes)
            app.mount("/", oauth_app)
            logger.info("OAuth routes mounted (authorize, token, register, metadata)")
        except Exception as e:
            logger.warning(f"OAuth routes not mounted: {e}")
    except Exception as e:
        logger.warning(f"MCP streamable-http not mounted: {e}")

    return app


def _do_garmin_auth(email: str, password: str, token_store: TokenStore, user_id: str) -> dict:
    """Authenticate with Garmin via garth and cache tokens.

    Error classification:
    - 429: Garmin SSO rate limit. Exponential backoff required.
    - 401: Wrong credentials.
    - 403: Account locked, region block, or CAPTCHA.
    - MFA: Multi-factor auth required (not supported in web flow).
    - Network: DNS, timeout, connection refused.
    - Unknown: Unclassified. Raw error preserved for diagnosis.
    """
    try:
        from garminconnect import Garmin

        client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
        client.login()

        td = token_store.garmin_token_dir(user_id)
        client.garth.dump(str(td))
        # Sync garth's token files back into SQLite
        token_store.sync_garmin_tokens(user_id)

        logger.info("garmin_auth success user_id=%s", user_id)
        return {
            "authenticated": True,
            "user_id": user_id,
        }
    except Exception as e:
        raw_error = str(e)
        error_type = "unknown"
        is_rate_limited = False
        user_msg = raw_error

        if "429" in raw_error or "Too Many Requests" in raw_error:
            error_type = "rate_limit_429"
            is_rate_limited = True
            user_msg = "Garmin is rate-limiting login attempts from this server. The retry timer above will tell you when to try again. Do not close this page."
        elif "401" in raw_error:
            error_type = "bad_credentials_401"
            user_msg = "Invalid email or password. Double-check your Garmin Connect credentials."
        elif "403" in raw_error:
            error_type = "forbidden_403"
            user_msg = "Garmin blocked this login. Possible causes: account locked, region restriction, or CAPTCHA required. Try logging into connect.garmin.com in your browser first, then retry here."
        elif "MFA" in raw_error.upper() or "verification" in raw_error.lower():
            error_type = "mfa_required"
            user_msg = "Your Garmin account requires multi-factor authentication. Please disable MFA temporarily, connect here, then re-enable it."
        elif any(w in raw_error.lower() for w in ["timeout", "timed out", "connection", "dns", "resolve"]):
            error_type = "network_error"
            user_msg = "Could not reach Garmin servers. This is a network issue, not a credentials issue. Try again in a few minutes."

        logger.warning(
            "garmin_auth failed user_id=%s error_type=%s raw=%s",
            user_id, error_type, raw_error[:200],
        )
        return {
            "authenticated": False,
            "error": user_msg,
            "error_type": error_type,
            "rate_limited": is_rate_limited,
        }


def _garmin_auth_page(user_id: str, state: str) -> str:
    """Render the Garmin auth HTML form."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connect Garmin</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'DM Sans', sans-serif;
    background: #09090b; color: #fafafa;
    min-height: 100vh; display: flex;
    align-items: center; justify-content: center;
    padding: 20px;
  }}
  .card {{
    background: #111113; border: 1px solid #27272a;
    border-radius: 16px; padding: 36px;
    width: 100%; max-width: 400px;
  }}
  h1 {{
    font-size: 1.2rem; font-weight: 600;
    margin-bottom: 6px;
  }}
  .subtitle {{
    font-size: 0.8rem; color: #71717a;
    margin-bottom: 24px; line-height: 1.5;
  }}
  .security-note {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem; color: #52525b;
    background: rgba(34, 197, 94, 0.05);
    border: 1px solid rgba(34, 197, 94, 0.1);
    border-radius: 8px; padding: 10px 12px;
    margin-bottom: 24px; line-height: 1.6;
  }}
  .security-note strong {{ color: #22c55e; }}
  label {{
    display: block; font-size: 0.75rem;
    color: #a1a1aa; margin-bottom: 6px;
    font-weight: 500;
  }}
  input[type="email"], input[type="password"] {{
    width: 100%; padding: 12px 14px;
    background: #18181b; border: 1px solid #27272a;
    border-radius: 8px; color: #fafafa;
    font-family: 'DM Sans', sans-serif; font-size: 0.95rem;
    margin-bottom: 16px; outline: none;
    transition: border-color 0.15s;
    -webkit-appearance: none;
  }}
  input:focus {{ border-color: #3b82f6; }}
  button {{
    width: 100%; padding: 14px;
    background: #fafafa; color: #09090b;
    border: none; border-radius: 8px;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.95rem; font-weight: 600;
    cursor: pointer; transition: opacity 0.15s;
  }}
  button:hover {{ opacity: 0.9; }}
  button:disabled {{ opacity: 0.5; cursor: wait; }}
  .status {{
    margin-top: 16px; font-size: 0.8rem;
    text-align: center; min-height: 20px;
  }}
  .status.error {{ color: #ef4444; }}
  .status.success {{ color: #22c55e; }}
  .status.loading {{ color: #a1a1aa; }}
</style>
</head>
<body>
<div class="card">
  <h1>Connect Garmin</h1>
  <p class="subtitle">Sign in to your Garmin Connect account to sync health data.</p>
  <div class="security-note">
    <strong>Your credentials are private.</strong> They go directly from this page
    to the auth server, are used once to obtain session tokens, and are immediately
    discarded. Nothing is stored.
  </div>
  <form id="authForm">
    <input type="hidden" name="user_id" value="{user_id}">
    <input type="hidden" name="state" value="{state}">
    <label for="email">Garmin Connect Email</label>
    <input type="email" id="email" name="email" required autocomplete="email" autofocus>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <button type="submit" id="submitBtn">Connect Garmin</button>
  </form>
  <div class="status" id="status"></div>
</div>
<script>
  const form = document.getElementById('authForm');
  const btn = document.getElementById('submitBtn');
  const status = document.getElementById('status');

  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    btn.disabled = true;
    btn.textContent = 'Connecting...';
    status.className = 'status loading';
    status.textContent = 'Authenticating with Garmin...';

    try {{
      const resp = await fetch('/auth/garmin/submit', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
        body: new URLSearchParams(new FormData(form)),
      }});
      const data = await resp.json();
      if (data.authenticated) {{
        status.className = 'status success';
        status.textContent = 'Connected! You can close this page and go back to your coach.';
        btn.textContent = 'Done';
        form.querySelectorAll('input[type="email"], input[type="password"]').forEach(i => {{
          i.value = ''; i.disabled = true;
        }});
      }} else if (data.rate_limited) {{
        status.className = 'status error';
        status.textContent = data.error;
        btn.disabled = true;
        let secs = data.retry_after_secs || 120;
        const timer = setInterval(() => {{
          secs--;
          if (secs > 60) {{
            btn.textContent = 'Wait ' + Math.ceil(secs/60) + ' min';
          }} else {{
            btn.textContent = 'Wait ' + secs + 's';
          }}
          if (secs <= 0) {{
            clearInterval(timer);
            btn.disabled = false;
            btn.textContent = 'Connect Garmin';
            status.textContent = 'You can try again now.';
            status.className = 'status';
          }}
        }}, 1000);
      }} else {{
        status.className = 'status error';
        status.textContent = data.error || 'Authentication failed. Check your credentials.';
        btn.disabled = false;
        btn.textContent = 'Connect Garmin';
      }}
    }} catch (err) {{
      status.className = 'status error';
      status.textContent = 'Connection error. Please try again.';
      btn.disabled = false;
      btn.textContent = 'Connect Garmin';
    }}
  }});
</script>
</body>
</html>"""


def _google_success_page(user_id: str) -> str:
    """Render the Google Calendar success page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Calendar Connected</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
  *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'DM Sans', sans-serif;
    background: #09090b; color: #fafafa;
    min-height: 100vh; display: flex;
    align-items: center; justify-content: center;
    padding: 20px;
  }}
  .card {{
    background: #111113; border: 1px solid #27272a;
    border-radius: 16px; padding: 36px;
    width: 100%; max-width: 400px; text-align: center;
  }}
  .check {{
    width: 64px; height: 64px; margin: 0 auto 20px;
    background: rgba(34, 197, 94, 0.1);
    border-radius: 50%; display: flex;
    align-items: center; justify-content: center;
  }}
  .check svg {{ width: 32px; height: 32px; color: #22c55e; }}
  h1 {{
    font-size: 1.2rem; font-weight: 600;
    margin-bottom: 8px; color: #22c55e;
  }}
  p {{
    font-size: 0.85rem; color: #a1a1aa;
    line-height: 1.6;
  }}
  .uid {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem; color: #52525b;
    margin-top: 16px;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="check">
    <svg fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
    </svg>
  </div>
  <h1>Calendar Connected</h1>
  <p>Your Google Calendar is linked. Your coach can now see your schedule and create events for you.</p>
  <p style="margin-top: 12px;">You can close this page and go back to your coach.</p>
  <p class="uid">{user_id}</p>
</div>
</body>
</html>"""


def _error_page(message: str) -> str:
    """Render an error page."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Error</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #09090b; color: #ef4444;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         padding: 20px; }}
  .card {{ background: #111113; border: 1px solid #27272a; border-radius: 16px;
           padding: 40px; max-width: 400px; text-align: center; }}
  h1 {{ font-size: 1.2rem; margin-bottom: 12px; }}
  p {{ color: #a1a1aa; font-size: 0.85rem; line-height: 1.6; }}
</style></head>
<body><div class="card">
  <h1>Link Error</h1>
  <p>{message}</p>
</div></body></html>"""


def run_gateway(config: GatewayConfig | None = None):
    """Start the gateway server (blocking)."""
    import uvicorn
    from .db import init_db

    if config is None:
        config = load_gateway_config()
    init_db()

    # Logfire OTEL tracing (auto-instruments FastAPI if available)
    _logfire_ok = False
    try:
        import logfire
        logfire.configure(service_name="kiso", send_to_logfire="if-token-present")
        _logfire_ok = True
        logger.info("Logfire tracing initialized")
    except ImportError:
        logger.info("logfire not installed, skipping tracing")
    except Exception as e:
        logger.warning("Logfire init failed: %s. Continuing without tracing.", e)

    app = create_app(config)

    if _logfire_ok:
        try:
            logfire.instrument_fastapi(app)
        except Exception:
            pass

    print(f"Health Engine Gateway starting on port {config.port}")
    if config.tunnel_domain:
        print(f"Tunnel domain: https://{config.tunnel_domain}")
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level="info")
