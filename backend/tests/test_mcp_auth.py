"""S3: /mcp auth middleware + API adapter + mount.

Missing / garbage / revoked / expired bearer tokens are 401'd by the ASGI
middleware before the MCP app ever runs (so GET /mcp can no longer fall through
to the SPA). A valid token round-trips a real SDK initialize against the
mounted app at exactly ``/mcp`` and the ``ping`` smoke tool returns the health
body. ``api.call`` is checked against the real app: it forwards the minted JWT
through the real ``get_current_user`` and maps non-2xx to ``ApiError``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import create_access_token, get_current_user
from app.models import McpToken, User


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class _Fixture:
    """Shared fixture: real app, sqlite sessions, patched middleware lookup."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.main import app

        self.app = app
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        self._overridden = (get_session,)
        app.dependency_overrides[get_session] = override_session
        # The middleware opens its own session; point it at the test DB.
        self._session_patch = patch("app.mcp.auth.SessionLocal", self.sessions)
        self._session_patch.start()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._session_patch.stop()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _seed_token(self, **fields) -> McpToken:
        row = McpToken(label=fields.pop("label", "t"), token_hash=fields.pop("token_hash"), **fields)
        async with self.sessions() as session:
            session.add(row)
            await session.commit()
        return row


class McpAuthRejectTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Bad credentials -> 401 from the middleware, never the MCP app or SPA."""

    async def test_get_mcp_without_token_is_401_json_not_index_html(self) -> None:
        response = await self.client.get("/mcp")

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertEqual(response.json()["detail"], "missing or malformed MCP token")
        self.assertNotIn("<html", response.text.lower())

    async def test_get_mcp_with_garbage_token_is_401(self) -> None:
        for header in ("Bearer not-a-token", "Basic dXNlcjpwYXNz", "Bearer rfm_"):
            response = await self.client.get("/mcp", headers={"Authorization": header})
            self.assertEqual(response.status_code, 401, header)

    async def test_unknown_rfm_token_is_401(self) -> None:
        response = await self.client.post(
            "/mcp", headers={"Authorization": "Bearer rfm_0000000000000000000000000"}, json={}
        )

        self.assertEqual(response.status_code, 401)

    async def test_revoked_token_is_401_and_last_used_not_touched(self) -> None:
        raw = "rfm_" + "b" * 32
        row = await self._seed_token(
            token_hash=_hash(raw), revoked_at=datetime.now(timezone.utc)
        )

        response = await self.client.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json={})

        self.assertEqual(response.status_code, 401)
        async with self.sessions() as session:
            fresh = await session.get(McpToken, row.id)
            self.assertIsNone(fresh.last_used_at)

    async def test_expired_token_is_401(self) -> None:
        raw = "rfm_" + "c" * 32
        await self._seed_token(
            token_hash=_hash(raw),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        response = await self.client.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json={})

        self.assertEqual(response.status_code, 401)


class McpInitializeTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """One happy-path test: the mounted app speaks real MCP at exactly /mcp."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Token creation via REST needs no real user in this sqlite world.
        self.app.dependency_overrides[get_current_user] = lambda: None
        self._overridden = (get_session, get_current_user)

    async def test_valid_token_initializes_and_ping_returns_health_body(self) -> None:
        created = await self.client.post("/api/mcp/tokens", json={"label": "sdk"})
        self.assertEqual(created.status_code, 201, created.text)
        raw = created.json()["token"]

        mount = next(r for r in self.app.routes if getattr(r, "path", None) == "/mcp")
        inner = mount.app.app  # McpAuthMiddleware -> Starlette streamable app
        transport = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {raw}"},
        )
        try:
            # ASGITransport never runs lifespans: start the session manager the
            # way main.py's lifespan does in production.
            async with inner.router.lifespan_context(inner):
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=transport
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        self.assertEqual(init.server_info.name, "reforger-manager")

                        tools = await session.list_tools()
                        self.assertIn("ping", [tool.name for tool in tools.tools])

                        ping = await session.call_tool("ping", {})
                        self.assertFalse(ping.is_error)
                        text = next(c for c in ping.content if getattr(c, "type", None) == "text")
                        self.assertEqual(json.loads(text.text), {"status": "ok"})
        finally:
            await transport.aclose()

        # The middleware stamped last_used_at during the round-trip.
        async with self.sessions() as session:
            rows = list((await session.execute(select(McpToken))).scalars().all())
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].last_used_at)


class McpAdapterTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """api.call: JWT forwarding through the real auth dependency + error mapping."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.sessions() as session:
            session.add(
                User(
                    username=settings.admin_username,
                    password_hash="not-a-real-hash",
                    is_admin=True,
                    is_active=True,
                )
            )
            await session.commit()

    async def test_call_forwards_minted_jwt_and_returns_body(self) -> None:
        from app.mcp import api as mcp_api
        from app.mcp.auth import reset_current_jwt, set_current_jwt

        handle = set_current_jwt(create_access_token(settings.admin_username))
        try:
            body = await mcp_api.call("GET", "/api/auth/me")
        finally:
            reset_current_jwt(handle)

        self.assertEqual(body.get("username"), settings.admin_username)

    async def test_call_without_jwt_hits_the_real_401(self) -> None:
        from app.mcp import api as mcp_api

        with self.assertRaises(mcp_api.ApiError) as caught:
            await mcp_api.call("GET", "/api/auth/me")
        self.assertEqual(caught.exception.status_code, 401)

    async def test_api_error_maps_status_and_detail(self) -> None:
        from app.mcp import api as mcp_api
        from app.mcp.auth import reset_current_jwt, set_current_jwt

        handle = set_current_jwt(create_access_token(settings.admin_username))
        try:
            with self.assertRaises(mcp_api.ApiError) as caught:
                await mcp_api.call("GET", "/api/servers/999")
        finally:
            reset_current_jwt(handle)

        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("not found", str(caught.exception.detail).lower())


if __name__ == "__main__":
    unittest.main()
