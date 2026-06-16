"""WHOOP OAuth 2.0 — authorization code flow and rotating-token refresh.

Endpoints verified against https://developer.whoop.com:
    auth:  https://api.prod.whoop.com/oauth/oauth2/auth
    token: https://api.prod.whoop.com/oauth/oauth2/token

CLI usage:
    python -m whoop_mcp.auth              # auth flow if no tokens, else refresh
    python -m whoop_mcp.auth --force-reauth  # delete tokens and re-authorize
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import tokens as token_store
from .config import Config, load_config

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

SCOPES = [
    "offline",  # required for refresh tokens
    "read:profile",
    "read:recovery",
    "read:sleep",
    "read:cycles",
    "read:workout",
    "read:body_measurement",
]


def _build_auth_url(cfg: Config, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    expected_state: str = ""
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = dict(urllib.parse.parse_qsl(parsed.query))

        if "error" in params:
            self._reply(400, f"WHOOP returned error: {params.get('error')}")
            type(self).result = {"error": params.get("error", "unknown")}
            return

        if params.get("state") != type(self).expected_state:
            self._reply(400, "State mismatch. Possible CSRF.")
            type(self).result = {"error": "state_mismatch"}
            return

        code = params.get("code")
        if not code:
            self._reply(400, "Missing authorization code.")
            type(self).result = {"error": "missing_code"}
            return

        self._reply(200, "Authorization received. You can close this tab.")
        type(self).result = {"code": code}

    def _reply(self, status: int, message: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = f"<html><body><h2>{message}</h2></body></html>".encode("utf-8")
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default stderr access log.
        return


def _await_callback(cfg: Config, state: str) -> str:
    parsed = urllib.parse.urlparse(cfg.redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080

    _CallbackHandler.expected_state = state
    _CallbackHandler.result = {}

    server = http.server.HTTPServer((host, port), _CallbackHandler)
    try:
        while not _CallbackHandler.result:
            server.handle_request()
    finally:
        server.server_close()

    result = _CallbackHandler.result
    if "error" in result:
        raise RuntimeError(f"Authorization failed: {result['error']}")
    return result["code"]


def _tokens_from_response(
    data: dict[str, Any],
    *,
    fallback_refresh_token: str | None = None,
) -> token_store.Tokens:
    expires_in = int(data["expires_in"])
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

    new_refresh = data.get("refresh_token")
    if not new_refresh:
        if not fallback_refresh_token:
            raise RuntimeError(
                "Token response missing refresh_token and no fallback available. "
                "Did you request the 'offline' scope?"
            )
        # Defensive fallback only — should not happen with `offline` scope.
        print(
            "WARNING: refresh response had no refresh_token; reusing old one.",
            file=sys.stderr,
        )
        new_refresh = fallback_refresh_token

    return token_store.Tokens(
        access_token=data["access_token"],
        refresh_token=new_refresh,
        expires_at=expires_at,
        token_type=data.get("token_type", "bearer"),
        scope=data.get("scope"),
    )


def _exchange_code(cfg: Config, code: str) -> token_store.Tokens:
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return _tokens_from_response(response.json())


def refresh(current: token_store.Tokens, cfg: Config | None = None) -> token_store.Tokens:
    """Refresh the access token. Persists the rotated refresh token internally.

    Call sites do not need to remember to save — this function does it.
    """
    cfg = cfg or load_config()
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "scope": "offline",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    new_tokens = _tokens_from_response(
        response.json(),
        fallback_refresh_token=current.refresh_token,
    )
    token_store.save(new_tokens, cfg.tokens_path)
    return new_tokens


def get_valid_tokens(cfg: Config | None = None) -> token_store.Tokens:
    """Return non-expired tokens, refreshing if needed. Used by later phases."""
    cfg = cfg or load_config()
    current = token_store.load(cfg.tokens_path)
    if current is None:
        raise RuntimeError(
            f"No tokens found at {cfg.tokens_path}. Run `uv run whoop-auth` first."
        )
    if current.is_expired():
        return refresh(current, cfg)
    return current


def authorize(cfg: Config | None = None) -> token_store.Tokens:
    cfg = cfg or load_config()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(cfg, state)

    print("Opening WHOOP authorization URL in your browser...")
    print(auth_url)
    webbrowser.open(auth_url)

    code = _await_callback(cfg, state)
    new_tokens = _exchange_code(cfg, code)
    token_store.save(new_tokens, cfg.tokens_path)
    print(f"Authorized. Tokens persisted to {cfg.tokens_path}.")
    return new_tokens


def main() -> None:
    parser = argparse.ArgumentParser(prog="whoop-auth", description=__doc__)
    parser.add_argument(
        "--force-reauth",
        action="store_true",
        help="Delete existing tokens and re-run the authorization flow.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.force_reauth and cfg.tokens_path.exists():
        cfg.tokens_path.unlink()
        print(f"Removed {cfg.tokens_path}.")

    current = token_store.load(cfg.tokens_path)
    if current is None:
        authorize(cfg)
        return

    old_refresh = current.refresh_token
    new = refresh(current, cfg)
    rotated = new.refresh_token != old_refresh
    print(
        f"Token refresh OK. New refresh token persisted "
        f"(rotated: {rotated}). expires_at={new.expires_at.isoformat()}"
    )


if __name__ == "__main__":
    main()
