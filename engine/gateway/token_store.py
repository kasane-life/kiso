"""Unified token storage for wearable services.

Tokens are stored at:
  ~/.config/health-engine/tokens/<service>/<user_id>/

Garmin tokens stay in garth format (oauth1_token.json, oauth2_token.json)
for backward compatibility. Other services use a single token.json.
"""

import json
import os
from pathlib import Path


_BASE_DIR = Path(os.path.expanduser("~/.config/health-engine/tokens"))


class TokenStore:
    """Manage wearable auth tokens per service and user."""

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else _BASE_DIR

    def _token_dir(self, service: str, user_id: str) -> Path:
        return self.base_dir / service / user_id

    def save_token(self, service: str, user_id: str, data: dict) -> Path:
        """Save token data as JSON. Returns the directory path."""
        td = self._token_dir(service, user_id)
        td.mkdir(parents=True, exist_ok=True)
        token_path = td / "token.json"
        with open(token_path, "w") as f:
            json.dump(data, f, indent=2)
        return td

    def load_token(self, service: str, user_id: str) -> dict | None:
        """Load token data. Returns None if not found."""
        token_path = self._token_dir(service, user_id) / "token.json"
        if not token_path.exists():
            return None
        with open(token_path) as f:
            return json.load(f)

    def has_token(self, service: str, user_id: str) -> bool:
        """Check if tokens exist for a service/user combo."""
        td = self._token_dir(service, user_id)
        if not td.exists():
            return False
        # Garmin uses garth format (multiple files), others use token.json
        return any(td.iterdir())

    def garmin_token_dir(self, user_id: str = "default") -> Path:
        """Get the garth-compatible token directory for Garmin.

        Garmin tokens are stored as garth dumps (oauth1_token.json,
        oauth2_token.json) directly in the directory, not wrapped in
        a single token.json.
        """
        td = self._token_dir("garmin", user_id)
        td.mkdir(parents=True, exist_ok=True)
        return td
