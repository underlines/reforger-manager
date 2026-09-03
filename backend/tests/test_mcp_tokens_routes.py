"""Route-level checks for the MCP token API (sprint 4 S2): POST /api/mcp/tokens
mints a raw ``rfm_...`` token shown exactly once (only the sha256 hash is
stored), GET omits the hash, DELETE soft-revokes idempotently, and the routes
401 without credentials.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import McpToken


class _RouteFixture:
    """Shared in-memory app fixture; concrete classes stay IsolatedAsyncioTestCase."""

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

        self._overridden = (get_session, get_current_user)
        app.dependency_overrides[get_session] = override_session

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _create(self, **body) -> httpx.Response:
        return await self.client.post("/api/mcp/tokens", json=body or {"label": "cli"})

    async def _rows(self) -> list[McpToken]:
        async with self.sessions() as session:
            return list(
                (await session.execute(select(McpToken).order_by(McpToken.id))).scalars().all()
            )


class McpTokenRouteTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Token routes never use the user object; unauthenticated is fine here.
        self.app.dependency_overrides[get_current_user] = lambda: None

    # ----------------------------------------------------------------- tests
    async def test_create_returns_201_and_raw_token_exactly_once(self) -> None:
        response = await self._create(label="claude code")

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        raw = body["token"]
        self.assertTrue(raw.startswith("rfm_"))
        # Exactly one occurrence of the raw token in the one response that is
        # ever allowed to carry it.
        self.assertEqual(response.text.count(raw), 1)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.label, "claude code")
        self.assertEqual(row.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotEqual(row.token_hash, raw)
        self.assertIsNone(row.revoked_at)
        self.assertIsNone(row.expires_at)

    async def test_list_omits_hash_and_raw_token(self) -> None:
        created = (await self._create(label="cli")).json()
        digest = hashlib.sha256(created["token"].encode()).hexdigest()

        response = await self.client.get("/api/mcp/tokens")

        self.assertEqual(response.status_code, 200, response.text)
        tokens = response.json()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(
            set(tokens[0]),
            {"id", "label", "created_at", "last_used_at", "expires_at", "revoked_at"},
        )
        self.assertEqual(tokens[0]["label"], "cli")
        self.assertNotIn("token_hash", tokens[0])
        self.assertNotIn("token", tokens[0])
        # Neither the raw secret nor its digest ever appear in a listing.
        self.assertNotIn(created["token"], response.text)
        self.assertNotIn(digest, response.text)

    async def test_revoke_is_idempotent_204(self) -> None:
        token_id = (await self._create(label="revoceme")).json()["id"]

        first = await self.client.delete(f"/api/mcp/tokens/{token_id}")
        self.assertEqual(first.status_code, 204)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        revoked_at = rows[0].revoked_at
        self.assertIsNotNone(revoked_at)

        # Re-revoking is a no-op that keeps the original timestamp.
        second = await self.client.delete(f"/api/mcp/tokens/{token_id}")
        self.assertEqual(second.status_code, 204)
        self.assertEqual((await self._rows())[0].revoked_at, revoked_at)

        # Unknown ids are still 404.
        self.assertEqual((await self.client.delete("/api/mcp/tokens/999")).status_code, 404)

    async def test_past_expires_at_is_422(self) -> None:
        response = await self._create(label="late", expires_at="2020-01-01T00:00:00Z")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(await self._rows(), [])

    async def test_future_expires_at_is_accepted(self) -> None:
        response = await self._create(label="timed", expires_at="2099-01-01T00:00:00Z")

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual((await self._rows())[0].expires_at is not None, True)
        self.assertEqual(
            (await self.client.get("/api/mcp/tokens")).json()[0]["expires_at"].startswith("2099"),
            True,
        )


class McpTokenAuthTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    """No get_current_user override: the real dependency must reject these."""

    async def test_missing_token_is_401(self) -> None:
        self.assertEqual((await self.client.get("/api/mcp/tokens")).status_code, 401)

    async def test_garbage_token_is_401(self) -> None:
        response = await self.client.get(
            "/api/mcp/tokens", headers={"Authorization": "Bearer not-a-jwt"}
        )
        self.assertEqual(response.status_code, 401)

    async def test_create_without_auth_is_401(self) -> None:
        response = await self.client.post("/api/mcp/tokens", json={"label": "x"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(await self._rows(), [])

    async def test_revoke_without_auth_is_401(self) -> None:
        self.assertEqual((await self.client.delete("/api/mcp/tokens/1")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
