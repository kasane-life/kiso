"""Unified token storage for wearable services.

Primary storage: SQLite (wearable_token table in kasane.db).
Garth compatibility: tokens are written to a temp-like directory when garth
needs filesystem access, then synced back to SQLite after any changes.

Migration: on first access for a user/service, checks the legacy file path
(~/.config/health-engine/tokens/<service>/<user_id>/) and imports into SQLite.

Tokens are encrypted at rest using Fernet (AES-128-CBC + HMAC).
Key source: HE_TOKEN_KEY env var, or auto-generated at
~/.config/health-engine/token.key.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("health-engine.token_store")

_LEGACY_BASE_DIR = Path(os.path.expanduser("~/.config/health-engine/tokens"))
_KEY_PATH = Path(os.path.expanduser("~/.config/health-engine/token.key"))
# Garth needs a real directory to load/dump tokens. We use a stable per-user
# path so garth's token refresh can persist across calls within a process.
_GARTH_CACHE_DIR = Path(os.path.expanduser("~/.config/health-engine/garth-cache"))


def _get_fernet():
    """Get a Fernet instance for token encryption/decryption.

    Returns None if cryptography is not installed (graceful degradation).
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None

    key_env = os.environ.get("HE_TOKEN_KEY")
    if key_env:
        return Fernet(key_env.encode() if isinstance(key_env, str) else key_env)

    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _KEY_PATH.write_bytes(key)
        os.chmod(_KEY_PATH, 0o600)
    return Fernet(key)


def _get_db():
    """Get a DB connection. Lazy import to avoid circular deps.

    Assumes init_db() was called at server startup. Does NOT call init_db()
    to avoid overriding test DB paths.
    """
    from engine.gateway.db import get_db
    return get_db()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TokenStore:
    """Manage wearable auth tokens per service and user.

    Reads/writes tokens in SQLite (wearable_token table).
    Falls back to legacy file paths for migration.
    """

    def __init__(self, base_dir: str | Path | None = None):
        # Legacy base_dir kept for backward compat during migration
        self._legacy_dir = Path(base_dir) if base_dir else _LEGACY_BASE_DIR
        self._fernet = _get_fernet()

    def _encrypt(self, data: bytes) -> bytes:
        if self._fernet:
            return self._fernet.encrypt(data)
        return data

    def _decrypt(self, data: bytes) -> bytes:
        if self._fernet and data.startswith(b"gAAAAA"):
            return self._fernet.decrypt(data)
        return data

    # --- SQLite operations ---

    def _db_save_token(self, user_id: str, service: str, token_name: str, raw_bytes: bytes, person_id: str | None = None):
        """Save a single token file to SQLite."""
        db = _get_db()
        encrypted = self._encrypt(raw_bytes)
        now = _now_iso()
        existing = db.execute(
            "SELECT id FROM wearable_token WHERE user_id = ? AND service = ? AND token_name = ?",
            (user_id, service, token_name),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE wearable_token SET token_data = ?, updated_at = ?, person_id = ? WHERE id = ?",
                (encrypted, now, person_id, existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO wearable_token (id, person_id, user_id, service, token_name, token_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), person_id, user_id, service, token_name, encrypted, now, now),
            )
        db.commit()

    def _db_load_token(self, user_id: str, service: str, token_name: str) -> bytes | None:
        """Load a single token from SQLite. Returns decrypted bytes or None."""
        db = _get_db()
        row = db.execute(
            "SELECT token_data FROM wearable_token WHERE user_id = ? AND service = ? AND token_name = ?",
            (user_id, service, token_name),
        ).fetchone()
        if row is None:
            return None
        return self._decrypt(row["token_data"])

    def _db_has_tokens(self, user_id: str, service: str) -> bool:
        db = _get_db()
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM wearable_token WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchone()
        return row["cnt"] > 0

    def _db_list_tokens(self, user_id: str, service: str) -> list[str]:
        """List token names for a user/service."""
        db = _get_db()
        rows = db.execute(
            "SELECT token_name FROM wearable_token WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchall()
        return [r["token_name"] for r in rows]

    # --- Legacy file migration ---

    def _legacy_token_dir(self, service: str, user_id: str) -> Path:
        return self._legacy_dir / service / user_id

    def _migrate_from_files(self, user_id: str, service: str):
        """One-time migration: copy file tokens into SQLite, then leave files as backup."""
        legacy_dir = self._legacy_token_dir(service, user_id)
        if not legacy_dir.exists() or legacy_dir.is_symlink():
            return
        if self._db_has_tokens(user_id, service):
            return  # Already migrated

        migrated = 0
        for fpath in legacy_dir.iterdir():
            if not fpath.is_file():
                continue
            raw = fpath.read_bytes()
            # Decrypt if file was Fernet-encrypted, then re-encrypt for DB
            if self._fernet and raw.startswith(b"gAAAAA"):
                raw = self._fernet.decrypt(raw)
            self._db_save_token(user_id, service, fpath.name, raw)
            migrated += 1

        if migrated:
            logger.info("Migrated %d token files for %s/%s from disk to SQLite", migrated, service, user_id)

    # --- Public API ---

    def save_token(self, service: str, user_id: str, data: dict) -> Path:
        """Save token data to SQLite. Returns a (legacy) directory path for backward compat."""
        raw = json.dumps(data, indent=2).encode()
        self._db_save_token(user_id, service, "token.json", raw)
        # Return legacy path for any callers that expect it
        return self._legacy_token_dir(service, user_id)

    def load_token(self, service: str, user_id: str) -> dict | None:
        """Load token data from SQLite. Falls back to files + migrates."""
        self._migrate_from_files(user_id, service)
        raw = self._db_load_token(user_id, service, "token.json")
        if raw is None:
            return None
        return json.loads(raw)

    def has_token(self, service: str, user_id: str) -> bool:
        """Check if tokens exist for a service/user combo."""
        self._migrate_from_files(user_id, service)
        return self._db_has_tokens(user_id, service)

    def garmin_token_dir(self, user_id: str = "default") -> Path:
        """Get a garth-compatible token directory for Garmin.

        Garmin tokens are stored as garth dumps (oauth1_token.json,
        oauth2_token.json). This method:
        1. Migrates legacy file tokens to SQLite if needed
        2. Writes SQLite tokens to a cache directory for garth to read
        3. Returns the cache directory path

        After garth modifies tokens (e.g. refresh), call sync_garmin_tokens()
        to write changes back to SQLite.
        """
        self._migrate_from_files(user_id, "garmin")

        cache_dir = _GARTH_CACHE_DIR / user_id
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Write SQLite tokens to cache dir for garth
        for token_name in self._db_list_tokens(user_id, "garmin"):
            raw = self._db_load_token(user_id, "garmin", token_name)
            if raw:
                (cache_dir / token_name).write_bytes(raw)

        return cache_dir

    def sync_garmin_tokens(self, user_id: str):
        """Sync garth cache directory back to SQLite after garth modifies tokens.

        Call this after garth.dump() or garth.refresh_oauth2().
        """
        cache_dir = _GARTH_CACHE_DIR / user_id
        if not cache_dir.exists():
            return

        for fpath in cache_dir.iterdir():
            if not fpath.is_file() or not fpath.name.endswith(".json"):
                continue
            raw = fpath.read_bytes()
            self._db_save_token(user_id, "garmin", fpath.name, raw)

    def save_garmin_tokens(self, user_id: str, token_dir: Path | str):
        """Save garth-dumped tokens from a directory into SQLite.

        Called after initial auth: garth.dump(dir) -> save_garmin_tokens(user_id, dir).
        """
        td = Path(token_dir)
        if not td.exists():
            return
        for fpath in td.iterdir():
            if not fpath.is_file() or not fpath.name.endswith(".json"):
                continue
            raw = fpath.read_bytes()
            self._db_save_token(user_id, "garmin", fpath.name, raw)
