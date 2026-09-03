"""S4: the mirrored read-only tool table, driven at the protocol level.

The SDK Client talks to a *fresh* ``mcp_app()`` per round-trip (the streamable
session manager is single-use and ASGITransport never sends lifespan — see
``test_mcp_auth.py``): the transport enters the inner app's lifespan manually,
one initialize per round-trip. ``list_tools`` must expose the whole mirrored
read surface (S5 registers the confirm-gated mutations on the same table —
asserted here so the anticipated names cannot silently drift);
``list_servers``/``get_server`` round-trip the real API path; an API 404
surfaces as a deterministic tool error carrying status + detail, not an opaque
crash.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base, get_session
from app.models import Job, McpToken, Mod, Server, User

EXPECTED_READ_TOOLS = {
    # servers
    "list_servers",
    "get_server",
    "get_server_config",
    "preview_server_config",
    "get_server_preflight",
    "get_server_stats",
    "get_server_players",
    "get_server_restart_schedule",
    # mods
    "list_mods",
    "get_mod",
    "get_mod_dependencies",
    "search_workshop_mods",
    # modpacks
    "list_modpacks",
    "get_modpack",
    # engine
    "get_engine_status",
    "check_engine_update",
    # jobs
    "list_jobs",
    "get_job",
    # files
    "list_server_files",
    "read_server_file",
    # backup / settings / storage / scenarios
    "export_backup",
    "get_settings",
    "get_storage_usage",
    "resolve_scenarios",
}

# The mutation names S4 anticipated; S5 registers them (confirmed below so a
# rename on the shared table cannot pass silently).
EXPECTED_MUTATION_NAMES = {
    # servers (definition CRUD + lifecycle + schedule/rcon + mod-set tools)
    "create_server",
    "update_server",
    "clone_server",
    "start_server",
    "stop_server",
    "delete_server",
    "schedule_server_restart",
    "cancel_server_restart",
    "send_rcon_command",
    "check_server_mod_updates",
    "apply_server_mod_updates",
    "pin_server_mod",
    "unpin_server_mod",
    # mods
    "scan_mods",
    # engine
    "update_engine",
    # jobs
    "prune_jobs",
    # files
    "write_server_file",
    # backup / settings
    "import_backup",
    "patch_settings",
}


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _first_text(result) -> str:
    return next(c.text for c in result.content if getattr(c, "type", None) == "text")


def _result_json(result) -> object:
    return json.loads(_first_text(result))


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

        # An MCP bearer token the middleware accepts (minted here; the REST
        # route needs no extra setup in this sqlite world, but seeding keeps
        # every fixture independent of the S2 token REST).
        self.raw_token = "rfm_" + "d4" * 16
        async with self.sessions() as session:
            session.add(McpToken(label="sdk", token_hash=_sha256(self.raw_token)))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self._session_patch.stop()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _seed_server(self, name: str = "main") -> Server:
        async with self.sessions() as session:
            row = Server(name=name)
            session.add(row)
            await session.commit()
            return row

    async def _seed_mod(self, guid: str, name: str) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=name))
            await session.commit()

    @asynccontextmanager
    async def mcp_session(self):
        """A fresh mcp_app() per use: lifespan entered manually, then initialize."""
        from app.mcp import mcp_app

        wrapper = mcp_app()
        inner = wrapper.app  # McpAuthMiddleware -> Starlette streamable app
        transport = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=wrapper),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.raw_token}"},
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


class McpReadToolSurfaceTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """list_tools: the whole read surface is registered and self-describing."""

    async def test_list_tools_exposes_the_read_surface(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()

        names = {tool.name for tool in tools.tools}
        # Every read tool survives S5's table extension, and the anticipated
        # mutation names exist alongside them (S5 asserts the full set).
        self.assertTrue(EXPECTED_READ_TOOLS | {"ping"} <= names)
        self.assertTrue(EXPECTED_MUTATION_NAMES <= names)

    async def test_every_tool_has_an_agent_oriented_description(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()

        by_name = {tool.name: tool for tool in tools.tools}
        for name in EXPECTED_READ_TOOLS:
            description = by_name[name].description or ""
            self.assertGreater(len(description), 80, name)
            self.assertNotIn("/api/", description, name)  # not a raw path string

    async def test_stats_description_warns_about_502_504_when_down(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()

        stats = next(tool for tool in tools.tools if tool.name == "get_server_stats")
        self.assertIn("502", stats.description)
        self.assertIn("504", stats.description)


class McpReadToolRoundTripTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Read tools return the API body verbatim over the real code path."""

    async def test_list_servers_returns_seeded_server(self) -> None:
        await self._seed_server("alpha")

        async with self.mcp_session() as session:
            result = await session.call_tool("list_servers", {})

        self.assertFalse(result.is_error, _first_text(result))
        rows = _result_json(result)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "alpha")

    async def test_get_server_round_trips(self) -> None:
        row = await self._seed_server("bravo")

        async with self.mcp_session() as session:
            result = await session.call_tool("get_server", {"server_id": row.id})

        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result)["name"], "bravo")

    async def test_unknown_server_404_is_deterministic_tool_error(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("get_server", {"server_id": 424242})

        self.assertTrue(result.is_error)
        text = _first_text(result)
        self.assertIn("404", text)
        self.assertIn("not found", text.lower())

    async def test_get_job_path_param_round_trip(self) -> None:
        async with self.sessions() as session:
            job = Job(kind="mod_sync")
            session.add(job)
            await session.commit()
            job_id = job.id

        async with self.mcp_session() as session:
            result = await session.call_tool("get_job", {"job_id": job_id})

        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result)["kind"], "mod_sync")

    async def test_list_mods_query_filters_round_trip(self) -> None:
        await self._seed_mod("A" * 16, "RHS")
        await self._seed_mod("B" * 16, "CBA")

        async with self.mcp_session() as session:
            filtered = await session.call_tool("list_mods", {"q": "rhs"})

        self.assertFalse(filtered.is_error, _first_text(filtered))
        rows = _result_json(filtered)
        self.assertEqual([m["name"] for m in rows], ["RHS"])

    async def test_preview_server_config_post_body_round_trip(self) -> None:
        row = await self._seed_server("saved-name")

        async with self.mcp_session() as session:
            result = await session.call_tool(
                "preview_server_config", {"server_id": row.id, "game_name": "draft-server"}
            )

        self.assertFalse(result.is_error, _first_text(result))
        body = _result_json(result)
        self.assertEqual(body["server_id"], row.id)
        # The draft is applied without persisting anything.
        self.assertEqual(body["config"]["game"]["name"], "draft-server")
        async with self.sessions() as session:
            fresh = await session.get(Server, row.id)
            self.assertEqual(fresh.game_name, None)


if __name__ == "__main__":
    unittest.main()
