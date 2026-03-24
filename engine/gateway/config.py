"""Gateway configuration loader.

Reads ~/.config/health-engine/gateway.yaml for tunnel domain, port,
and future OAuth credentials (Oura, Whoop).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_CONFIG_PATH = Path(
    os.environ.get("HE_CONFIG_DIR", os.path.expanduser("~/.config/health-engine"))
) / "gateway.yaml"


@dataclass
class GatewayConfig:
    port: int = 18800
    tunnel_domain: str = ""
    hmac_secret: str = ""
    api_token: str = ""
    sessions_dir: str = ""
    google_client_secrets_path: str = ""
    # Per-token person access control. Maps token -> list of allowed person IDs.
    # If empty, any valid token can access all persons (backward compat).
    # Example: {"tok_paul": ["paul-person-uuid"], "tok_andrew": ["andrew-deal-001"]}
    token_persons: dict = field(default_factory=dict)
    # Future: OAuth client credentials for Oura, Whoop
    oura: dict = field(default_factory=dict)
    whoop: dict = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        """Public URL if tunnel is configured, else localhost."""
        if self.tunnel_domain:
            return f"https://{self.tunnel_domain}"
        return f"http://localhost:{self.port}"


def load_gateway_config(path: str | Path | None = None) -> GatewayConfig:
    """Load gateway config from YAML. Returns defaults if file missing."""
    p = Path(path) if path else _CONFIG_PATH
    if not p.exists():
        return GatewayConfig()
    with open(p) as f:
        raw = yaml.safe_load(f) or {}
    return GatewayConfig(
        port=raw.get("port", 18800),
        tunnel_domain=raw.get("tunnel_domain", ""),
        hmac_secret=raw.get("hmac_secret", ""),
        api_token=raw.get("api_token", ""),
        sessions_dir=raw.get("sessions_dir", ""),
        google_client_secrets_path=raw.get("google_client_secrets_path", ""),
        token_persons=raw.get("token_persons", {}),
        oura=raw.get("oura", {}),
        whoop=raw.get("whoop", {}),
    )
