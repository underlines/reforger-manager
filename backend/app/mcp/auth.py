"""Bearer-token auth for the ``/mcp`` mount.

The middleware validates ``Authorization: Bearer rfm_...`` against the
``mcp_tokens`` table (sha256 lookup), refuses unknown / revoked / expired
tokens with 401, stamps ``last_used_at``, then mints an app JWT
(``create_access_token(settings.admin_username)``) into a ``contextvars.ContextVar``
that ``api.call`` attaches to every internal API request — so ``get_current_user``
runs the normal webui path downstream.

The DB session is opened and closed *before* dispatch: no handle is held across
the MCP call. The token lookup session comes from this module's own ``SessionLocal``
import (patch ``app.mcp.auth.SessionLocal`` in tests), mirroring the
``SessionLocal()`` pattern used by ``api/jobs.py`` — never a request-scoped session.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar, Token
from datetime import datetime, timezone

from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..core.config import settings
from ..core.db import SessionLocal
from ..core.security import create_access_token
from ..models import McpToken

_TOKEN_PREFIX = "rfm_"
_MIN_TOKEN_LEN = len(_TOKEN_PREFIX) + 16

_current_jwt: ContextVar[str | None] = ContextVar("mcp_jwt", default=None)


def current_jwt() -> str | None:
    """The app JWT minted for the current MCP request (None outside one)."""
    return _current_jwt.get()


def set_current_jwt(token: str) -> Token:
    return _current_jwt.set(token)


def reset_current_jwt(token: Token) -> None:
    _current_jwt.reset(token)


def _as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo; treat naive timestamps as UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _bearer_token(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            header = value.decode("latin-1")
            scheme, _, credential = header.partition(" ")
            if scheme.lower() == "bearer":
                return credential.strip()
    return None


async def _reject(send: Send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class McpAuthMiddleware:
    """ASGI middleware: validate the MCP token, then dispatch with the JWT stashed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan (session manager startup) and anything non-HTTP passes through.
            await self.app(scope, receive, send)
            return

        raw = _bearer_token(scope)
        if raw is None or not raw.startswith(_TOKEN_PREFIX) or len(raw) < _MIN_TOKEN_LEN:
            await _reject(send, "missing or malformed MCP token")
            return

        now = datetime.now(timezone.utc)
        # Short-lived on purpose: open -> look up -> stamp -> commit -> close,
        # all before the MCP app (and its task tree) ever runs.
        async with SessionLocal() as session:
            row = (
                await session.execute(select(McpToken).where(McpToken.token_hash == _hash(raw)))
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                await _reject(send, "invalid, expired or revoked MCP token")
                return
            if row.expires_at is not None and _as_utc(row.expires_at) <= now:
                await _reject(send, "invalid, expired or revoked MCP token")
                return
            row.last_used_at = now
            await session.commit()

        set_current_jwt(create_access_token(settings.admin_username))
        await self.app(scope, receive, send)
