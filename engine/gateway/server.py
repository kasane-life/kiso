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


def create_app(config: GatewayConfig | None = None) -> "FastAPI":
    """Create the FastAPI application."""
    from fastapi import FastAPI, Form, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    if config is None:
        config = load_gateway_config()

    app = FastAPI(title="Health Engine Gateway", docs_url=None, redoc_url=None)
    token_store = TokenStore()

    # --- Health-engine tool API + transcript viewer ---
    from .api import api_handler, api_list_tools, api_async_handler, api_job_status, api_upload, api_shortcut, open_shortcut_redirect, open_automation_redirect
    from .transcripts import transcripts_api, transcripts_html
    from .v1_api import register_v1_routes

    # Kasane v1 API (must come before the {tool_name} wildcard)
    register_v1_routes(app)

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

        # 4. Apple Health per-user freshness
        apple_health = {}
        if users_dir.exists():
            for user_dir in sorted(users_dir.iterdir()):
                if not user_dir.is_dir() or user_dir.name in skip_users:
                    continue
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
                                apple_health[user_dir.name] = {
                                    "status": "ok" if age_hours < 48 else "stale",
                                    "last_sync_hours_ago": round(age_hours, 1),
                                }
                            except:
                                apple_health[user_dir.name] = {"status": "parse_error"}
                        else:
                            apple_health[user_dir.name] = {"status": "no_timestamp"}
                    except:
                        apple_health[user_dir.name] = {"status": "read_error"}

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

    if config is None:
        config = load_gateway_config()
    app = create_app(config)
    print(f"Health Engine Gateway starting on port {config.port}")
    if config.tunnel_domain:
        print(f"Tunnel domain: https://{config.tunnel_domain}")
    uvicorn.run(app, host="0.0.0.0", port=config.port, log_level="info")
