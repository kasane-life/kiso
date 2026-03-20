"""FastAPI auth gateway for wearable onboarding.

Persistent server (port 18800) that serves auth pages and handles
credential submission. Designed to sit behind a Cloudflare Tunnel
so WhatsApp users can tap a link on their phone.

Routes:
  GET  /              — status page (health check)
  GET  /health        — JSON health check for monitoring
  GET  /auth/garmin   — credential form (requires ?user=<user_id>&state=<hmac>)
  POST /auth/garmin/submit — processes credentials, caches tokens
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from .config import GatewayConfig, load_gateway_config
from .token_store import TokenStore


def create_app(config: GatewayConfig | None = None) -> "FastAPI":
    """Create the FastAPI application."""
    from fastapi import FastAPI, Form, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse

    if config is None:
        config = load_gateway_config()

    app = FastAPI(title="Health Engine Auth Gateway", docs_url=None, redoc_url=None)
    token_store = TokenStore()

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

    # Expose for use by MCP tools
    app.state.generate_auth_url = generate_auth_url
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

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

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

        result = _do_garmin_auth(email, password, token_store, verified[0])
        return JSONResponse(result)

    return app


def _do_garmin_auth(email: str, password: str, token_store: TokenStore, user_id: str) -> dict:
    """Authenticate with Garmin via garth and cache tokens."""
    try:
        from garminconnect import Garmin

        client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
        client.login()

        td = token_store.garmin_token_dir(user_id)
        client.garth.dump(str(td))

        return {
            "authenticated": True,
            "user_id": user_id,
            "token_dir": str(td),
        }
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg:
            error_msg = "Invalid email or password."
        elif "MFA" in error_msg.upper() or "verification" in error_msg.lower():
            error_msg = "MFA required. Currently only non-MFA accounts are supported via web auth."
        return {
            "authenticated": False,
            "error": error_msg,
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
