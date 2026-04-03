"""OAuth Authorization Server Provider for MCP.

Implements the OAuthAuthorizationServerProvider protocol so Claude iOS
(and any MCP client using OAuth) can authenticate via magic invite links.

Flow:
  1. Claude registers itself as a client (dynamic client registration)
  2. User taps "Connect" -> browser opens /authorize with invite code
  3. Consent page auto-approves, redirects back with auth code
  4. Claude exchanges code for access/refresh tokens
  5. Claude uses Bearer token for MCP calls
  6. MCPAuthMiddleware resolves token -> person_id -> health_engine_user_id
"""

import json
import logging
import secrets
import time
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import get_db

logger = logging.getLogger("kiso.oauth")

# Token lifetimes
ACCESS_TOKEN_TTL = 3600 * 24  # 24 hours
REFRESH_TOKEN_TTL = 3600 * 24 * 90  # 90 days
AUTH_CODE_TTL = 600  # 10 minutes


class KisoAccessToken(AccessToken):
    """Extended access token that carries person_id for user resolution."""
    person_id: str = ""


class KisoRefreshToken(RefreshToken):
    """Extended refresh token that carries person_id."""
    person_id: str = ""
    resource: str | None = None


class KisoAuthorizationCode(AuthorizationCode):
    """Extended auth code that carries person_id."""
    person_id: str = ""


class KisoOAuthProvider:
    """SQLite-backed OAuth provider with magic invite flow."""

    def __init__(self, db_path: Path | str | None = None):
        self._db_path = db_path

    def _db(self):
        return get_db(self._db_path)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._db().execute(
            "SELECT client_json FROM oauth_client WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if not row:
            return None
        return OAuthClientInformationFull.model_validate_json(row["client_json"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._db().execute(
            "INSERT OR REPLACE INTO oauth_client (client_id, client_json) VALUES (?, ?)",
            (client_info.client_id, client_info.model_dump_json()),
        )
        self._db().commit()

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
        invite_code: str | None = None,
    ) -> str:
        """Return a redirect URL. With a valid invite, auto-approve and redirect
        back with an auth code. Without an invite, redirect to consent page."""
        if invite_code:
            # Validate invite
            row = self._db().execute(
                "SELECT person_id, used_at FROM oauth_invite WHERE code = ?",
                (invite_code,),
            ).fetchone()
            if row:
                person_id = row["person_id"]
                # Auto-approve: create auth code and redirect
                code = await self.create_authorization_code(
                    client_id=client.client_id,
                    code_challenge=params.code_challenge,
                    redirect_uri=str(params.redirect_uri),
                    scopes=params.scopes or [],
                    person_id=person_id,
                    resource=params.resource,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                )
                # Mark invite as used
                self._db().execute(
                    "UPDATE oauth_invite SET used_at = ? WHERE code = ?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ"), invite_code),
                )
                self._db().commit()

                return construct_redirect_uri(
                    str(params.redirect_uri),
                    code=code,
                    state=params.state,
                )

        # No invite or invalid invite: redirect to consent page
        # The consent page will collect the invite code and POST back
        consent_params = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "state": params.state,
            "code_challenge": params.code_challenge,
            "scopes": " ".join(params.scopes) if params.scopes else "",
            "resource": params.resource,
        }
        # Build consent URL (served by our gateway)
        from urllib.parse import urlencode
        return f"/oauth/consent?{urlencode({k: v for k, v in consent_params.items() if v})}"

    async def create_authorization_code(
        self,
        client_id: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
        person_id: str,
        resource: str | None = None,
        redirect_uri_provided_explicitly: bool = True,
    ) -> str:
        """Generate and store an authorization code. Returns the code string."""
        code = secrets.token_urlsafe(32)  # 256 bits
        expires_at = time.time() + AUTH_CODE_TTL

        self._db().execute(
            "INSERT INTO oauth_code (code, client_id, person_id, scopes, code_challenge, "
            "redirect_uri, redirect_uri_provided_explicitly, resource, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, person_id, " ".join(scopes), code_challenge,
             redirect_uri, int(redirect_uri_provided_explicitly), resource, expires_at),
        )
        self._db().commit()
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> KisoAuthorizationCode | None:
        row = self._db().execute(
            "SELECT * FROM oauth_code WHERE code = ? AND client_id = ?",
            (authorization_code, client.client_id),
        ).fetchone()
        if not row:
            return None
        return KisoAuthorizationCode(
            code=row["code"],
            client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
            resource=row["resource"],
            expires_at=row["expires_at"],
            person_id=row["person_id"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: KisoAuthorizationCode
    ) -> OAuthToken:
        person_id = authorization_code.person_id

        # Generate tokens
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = time.time()

        # Store access token
        self._db().execute(
            "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, "
            "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (access_token, "access", client.client_id, person_id,
             " ".join(authorization_code.scopes),
             authorization_code.resource,
             now + ACCESS_TOKEN_TTL),
        )

        # Store refresh token
        self._db().execute(
            "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, "
            "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (refresh_token, "refresh", client.client_id, person_id,
             " ".join(authorization_code.scopes),
             authorization_code.resource,
             now + REFRESH_TOKEN_TTL),
        )

        # Delete used auth code
        self._db().execute(
            "DELETE FROM oauth_code WHERE code = ?",
            (authorization_code.code,),
        )
        self._db().commit()

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> KisoAccessToken | None:
        row = self._db().execute(
            "SELECT * FROM oauth_token WHERE token = ? AND token_type = 'access' AND revoked = 0",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < time.time():
            return None
        return KisoAccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            resource=row["resource"],
            person_id=row["person_id"],
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> KisoRefreshToken | None:
        row = self._db().execute(
            "SELECT * FROM oauth_token WHERE token = ? AND token_type = 'refresh' "
            "AND client_id = ? AND revoked = 0",
            (refresh_token, client.client_id),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < time.time():
            return None
        return KisoRefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=int(row["expires_at"]) if row["expires_at"] else None,
            person_id=row["person_id"],
            resource=row["resource"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: KisoRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        person_id = refresh_token.person_id
        now = time.time()

        # Generate new tokens
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)

        # Revoke old tokens
        self._db().execute(
            "UPDATE oauth_token SET revoked = 1 WHERE token = ?",
            (refresh_token.token,),
        )

        # Store new tokens
        self._db().execute(
            "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, "
            "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_access, "access", client.client_id, person_id,
             " ".join(scopes), refresh_token.resource, now + ACCESS_TOKEN_TTL),
        )
        self._db().execute(
            "INSERT INTO oauth_token (token, token_type, client_id, person_id, scopes, "
            "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_refresh, "refresh", client.client_id, person_id,
             " ".join(scopes), refresh_token.resource, now + REFRESH_TOKEN_TTL),
        )
        self._db().commit()

        return OAuthToken(
            access_token=new_access,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=new_refresh,
        )

    async def revoke_token(self, token: KisoAccessToken | KisoRefreshToken) -> None:
        # Revoke the token itself
        self._db().execute(
            "UPDATE oauth_token SET revoked = 1 WHERE token = ?",
            (token.token,),
        )
        # Also revoke all tokens for the same client + person (both access and refresh)
        self._db().execute(
            "UPDATE oauth_token SET revoked = 1 WHERE client_id = ? AND person_id = ?",
            (token.client_id, token.person_id),
        )
        self._db().commit()
