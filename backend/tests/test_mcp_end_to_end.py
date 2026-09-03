"""S7: the full protocol-level regression pass — one agent arc, end to end.

Everything happens in-process over the real mounted stack: the token is minted
through the webui's own path (real login -> real JWT -> POST /api/mcp/tokens),
then an SDK ``Client`` drives the middleware + adapter + tools over ASGITransport
through the entire lifecycle:

    server_overview -> create_server (confirm-gated) -> start (confirm) ->
    tail_log -> stats while down (deterministic 502, tolerated) -> stop
    (confirm) -> delete (confirm) -> revoke -> next MCP call 401s.

No network, no spawned process: the supervisor is stubbed at the exact seam the
routes use (the ``app.api.servers.supervisor`` binding, the ``SimpleNamespace``
+ recorder precedent from tests/test_mcp_tools_mutations.py), returning the
endpoints' documented payload shapes and recording the ids they pass; the A2S
query is stubbed to raise the connection error the "server down" path maps to
502 — the tool contract under test is the deterministic error surfacing, not
UDP. After revocation a *fresh* request/session is forced (the middleware
re-validates the bearer on every request, but a live SDK session must never be
trusted to prove a 401): a brand-new ``mcp_app()`` + transport answers the same
JSON-RPC initialize the SDK would send with the middleware's 401, and a fresh
SDK session fails to come up at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import servers as servers_api
from app.a2s.client import A2SConnectionError
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import hash_password
from app.models import ENGINE_SINGLETON_ID, McpToken, User

# The exact wire request an MCP client sends first — used verbatim for the
# post-revoke check so the 401 is proven at the protocol level, not inferred.
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "e2e", "version": "0"},
    },
}


def _first_text(result) -> str:
    return next(c.text for c in result.content if getattr(c, "type", None) == "text")


def _result_json(result) -> object:
    return json.loads(_first_text(result))


def _flatten_error(error: BaseException):
    """The SDK wraps transport failures in (nested) anyio ExceptionGroups."""
    yield error
    for sub in getattr(error, "exceptions", ()):
        yield from _flatten_error(sub)


class _Fixture:
    """Real app, sqlite sessions, patched middleware lookup (S3 pattern)."""

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

        # The single admin, with the real password -> the real webui login path
        # issues the JWT the token REST (and, minted by the middleware, the MCP
        # adapter) authenticates with.
        async with self.sessions() as session:
            session.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    is_admin=True,
                    is_active=True,
                )
            )
            await session.commit()

        # tail_log reads the profile dir; point it at a throwaway one.
        self.tmp = Path(tempfile.mkdtemp(prefix="rfmr-mcp-e2e-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self._profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self._profiles_patch.start()

        # The webui client (REST: login, token create/revoke).
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._profiles_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._session_patch.stop()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _login(self) -> str:
        response = await self.client.post(
            "/api/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    async def _mint_token(self, jwt: str, label: str) -> tuple[int, str]:
        """POST /api/mcp/tokens through the real route; (row id, raw token)."""
        response = await self.client.post(
            "/api/mcp/tokens",
            json={"label": label},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["token"].startswith("rfm_"), body["token"])
        return body["id"], body["token"]

    def _write_console_log(self, server_id: int, total_lines: int) -> None:
        logs = self.profiles / str(server_id) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "console.log").write_text(
            "\n".join(f"line {i:02d}" for i in range(1, total_lines + 1)) + "\n",
            encoding="utf-8",
        )

    @asynccontextmanager
    async def mcp_session(self, raw_token: str):
        """A fresh mcp_app() per use: lifespan entered manually, then initialize."""
        from app.mcp import mcp_app

        wrapper = mcp_app()
        inner = wrapper.app  # McpAuthMiddleware -> Starlette streamable app
        transport = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=wrapper),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        try:
            # ASGITransport never runs lifespans: start the session manager the
            # way main.py's lifespan does in production.
            async with inner.router.lifespan_context(inner):
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=transport
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        finally:
            await transport.aclose()

    async def _raw_initialize(self, raw_token: str):
        """One raw MCP initialize request on a brand-new app + transport."""
        from app.mcp import mcp_app

        wrapper = mcp_app()
        transport = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=wrapper),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        try:
            return await transport.post("http://testserver/mcp", json=_INITIALIZE)
        finally:
            await transport.aclose()


class FullAgentArcTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Token via the webui path -> whole server lifecycle over MCP -> revoke."""

    async def test_token_to_revocation_over_one_sdk_session(self) -> None:
        # 1. Fresh token via the REST route, behind a real login (webui path).
        jwt = await self._login()
        token_id, raw = await self._mint_token(jwt, "agent-arc")

        # The supervisor stub is the only seam mocked: it records the ids the
        # API hands it and answers with the endpoints' documented payload
        # shapes. No engine binary, no OS process, no disk side effects.
        started: list[int] = []
        stopped: list[int] = []

        async def fake_start(server_id: int) -> dict:
            started.append(server_id)
            return {"server_id": server_id, "pid": 4242,
                    "config_path": "config.json", "log_path": "console.log"}

        async def fake_stop() -> dict:
            stopped.append(started[-1] if started else -1)
            return {"server_id": started[-1] if started else None, "exit_code": 0}

        stub = SimpleNamespace(
            active_server_id=None, is_running=lambda: False,
            start=fake_start, stop=fake_stop,
        )
        # Stats while down: A2S "connection refused" -> the route's 502 path.
        a2s_down = AsyncMock(side_effect=A2SConnectionError("connection refused"))

        with patch.object(servers_api, "supervisor", stub), patch.object(
            servers_api, "query_a2s", a2s_down
        ):
            async with self.mcp_session(raw) as session:
                # 2. server_overview: the empty situation report.
                overview = _result_json(await session.call_tool("server_overview", {}))
                self.assertEqual(overview["servers"], [])
                self.assertIsNone(overview["running_server_id"])
                self.assertEqual(overview["recent_jobs"], [])
                self.assertEqual(overview["engine"]["id"], ENGINE_SINGLETON_ID)

                # 3. Create via MCP — confirm-gated like every write tool.
                refused = await session.call_tool("create_server", {"name": "arc"})
                self.assertTrue(refused.is_error)
                self.assertIn("confirm", _first_text(refused))

                made = _result_json(
                    await session.call_tool("create_server", {"name": "arc", "confirm": True})
                )
                self.assertEqual(made["name"], "arc")
                server_id = made["id"]

                # 4. Start (confirm): the API called the supervisor with the
                # created id; the payload shape survives the round-trip.
                stub.active_server_id = server_id
                up = _result_json(
                    await session.call_tool("start_server", {"server_id": server_id, "confirm": True})
                )
                self.assertEqual(up["server_id"], server_id)
                self.assertIn("pid", up)
                self.assertEqual(started, [server_id])

                # 5. tail_log: the last N entries of the fixture console log.
                self._write_console_log(server_id, total_lines=12)
                log = _result_json(
                    await session.call_tool("tail_log", {"server_id": server_id, "lines": 5})
                )
                self.assertEqual(log["server_id"], server_id)
                self.assertEqual(
                    [line["text"] for line in log["lines"]],
                    [f"line {i:02d}" for i in range(8, 13)],
                )
                self.assertEqual(log["lines"][-1]["line_number"], 12)

                # 6. Stats while down: a deterministic 502 tool error the agent
                # is told to read as "not running" — the arc simply continues.
                stats = await session.call_tool("get_server_stats", {"server_id": server_id})
                self.assertTrue(stats.is_error)
                self.assertIn("502", _first_text(stats))

                # 7. Stop (confirm): the running definition's id, payload intact.
                down = _result_json(
                    await session.call_tool("stop_server", {"server_id": server_id, "confirm": True})
                )
                self.assertEqual(down, {"server_id": server_id, "exit_code": 0})
                self.assertEqual(started, [server_id])
                self.assertEqual(stopped, [server_id])
                stub.active_server_id = None

                # 8. Delete (confirm), then the list is provably empty.
                gone = await session.call_tool(
                    "delete_server", {"server_id": server_id, "confirm": True}
                )
                self.assertFalse(gone.is_error, _first_text(gone))
                self.assertEqual(_result_json(await session.call_tool("list_servers", {})), [])

        # The middleware stamped last_used_at on the accepted requests.
        async with self.sessions() as db:
            row = await db.get(McpToken, token_id)
        self.assertIsNotNone(row.last_used_at)

        # 9. Revoke via the REST route (204), then the very next MCP call —
        # a fresh request carrying the same bearer — is a middleware 401.
        revoked = await self.client.delete(
            f"/api/mcp/tokens/{token_id}", headers={"Authorization": f"Bearer {jwt}"}
        )
        self.assertEqual(revoked.status_code, 204)

        response = await self._raw_initialize(raw)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "invalid, expired or revoked MCP token")
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

        # ...and a *fresh SDK session* cannot come up either: its initialize
        # dies on the transport 401, surfaced by the SDK as MCPError (maybe
        # wrapped in an anyio ExceptionGroup) — never a silent success.
        with self.assertRaises(BaseException) as caught:
            async with self.mcp_session(raw) as session:
                await session.call_tool("server_overview", {})
        errors = list(_flatten_error(caught.exception))
        self.assertTrue(
            any(isinstance(exc, MCPError) or "401" in str(exc) for exc in errors),
            repr(caught.exception),
        )


class RevokedTokenFreshSessionTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """The post-revoke assertion in isolation: fresh session, hard 401."""

    async def test_revoked_token_rejects_initialize_on_a_fresh_transport(self) -> None:
        jwt = await self._login()
        token_id, raw = await self._mint_token(jwt, "short-lived")

        # Sanity: the token works before revocation (initialize round-trips).
        async with self.mcp_session(raw) as session:
            tools = await session.list_tools()
        self.assertIn("server_overview", [tool.name for tool in tools.tools])

        response = await self.client.delete(
            f"/api/mcp/tokens/{token_id}", headers={"Authorization": f"Bearer {jwt}"}
        )
        self.assertEqual(response.status_code, 204)

        rejected = await self._raw_initialize(raw)
        self.assertEqual(rejected.status_code, 401)
        self.assertNotIn("<html", rejected.text.lower())  # never the SPA either


if __name__ == "__main__":
    unittest.main()
