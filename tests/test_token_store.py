"""Tests for TokenStore encryption and SQLite storage."""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def token_dir(tmp_path):
    return tmp_path / "tokens"


@pytest.fixture
def key_path(tmp_path):
    return tmp_path / "token.key"


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Set up a test SQLite DB and patch _get_db to use it."""
    from engine.gateway.db import init_db, close_db, get_db
    close_db()
    db_path = tmp_path / "test.db"
    init_db(db_path)
    monkeypatch.setattr(
        "engine.gateway.token_store._get_db",
        lambda: get_db(db_path),
    )
    yield db_path
    close_db()


@pytest.fixture
def store_encrypted(token_dir, key_path, test_db, monkeypatch):
    """TokenStore with Fernet encryption enabled."""
    monkeypatch.setattr("engine.gateway.token_store._LEGACY_BASE_DIR", token_dir)
    monkeypatch.setattr("engine.gateway.token_store._KEY_PATH", key_path)
    monkeypatch.delenv("HE_TOKEN_KEY", raising=False)
    from engine.gateway.token_store import TokenStore
    return TokenStore(base_dir=token_dir)


@pytest.fixture
def store_no_crypto(token_dir, test_db, monkeypatch):
    """TokenStore with cryptography unavailable."""
    monkeypatch.setattr("engine.gateway.token_store._LEGACY_BASE_DIR", token_dir)

    import engine.gateway.token_store as mod
    monkeypatch.setattr(mod, "_get_fernet", lambda: None)
    from engine.gateway.token_store import TokenStore
    store = TokenStore(base_dir=token_dir)
    store._fernet = None
    return store


def test_encrypt_decrypt_roundtrip(store_encrypted):
    """Tokens saved encrypted can be loaded back."""
    data = {
        "access_token": "ya29.test",
        "refresh_token": "1//0test",
        "client_id": "test.apps.googleusercontent.com",
        "client_secret": "secret123",
    }
    store_encrypted.save_token("google-calendar", "testuser", data)
    loaded = store_encrypted.load_token("google-calendar", "testuser")
    assert loaded == data


def test_encrypted_blob_is_not_plaintext(store_encrypted, test_db):
    """The token_data in SQLite should not contain plaintext tokens."""
    from engine.gateway.db import get_db
    data = {"access_token": "ya29.plaintext_check", "refresh_token": "1//0refresh"}
    store_encrypted.save_token("google-calendar", "testuser", data)

    db = get_db(test_db)
    row = db.execute(
        "SELECT token_data FROM wearable_token WHERE user_id = 'testuser' AND service = 'google-calendar'"
    ).fetchone()
    assert row is not None
    raw = row["token_data"]
    assert b"ya29.plaintext_check" not in raw
    assert raw.startswith(b"gAAAAA")  # Fernet prefix


def test_legacy_file_migration(store_encrypted, token_dir):
    """Plaintext JSON files from before SQLite migration can be loaded via migration."""
    td = token_dir / "google-calendar" / "legacy"
    td.mkdir(parents=True)
    data = {"access_token": "old_token", "refresh_token": "old_refresh"}
    (td / "token.json").write_text(json.dumps(data))

    loaded = store_encrypted.load_token("google-calendar", "legacy")
    assert loaded == data


def test_no_crypto_roundtrip(store_no_crypto):
    """Without cryptography, tokens still save and load via SQLite."""
    data = {"access_token": "plain", "refresh_token": "text"}
    store_no_crypto.save_token("test-service", "user1", data)
    loaded = store_no_crypto.load_token("test-service", "user1")
    assert loaded == data


def test_has_token(store_encrypted):
    """has_token returns True after saving."""
    assert not store_encrypted.has_token("svc", "u1")
    store_encrypted.save_token("svc", "u1", {"token": "val"})
    assert store_encrypted.has_token("svc", "u1")


def test_load_missing_returns_none(store_encrypted):
    assert store_encrypted.load_token("nonexistent", "nobody") is None


def test_key_auto_generated(key_path, token_dir, monkeypatch):
    """Key file is auto-generated on first use."""
    monkeypatch.setattr("engine.gateway.token_store._LEGACY_BASE_DIR", token_dir)
    monkeypatch.setattr("engine.gateway.token_store._KEY_PATH", key_path)
    monkeypatch.delenv("HE_TOKEN_KEY", raising=False)

    assert not key_path.exists()
    from engine.gateway.token_store import _get_fernet
    f = _get_fernet()
    assert f is not None
    assert key_path.exists()
    mode = oct(key_path.stat().st_mode)[-3:]
    assert mode == "600"


def test_env_var_key(token_dir, monkeypatch):
    """HE_TOKEN_KEY env var is used when set."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    monkeypatch.setenv("HE_TOKEN_KEY", key.decode())
    monkeypatch.setattr("engine.gateway.token_store._LEGACY_BASE_DIR", token_dir)

    from engine.gateway.token_store import _get_fernet
    f = _get_fernet()
    assert f is not None
    ct = f.encrypt(b"test")
    assert f.decrypt(ct) == b"test"


# --- Garth-cache fallback tests ---


@pytest.fixture
def garth_cache_dir(tmp_path, monkeypatch):
    """Patch _GARTH_CACHE_DIR to a temp location."""
    cache = tmp_path / "garth-cache"
    monkeypatch.setattr("engine.gateway.token_store._GARTH_CACHE_DIR", cache)
    return cache


def test_has_token_garth_cache_fallback(store_encrypted, garth_cache_dir, test_db):
    """has_token returns True when SQLite is empty but garth-cache has tokens."""
    # SQLite is empty
    assert not store_encrypted.has_token("garmin", "andrew")

    # Create garth-cache tokens (as garth would)
    user_cache = garth_cache_dir / "andrew"
    user_cache.mkdir(parents=True)
    (user_cache / "oauth1_token.json").write_text('{"token": "o1"}')
    (user_cache / "oauth2_token.json").write_text('{"token": "o2"}')

    # Now has_token should find them and import to SQLite
    assert store_encrypted.has_token("garmin", "andrew")

    # Verify they were imported into SQLite (subsequent call doesn't need cache)
    from engine.gateway.db import get_db
    db = get_db(test_db)
    count = db.execute(
        "SELECT COUNT(*) as cnt FROM wearable_token WHERE user_id = 'andrew' AND service = 'garmin'"
    ).fetchone()["cnt"]
    assert count == 2


def test_has_token_no_garth_cache(store_encrypted, garth_cache_dir):
    """has_token returns False when both SQLite and garth-cache are empty."""
    assert not store_encrypted.has_token("garmin", "andrew")


def test_has_token_garth_cache_non_garmin(store_encrypted, garth_cache_dir):
    """Garth-cache fallback only applies to garmin, not other services."""
    # Create a garth-cache dir for oura (shouldn't exist, but test the guard)
    user_cache = garth_cache_dir / "andrew"
    user_cache.mkdir(parents=True)
    (user_cache / "oauth1_token.json").write_text('{"token": "o1"}')

    # oura should NOT use garth-cache fallback
    assert not store_encrypted.has_token("oura", "andrew")
