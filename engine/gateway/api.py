"""HTTP API for health-engine tools.

Exposes all tools from TOOL_REGISTRY as GET/POST endpoints at /api/{tool_name}.
Auth via ?token=<secret> query parameter (no headers needed — compatible with
OpenClaw's GET-only web_fetch).

JSON dict/list params are passed as URL-encoded JSON strings and auto-parsed.
"""

import inspect
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse

from mcp_server.tools import TOOL_REGISTRY

# Background job tracking for async tool calls (e.g. pull_garmin_async)
_background_jobs: dict[str, dict] = {}
_job_lock = threading.Lock()

logger = logging.getLogger("health-engine.api")

_AUDIT_LOG_PATH = os.path.join("data", "admin", "api_audit.jsonl")


def _audit_log(tool: str, user_id: str, params: dict, result: dict | None,
               error: str | None, elapsed_ms: int):
    """Append one audit entry to data/admin/api_audit.jsonl."""
    entry = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(),
        "tool": tool,
        "user_id": user_id,
        "params": {k: v for k, v in params.items() if k != "token"},
        "status": "ok" if error is None else "error",
        "ms": elapsed_ms,
    }
    if error is not None:
        entry["error"] = str(error)
    elif result is not None and isinstance(result, dict):
        entry["result_keys"] = list(result.keys())
    try:
        os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
        with open(_AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        logger.warning("Failed to write audit log", exc_info=True)

# Params that accept complex types (dicts/lists) — auto-parse from JSON strings
_COMPLEX_PARAMS = {"habits", "results", "supplements", "goals", "conditions"}

# Flat health metric keys that can be passed as query params instead of nested metrics dict
_HEALTH_METRIC_KEYS = {
    "resting_hr", "hrv_sdnn", "steps", "sleep_hours",
    "sleep_start", "sleep_end", "weight_lbs", "vo2_max",
    "blood_oxygen", "active_calories", "respiratory_rate",
}


def _coerce_params(tool_name: str, params: dict) -> dict:
    """Coerce query string values to match tool function signatures."""
    func = TOOL_REGISTRY.get(tool_name)
    if not func:
        return params

    sig = inspect.signature(func)
    coerced = {}
    for key, value in params.items():
        if key not in sig.parameters:
            continue
        param = sig.parameters[key]
        annotation = param.annotation

        # Parse JSON strings for complex types
        if key in _COMPLEX_PARAMS or (isinstance(value, str) and value.startswith(("{", "["))):
            try:
                coerced[key] = json.loads(value)
                continue
            except (json.JSONDecodeError, TypeError):
                pass

        # Coerce numeric types
        origin = getattr(annotation, "__origin__", None)
        base = annotation if origin is None else None

        if base is int:
            try:
                coerced[key] = int(value)
                continue
            except (ValueError, TypeError):
                pass
        elif base is float:
            try:
                coerced[key] = float(value)
                continue
            except (ValueError, TypeError):
                pass
        elif base is bool:
            coerced[key] = value.lower() in ("true", "1", "yes")
            continue

        # Check union types (e.g. float | None)
        if origin is not None:
            args = getattr(annotation, "__args__", ())
            for arg in args:
                if arg is type(None):
                    continue
                if arg is int:
                    try:
                        coerced[key] = int(value)
                        break
                    except (ValueError, TypeError):
                        pass
                elif arg is float:
                    try:
                        coerced[key] = float(value)
                        break
                    except (ValueError, TypeError):
                        pass
                elif arg is bool:
                    coerced[key] = value.lower() in ("true", "1", "yes")
                    break
            else:
                coerced[key] = value
        else:
            coerced[key] = value

    return coerced


async def api_handler(tool_name: str, request: Request, token: str = Query(None)):
    """Generic handler for /api/{tool_name}.

    If tool_name ends with _async, dispatches to the background job handler.
    Token can be passed as query param OR in JSON body (for iOS Shortcuts).
    """
    config = request.app.state.config

    # Parse POST body early so we can extract token from it if needed
    body = {}
    if request.method == "POST":
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

    # Accept token from query string or JSON body
    effective_token = token or body.get("token")

    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if not effective_token or effective_token != config.api_token:
        raise HTTPException(403, "Invalid token")

    # Route _async suffix to background handler
    if tool_name.endswith("_async"):
        return await api_async_handler(tool_name, request, effective_token)

    if tool_name not in TOOL_REGISTRY:
        raise HTTPException(404, f"Unknown tool: {tool_name}")

    # Collect params from query string (GET) or body (POST)
    params = dict(request.query_params)
    params.pop("token", None)

    if body:
        params.update(body)
    params.pop("token", None)

    # For ingest_health_snapshot: collect flat metric params into a metrics dict
    # so shortcuts can send ?resting_hr=58&steps=8000 instead of metrics={"resting_hr":58,...}
    if tool_name == "ingest_health_snapshot" and "metrics" not in params:
        metrics = {}
        for key in list(params.keys()):
            if key in _HEALTH_METRIC_KEYS:
                val = params.pop(key)
                # Try to convert to float for numeric metrics
                try:
                    metrics[key] = float(val)
                except (ValueError, TypeError):
                    metrics[key] = val  # Keep as string (e.g. sleep_start/sleep_end)
        if metrics:
            params["metrics"] = metrics
            # Default user_id if not provided (matches shortcut behavior)
            if "user_id" not in params:
                params["user_id"] = "default"

    # Enforce user_id on write operations (prevents data going to wrong directory)
    WRITE_TOOLS = {
        "log_weight", "log_bp", "log_meal", "log_habits", "log_supplements",
        "log_sleep", "log_medication", "log_session", "log_labs", "log_nudge",
        "setup_profile", "checkin", "score", "get_meals", "get_daily_snapshot",
        "pull_garmin", "pull_oura", "pull_whoop", "ingest_health_snapshot",
        "save_coaching_message",
    }
    if tool_name in WRITE_TOOLS and not params.get("user_id"):
        raise HTTPException(
            400,
            f"user_id is required for {tool_name}. Add &user_id=andrew (or the appropriate user) to the request."
        )

    # Coerce types to match function signatures
    params = _coerce_params(tool_name, params)

    user_id = params.get("user_id", "default")
    start = time.monotonic()

    try:
        result = TOOL_REGISTRY[tool_name](**params)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Serialize with default=str to handle datetime, Path, etc.
        serialized = json.loads(json.dumps(result, default=str))
        _audit_log(tool_name, user_id, params, serialized, None, elapsed_ms)
        return JSONResponse(content=serialized)
    except TypeError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _audit_log(tool_name, user_id, params, None, str(e), elapsed_ms)
        raise HTTPException(400, f"Parameter error: {e}")
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _audit_log(tool_name, user_id, params, None, str(e), elapsed_ms)
        logger.exception(f"Tool {tool_name} failed")
        raise HTTPException(500, f"Tool error: {e}")


async def api_list_tools(request: Request, token: str = Query(...)):
    """List all available tool names."""
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    tools = []
    for name, func in TOOL_REGISTRY.items():
        sig = inspect.signature(func)
        params = [
            {"name": p.name, "default": None if p.default is inspect.Parameter.empty else repr(p.default)}
            for p in sig.parameters.values()
        ]
        tools.append({"name": name, "params": params, "doc": (func.__doc__ or "").split("\n")[0]})
    return JSONResponse(content={"tools": tools})


def _run_background_job(job_id: str, tool_name: str, params: dict):
    """Run a tool in a background thread, storing result in _background_jobs."""
    start = time.monotonic()
    try:
        result = TOOL_REGISTRY[tool_name](**params)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        serialized = json.loads(json.dumps(result, default=str))
        user_id = params.get("user_id", "default")
        _audit_log(tool_name, user_id, params, serialized, None, elapsed_ms)
        with _job_lock:
            _background_jobs[job_id] = {
                "status": "completed",
                "result": serialized,
                "elapsed_ms": elapsed_ms,
            }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        user_id = params.get("user_id", "default")
        _audit_log(tool_name, user_id, params, None, str(e), elapsed_ms)
        with _job_lock:
            _background_jobs[job_id] = {
                "status": "error",
                "error": str(e),
                "elapsed_ms": elapsed_ms,
            }


async def api_async_handler(tool_name: str, request: Request, token: str = Query(...)):
    """Fire-and-forget handler: runs a tool in background, returns job ID immediately.

    Use /api/job_status?token=...&job_id=... to poll for results.
    Currently supported: pull_garmin_async → runs pull_garmin in background.
    """
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    # Strip _async suffix to get the real tool name
    real_tool = tool_name.replace("_async", "")
    if real_tool not in TOOL_REGISTRY:
        raise HTTPException(404, f"Unknown tool: {real_tool}")

    params = dict(request.query_params)
    params.pop("token", None)
    params = _coerce_params(real_tool, params)

    job_id = f"{real_tool}_{int(time.time() * 1000)}"
    with _job_lock:
        _background_jobs[job_id] = {"status": "running", "tool": real_tool}

    thread = threading.Thread(
        target=_run_background_job,
        args=(job_id, real_tool, params),
        daemon=True,
    )
    thread.start()

    return JSONResponse(content={
        "job_id": job_id,
        "status": "running",
        "message": f"{real_tool} started in background. Poll /api/job_status?job_id={job_id} for results.",
    })


async def api_job_status(request: Request, token: str = Query(...), job_id: str = Query(...)):
    """Check the status of a background job."""
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    with _job_lock:
        job = _background_jobs.get(job_id)

    if not job:
        raise HTTPException(404, f"Unknown job: {job_id}")

    return JSONResponse(content={"job_id": job_id, **job})


# Supported upload types and their corresponding tool functions
_UPLOAD_TYPES = {
    "apple_health": "import_apple_health",
}

# Allowed file extensions per upload type
_UPLOAD_EXTENSIONS = {
    "apple_health": {".zip", ".xml"},
}


async def api_upload(
    request: Request,
    token: str = Query(...),
    user_id: str = Query("default"),
    type: str = Query(..., description="Upload type, e.g. 'apple_health'"),
    file: UploadFile = File(...),
):
    """Accept file uploads and route to the appropriate import tool.

    POST /api/upload?token=...&user_id=...&type=apple_health
    Content-Type: multipart/form-data
    Body: file=<the export ZIP or XML>

    Saves the uploaded file to a temp location, calls the import tool,
    cleans up, and returns import results.
    """
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    if type not in _UPLOAD_TYPES:
        raise HTTPException(
            400,
            f"Unsupported upload type: {type}. Supported: {list(_UPLOAD_TYPES.keys())}",
        )

    # Validate file extension
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    allowed = _UPLOAD_EXTENSIONS.get(type, set())
    if allowed and ext not in allowed:
        raise HTTPException(
            400,
            f"Invalid file type '{ext}' for {type}. Accepted: {sorted(allowed)}",
        )

    # Validate file size (100MB max for Apple Health exports)
    max_size = 100 * 1024 * 1024
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(
            400,
            f"File too large ({len(content) / 1024 / 1024:.1f}MB). Max: {max_size / 1024 / 1024:.0f}MB.",
        )

    tool_name = _UPLOAD_TYPES[type]
    if tool_name not in TOOL_REGISTRY:
        raise HTTPException(500, f"Import tool '{tool_name}' not found in registry")

    start = time.monotonic()
    tmp_path = None

    try:
        # Save to temp file preserving extension
        suffix = ext or ".tmp"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix=f"he_upload_{type}_")
        os.write(fd, content)
        os.close(fd)

        # Call the import tool
        params = {"file_path": tmp_path, "user_id": user_id}
        result = TOOL_REGISTRY[tool_name](**params)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        serialized = json.loads(json.dumps(result, default=str))
        _audit_log(tool_name, user_id, {"type": type, "filename": filename}, serialized, None, elapsed_ms)
        return JSONResponse(content=serialized)

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _audit_log(tool_name, user_id, {"type": type, "filename": filename}, None, str(e), elapsed_ms)
        logger.exception(f"Upload handler failed for type={type}")
        raise HTTPException(500, f"Import error: {e}")

    finally:
        # Clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def api_shortcut(
    request: Request,
    token: str = Query(...),
    user_id: str = Query(...),
):
    """Generate and serve a personalized Apple Health Shortcut file.

    GET /api/shortcut?token=...&user_id=paul

    Returns a signed .shortcut file that the user can tap to install.
    The shortcut reads HealthKit data and syncs it daily.
    """
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    from engine.shortcuts.generator import generate_shortcut

    start = time.monotonic()

    try:
        # Check for pre-signed shortcut (generated by scripts/sign_shortcuts.sh)
        signed_path = os.path.join("data", "shortcuts", f"{user_id}.shortcut")
        if os.path.exists(signed_path):
            with open(signed_path, "rb") as f:
                result_bytes = f.read()
            logger.info(f"Serving pre-signed shortcut for {user_id} ({len(result_bytes)} bytes)")
        else:
            # Generate unsigned on-the-fly as fallback
            logger.warning(f"No pre-signed shortcut for {user_id}, generating unsigned")
            shortcut_bytes = generate_shortcut(
                user_id=user_id,
                api_token=config.api_token,
            )

            # Try to sign it (requires macOS shortcuts CLI, won't work in Docker)
            signed_bytes = _sign_shortcut(shortcut_bytes)
            if signed_bytes is not None:
                result_bytes = signed_bytes
            else:
                result_bytes = shortcut_bytes

        elapsed_ms = int((time.monotonic() - start) * 1000)
        _audit_log("shortcut", user_id, {}, {"size": len(result_bytes)}, None, elapsed_ms)

        from fastapi.responses import Response
        return Response(
            content=result_bytes,
            media_type="application/x-shortcut",
            headers={
                "Content-Disposition": f'inline; filename="Baseline Health Sync.shortcut"',
            },
        )

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _audit_log("shortcut", user_id, {}, None, str(e), elapsed_ms)
        raise HTTPException(500, f"Failed to generate shortcut: {e}")


def _sign_shortcut(unsigned_bytes: bytes) -> bytes | None:
    """Sign a .shortcut file using the macOS shortcuts CLI.

    Returns signed bytes, or None if signing is unavailable.
    """
    import subprocess

    # Check if shortcuts CLI exists
    if not shutil.which("shortcuts"):
        return None

    tmp_in = None
    tmp_out = None
    try:
        # Write unsigned file
        fd, tmp_in = tempfile.mkstemp(suffix=".shortcut", prefix="he_unsigned_")
        os.write(fd, unsigned_bytes)
        os.close(fd)

        # Sign it
        tmp_out = tmp_in.replace("unsigned", "signed")
        result = subprocess.run(
            ["shortcuts", "sign", "--mode", "anyone", "--input", tmp_in, "--output", tmp_out],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning(f"shortcuts sign failed: {result.stderr}")
            return None

        with open(tmp_out, "rb") as f:
            return f.read()

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning(f"Shortcut signing error: {e}")
        return None
    finally:
        for p in (tmp_in, tmp_out):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def open_shortcut_redirect(
    request: Request,
    token: str = Query(...),
    user_id: str = Query(...),
):
    """Redirect to shortcuts://import-shortcut via an HTML page.

    GET /open/shortcut?token=...&user_id=paul

    Builds a shortcuts://import-shortcut URL that points to /api/shortcut
    for the actual file download. This way tapping one HTTPS link in WhatsApp
    opens the Shortcuts app with an "Add Shortcut" prompt.
    """
    config = request.app.state.config
    if not config.api_token:
        raise HTTPException(500, "API token not configured on server")
    if token != config.api_token:
        raise HTTPException(403, "Invalid token")

    from urllib.parse import quote

    # The raw .shortcut file URL that the Shortcuts app will fetch
    download_url = f"https://auth.mybaseline.health/api/shortcut?token={token}&user_id={user_id}"
    encoded_url = quote(download_url, safe="")
    shortcut_name = quote("Baseline Health Sync", safe="")
    import_url = f"shortcuts://import-shortcut?url={encoded_url}&name={shortcut_name}"

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0;url={import_url}">
<title>Installing Shortcut...</title>
<style>
body {{ font-family: -apple-system, sans-serif; text-align: center; padding: 60px 20px; background: #f5f5f7; color: #1d1d1f; }}
h1 {{ font-size: 22px; font-weight: 600; margin-bottom: 12px; }}
p {{ font-size: 16px; color: #86868b; margin-bottom: 24px; }}
a {{ color: #0071e3; text-decoration: none; font-size: 16px; }}
</style>
</head>
<body>
<h1>Installing Baseline Health Sync</h1>
<p>If nothing happened, tap the link below.</p>
<a href="{import_url}">Open in Shortcuts App</a>
</body>
</html>""", status_code=200)


async def open_automation_redirect():
    """Redirect to shortcuts://create-automation via an HTML page.

    GET /open/automation

    WhatsApp won't make shortcuts:// URLs tappable, but it will make
    https:// URLs tappable. This page redirects to the Shortcuts app's
    automation creation screen.
    """
    return HTMLResponse(content="""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0;url=shortcuts://create-automation">
<title>Opening Shortcuts...</title>
<style>
body { font-family: -apple-system, sans-serif; text-align: center; padding: 60px 20px; background: #f5f5f7; color: #1d1d1f; }
h1 { font-size: 22px; font-weight: 600; margin-bottom: 12px; }
p { font-size: 16px; color: #86868b; margin-bottom: 24px; }
a { color: #0071e3; text-decoration: none; font-size: 16px; }
</style>
</head>
<body>
<h1>Opening Shortcuts</h1>
<p>If nothing happened, tap the link below.</p>
<a href="shortcuts://create-automation">Open Shortcuts App</a>
</body>
</html>""", status_code=200)
