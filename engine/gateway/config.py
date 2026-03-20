"""Gateway configuration loader.

Reads ~/.config/health-engine/gateway.yaml for tunnel domain, port,
and future OAuth credentials (Oura, Whoop).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_CONFIG_PATH = Path(os.path.expanduser("~/.config/health-engine/gateway.yaml"))


@dataclass
class GatewayConfig:
    port: int = 18800
    tunnel_domain: str = ""
    hmac_secret: str = ""
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
        oura=raw.get("oura", {}),
        whoop=raw.get("whoop", {}),
    )
