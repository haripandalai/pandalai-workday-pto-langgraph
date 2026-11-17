"""OAuth 2.0 authorization-code + PKCE client for the Workday Agent Gateway.

The Workday agent runs in Delegate mode, so tool calls execute as the user who
signed in. We sign in ONCE (as lmcneil) through /auth/login, store the tokens on
the persistent disk, and refresh them when they expire.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import Settings


class NeedsLogin(Exception):
    """No usable Workday token; an admin must visit /auth/login."""


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class TokenStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict | None:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)


class WorkdayAuth:
    PENDING_TTL = 600

    def __init__(
        self,
        settings: Settings,
        store: TokenStore | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
    ):
        """One instance per OAuth client: the default one (MCP tools) or the A2A bridge client."""
        self.s = settings
        self.store = store or TokenStore(settings.token_path)
        self.client_id = settings.client_id if client_id is None else client_id
        self.client_secret = settings.client_secret if client_secret is None else client_secret
        self.redirect_uri = settings.redirect_uri if redirect_uri is None else redirect_uri
        self._pending: dict[str, tuple[str, float]] = {}  # state -> (verifier, created)
        self._lock = asyncio.Lock()
        self._meta: dict | None = None

    # ---- endpoints (discovered, with documented fallback) -------------------
    async def endpoints(self) -> tuple[str, str]:
        if self._meta is None:
            url = f"{self.s.agent_host}/.well-known/oauth-authorization-server/{self.s.tenant}"
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    self._meta = r.json()
            except Exception:
                self._meta = {
                    "authorization_endpoint": f"{self.s.agent_host}/auth/authorize/{self.s.tenant}",
                    "token_endpoint": f"{self.s.agent_host}/auth/oauth2/{self.s.tenant}/token",
                }
        return self._meta["authorization_endpoint"], self._meta["token_endpoint"]

    # ---- login ------------------------------------------------------------
    async def login_url(self) -> str:
        if not (self.client_id and self.client_secret):
            raise RuntimeError("the OAuth client ID / secret for this sign-in are not set")
        authorize, _ = await self.endpoints()
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[1] < self.PENDING_TTL}
        state = secrets.token_urlsafe(24)
        verifier, challenge = make_pkce()
        self._pending[state] = (verifier, now)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.s.scope:
            params["scope"] = self.s.scope
        return f"{authorize}?{urlencode(params)}"

    async def exchange_code(self, code: str, state: str) -> None:
        entry = self._pending.pop(state, None)
        if not entry or time.time() - entry[1] > self.PENDING_TTL:
            raise ValueError("unknown or expired state; start again at /auth/login")
        _, token_url = await self.endpoints()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": entry[0],
        }
        await self._token_request(token_url, data, previous=None)

    # ---- tokens -----------------------------------------------------------
    async def _token_request(self, token_url: str, data: dict, previous: dict | None) -> dict:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(token_url, data=data)
        if r.status_code >= 400:
            # Never echo the response body into logs: it can include token material.
            raise RuntimeError(f"Workday token endpoint returned HTTP {r.status_code}")
        body = r.json()
        now = int(time.time())
        rec = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": now + int(body.get("expires_in", 3600)),
            "obtained_at": now,
        }
        self.store.save(rec)
        return rec

    async def get_access_token(self) -> str:
        async with self._lock:
            rec = self.store.load()
            if not rec:
                raise NeedsLogin("Workday is not connected yet")
            if rec["expires_at"] - time.time() > 60:
                return rec["access_token"]
            if not rec.get("refresh_token"):
                raise NeedsLogin("Workday token expired and no refresh token was issued")
            _, token_url = await self.endpoints()
            try:
                rec = await self._token_request(
                    token_url,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": rec["refresh_token"],
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    previous=rec,
                )
            except RuntimeError as e:
                raise NeedsLogin(f"Workday token refresh failed ({e})") from e
            return rec["access_token"]

    def status(self) -> dict:
        rec = self.store.load()
        if not rec:
            return {"connected": False}
        return {
            "connected": True,
            "expires_in_seconds": int(rec["expires_at"] - time.time()),
            "has_refresh_token": bool(rec.get("refresh_token")),
            "obtained_at": rec.get("obtained_at"),
        }
