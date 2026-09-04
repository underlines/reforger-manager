"""S5: the confirm-gated mutation tools, driven at the protocol level.

Same harness as the read-tool tests (a *fresh* ``mcp_app()`` per round-trip —
the streamable session manager is single-use and ASGITransport never sends
lifespan — so the inner app's lifespan is entered manually, one initialize per
round-trip). The contract under test:

- a confirm-gated tool called without ``confirm=true`` fails with the
  deterministic 400-style tool error, and the API state provably does not
  change (asserted through the API's own read path);
- with ``confirm=true`` the real API call happens (the supervisor is mocked
  where the route would touch process state — no engine binary, no spawn);
- job-returning tools answer with the API's ``JobEnqueuedOut`` ({job_id,
  kind}), with the job manager's ``enqueue`` mocked (this sqlite harness
  registers no handlers); their descriptions document ``wait_for_job``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import servers as servers_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.jobs import job_manager
from app.models import ENGINE_SINGLETON_ID, Engine, McpToken, Server, ServerMod, User

# Every mutation tool S5 registers (grouped per PLAN §4's starred list).
MUTATION_TOOLS = {
    # servers (definition CRUD + lifecycle + schedule/rcon + server-scoped mods)
    "start_server",
    "stop_server",
    "delete_server",
    "create_server",
    "update_server",
    "clone_server",
    "schedule_server_restart",
    "cancel_server_restart",
    "send_rcon_command",
    "check_server_mod_updates",
    "apply_server_mod_updates",
    "pin_server_mod",
    "unpin_server_mod",
    # mods
    "scan_mods",
    "add_mod",
    "delete_mod",
    "delete_mod_local",
    "download_mod",
    "verify_mods",
    "check_all_mod_updates",
    "apply_all_mod_updates",
    # modpacks
    "create_modpack",
    "create_modpack_from_server",
    "update_modpack",
    "delete_modpack",
    "apply_modpack",
    # engine
    "update_engine",
    # jobs
    "cancel_job",
    "prune_jobs",
    "delete_job",
    # files
    "write_server_file",
    "delete_server_file",
    "mkdir_server_dir",
    "rename_server_file",
    "upload_server_files",
    # backup / settings
    "import_backup",
    "patch_settings",
}

JOB_RETURNING_TOOLS = {
    "apply_server_mod_updates",
    "check_server_mod_updates",
    "scan_mods",
    "update_engine",
    "download_mod",
    "verify_mods",
    "check_all_mod_updates",
    "apply_all_mod_updates",
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
    async def _seed_server(self, name: str = "main", **fields) -> Server:
        async with self.sessions() as session:
            row = Server(name=name, **fields)
            session.add(row)
            await session.commit()
            return row

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


class ConfirmGateTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """confirm=false/omitted -> 400-style tool error, API state untouched."""

    async def test_stop_server_without_confirm_is_refused_and_changes_nothing(self) -> None:
        row = await self._seed_server("main")
        stop = AsyncMock(return_value={"state": "stopped"})
        supervisor = SimpleNamespace(
            active_server_id=row.id, is_running=lambda: True, stop=stop
        )
        with patch.object(servers_api, "supervisor", supervisor):
            async with self.mcp_session() as session:
                refused = await session.call_tool("stop_server", {"server_id": row.id})
                self.assertTrue(refused.is_error)
                text = _first_text(refused)
                self.assertIn("400", text)
                self.assertIn("confirm", text)
                # State unchanged, asserted through the API's read path.
                read = await session.call_tool("get_server", {"server_id": row.id})
                self.assertFalse(read.is_error, _first_text(read))
                self.assertEqual(_result_json(read)["name"], "main")
        stop.assert_not_awaited()

    async def test_stop_server_with_confirm_reaches_the_api(self) -> None:
        row = await self._seed_server("main")
        stop = AsyncMock(return_value={"state": "stopped", "exit_code": 0})
        supervisor = SimpleNamespace(
            active_server_id=row.id, is_running=lambda: True, stop=stop
        )
        with patch.object(servers_api, "supervisor", supervisor):
            async with self.mcp_session() as session:
                done = await session.call_tool(
                    "stop_server", {"server_id": row.id, "confirm": True}
                )
        self.assertFalse(done.is_error, _first_text(done))
        self.assertEqual(_result_json(done), {"state": "stopped", "exit_code": 0})
        stop.assert_awaited_once()

    async def test_delete_server_with_explicit_false_is_refused(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "delete_server", {"server_id": row.id, "confirm": False}
            )
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            rows = _result_json(await session.call_tool("list_servers", {}))
        self.assertEqual([s["name"] for s in rows], ["main"])

    async def test_delete_server_with_confirm_deletes_via_the_api(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "delete_server", {"server_id": row.id, "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            rows = _result_json(await session.call_tool("list_servers", {}))
        self.assertEqual(rows, [])

    async def test_patch_settings_without_confirm_is_refused_and_changes_nothing(self) -> None:
        async with self.mcp_session() as session:
            before = _result_json(await session.call_tool("get_settings", {}))
            refused = await session.call_tool("patch_settings", {"nightly_check_hour": 7})
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            after = _result_json(await session.call_tool("get_settings", {}))
        self.assertEqual(before, after)

    async def test_patch_settings_with_confirm_patches_via_the_api(self) -> None:
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "patch_settings", {"nightly_check_hour": 7, "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            self.assertEqual(_result_json(done)["nightly_check_hour"], 7)
            after = _result_json(await session.call_tool("get_settings", {}))
            self.assertEqual(after["nightly_check_hour"], 7)

    async def test_update_server_without_confirm_is_refused_and_changes_nothing(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "update_server", {"server_id": row.id, "game_name": "renamed"}
            )
            self.assertTrue(refused.is_error)
            text = _first_text(refused)
            self.assertIn("400", text)
            self.assertIn("confirm", text)
            # State unchanged, asserted through the API's read path.
            read = await session.call_tool("get_server", {"server_id": row.id})
        self.assertIsNone(_result_json(read)["game_name"])

    async def test_update_server_with_confirm_patches_via_the_api(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "update_server", {"server_id": row.id, "game_name": "renamed", "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            read = await session.call_tool("get_server", {"server_id": row.id})
        self.assertEqual(_result_json(read)["game_name"], "renamed")

    async def test_clone_server_with_confirm_clones_via_the_api(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "clone_server", {"server_id": row.id, "name": "copy"}
            )
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            done = await session.call_tool(
                "clone_server", {"server_id": row.id, "name": "copy", "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            self.assertEqual(_result_json(done)["name"], "copy")
            rows = _result_json(await session.call_tool("list_servers", {}))
        self.assertEqual([s["name"] for s in rows], ["main", "copy"])

    async def test_create_modpack_from_server_gates_on_confirm_and_creates_via_the_api(self) -> None:
        from app.models import ServerMod

        row = await self._seed_server("main")
        async with self.sessions() as s:
            s.add(ServerMod(server_id=row.id, mod_guid="C" * 16, mod_name="CBA"))
            await s.commit()
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "create_modpack_from_server", {"server_id": row.id, "name": "snap"}
            )
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            done = await session.call_tool(
                "create_modpack_from_server",
                {"server_id": row.id, "name": "snap", "confirm": True},
            )
            self.assertFalse(done.is_error, _first_text(done))
            self.assertEqual(_result_json(done)["name"], "snap")
            self.assertIsNone(_result_json(done)["pins_note"])
            packs = _result_json(await session.call_tool("list_modpacks", {}))
        self.assertEqual([p["name"] for p in packs], ["snap"])

    async def test_schedule_server_restart_gates_on_confirm(self) -> None:
        row = await self._seed_server("main", rcon_password="secret")
        schedule = MagicMock(
            return_value={"armed": True, "restart_at": "t", "warn_at": [60], "seconds_remaining": 600}
        )
        supervisor = SimpleNamespace(
            active_server_id=row.id, is_running=lambda: True, schedule_restart=schedule
        )
        with patch.object(servers_api, "supervisor", supervisor):
            async with self.mcp_session() as session:
                refused = await session.call_tool(
                    "schedule_server_restart", {"server_id": row.id, "in_seconds": 600}
                )
                self.assertTrue(refused.is_error)
                self.assertIn("confirm", _first_text(refused))
                done = await session.call_tool(
                    "schedule_server_restart",
                    {"server_id": row.id, "in_seconds": 600, "confirm": True},
                )
        self.assertFalse(done.is_error, _first_text(done))
        self.assertTrue(_result_json(done)["armed"])
        schedule.assert_called_once_with(600, [300, 60, 10])

    async def test_cancel_server_restart_without_confirm_is_refused(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            refused = await session.call_tool("cancel_server_restart", {"server_id": row.id})
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            read = await session.call_tool("get_server", {"server_id": row.id})
        self.assertFalse(read.is_error, _first_text(read))

    async def test_cancel_server_restart_with_confirm_returns_armed_false(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "cancel_server_restart", {"server_id": row.id, "confirm": True}
            )
        self.assertFalse(done.is_error, _first_text(done))
        self.assertEqual(_result_json(done), {"armed": False})

    async def test_send_rcon_command_without_confirm_never_reaches_rcon(self) -> None:
        row = await self._seed_server("main", rcon_password="secret")
        client = MagicMock()
        with (
            patch.object(servers_api, "supervisor", SimpleNamespace(active_server_id=row.id, is_running=lambda: True)),
            patch.object(servers_api, "RconClient", return_value=client),
        ):
            async with self.mcp_session() as session:
                refused = await session.call_tool(
                    "send_rcon_command", {"server_id": row.id, "command": "#lock"}
                )
        self.assertTrue(refused.is_error)
        self.assertIn("confirm", _first_text(refused))
        client.command.assert_not_called()

    async def test_send_rcon_command_with_confirm_runs_via_the_api(self) -> None:
        row = await self._seed_server("main", rcon_password="secret")
        client = MagicMock()
        client.__aenter__.return_value = client
        client.connect = AsyncMock()
        client.command = AsyncMock(return_value="Locking the session")
        with (
            patch.object(servers_api, "supervisor", SimpleNamespace(active_server_id=row.id, is_running=lambda: True)),
            patch.object(servers_api, "RconClient", return_value=client),
        ):
            async with self.mcp_session() as session:
                done = await session.call_tool(
                    "send_rcon_command",
                    {"server_id": row.id, "command": "#lock", "confirm": True},
                )
        self.assertFalse(done.is_error, _first_text(done))
        self.assertEqual(_result_json(done), {"response": "Locking the session"})
        client.command.assert_awaited_once_with("#lock")

    async def test_pin_server_mod_gates_on_confirm_and_pins_via_the_api(self) -> None:
        row = await self._seed_server("main")
        guid = "C" * 16
        async with self.sessions() as session:
            session.add(Engine(id=ENGINE_SINGLETON_ID, installed_build="1.5.0"))
            session.add(ServerMod(server_id=row.id, mod_guid=guid, mod_name="CBA"))
            await session.commit()
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "pin_server_mod", {"server_id": row.id, "guid": guid, "version": "1.2.3"}
            )
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            # The assignment is still unpinned, read through the API's path.
            read = await session.call_tool("get_server", {"server_id": row.id})
            self.assertIsNone(_result_json(read)["mods"][0]["pinned_version"])
            done = await session.call_tool(
                "pin_server_mod",
                {"server_id": row.id, "guid": guid, "version": "1.2.3", "confirm": True},
            )
            self.assertFalse(done.is_error, _first_text(done))
            mods = _result_json(done)["mods"]
        self.assertEqual(mods[0]["pinned_version"], "1.2.3")
        self.assertEqual(mods[0]["pinned_at_build"], "1.5.0")

    async def test_unpin_server_mod_with_confirm_clears_the_pin(self) -> None:
        row = await self._seed_server("main")
        guid = "D" * 16
        async with self.sessions() as session:
            session.add(
                ServerMod(
                    server_id=row.id,
                    mod_guid=guid,
                    mod_name="CBA",
                    pinned_version="1.2.3",
                    pinned_at_build="1.5.0",
                    pinned_reason="compat",
                )
            )
            await session.commit()
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "unpin_server_mod", {"server_id": row.id, "guid": guid, "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            mods = _result_json(done)["mods"]
        self.assertIsNone(mods[0]["pinned_version"])
        self.assertIsNone(mods[0]["pinned_reason"])

    async def test_delete_mod_local_gates_on_confirm_and_reaches_the_api(self) -> None:
        from app.models import Mod

        async with self.sessions() as s:
            s.add(Mod(guid="B" * 16, is_local=False))
            await s.commit()
        async with self.mcp_session() as session:
            refused = await session.call_tool("delete_mod_local", {"guid": "B" * 16})
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            reached = await session.call_tool(
                "delete_mod_local", {"guid": "B" * 16, "confirm": True}
            )
        self.assertTrue(reached.is_error)
        self.assertIn("409", _first_text(reached))


class JobReturningToolTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Job tools answer with the API's JobEnqueuedOut ({job_id, kind})."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # No job handlers run in this sqlite harness; the enqueued job id is
        # all the route contract returns.
        self.enqueue = AsyncMock(return_value=7)
        self._enqueue_patch = patch.object(job_manager, "enqueue", new=self.enqueue)
        self._enqueue_patch.start()
        # The update-apply route free-space-guards against mods_dir.
        self.tmp = Path(tempfile.mkdtemp(prefix="rfmr-mcp-mutations-"))
        self._mods_patch = patch.object(settings, "mods_dir", self.tmp)
        self._mods_patch.start()

    async def asyncTearDown(self) -> None:
        self._mods_patch.stop()
        self._enqueue_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        await super().asyncTearDown()

    async def test_scan_mods_returns_job_enqueued_out(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("scan_mods", {"confirm": True})
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_sync"})
        self.enqueue.assert_awaited_once()

    async def test_download_mod_returns_job_enqueued_out(self) -> None:
        from app.models import Mod

        async with self.sessions() as s:
            s.add(Mod(guid="A" * 16, size=1))
            await s.commit()
        async with self.mcp_session() as session:
            result = await session.call_tool(
                "download_mod", {"guid": "A" * 16, "confirm": True}
            )
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_download"})

    async def test_verify_mods_returns_job_enqueued_out(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("verify_mods", {"confirm": True})
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "verify_repair"})

    async def test_check_all_mod_updates_returns_job_enqueued_out(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("check_all_mod_updates", {"confirm": True})
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_update_check"})

    async def test_apply_all_mod_updates_returns_job_enqueued_out(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("apply_all_mod_updates", {"confirm": True})
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_update_apply"})

    async def test_apply_server_mod_updates_returns_job_enqueued_out(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            result = await session.call_tool(
                "apply_server_mod_updates", {"server_id": row.id, "confirm": True}
            )
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_update_apply"})

    async def test_check_server_mod_updates_returns_job_enqueued_out(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            result = await session.call_tool(
                "check_server_mod_updates", {"server_id": row.id, "confirm": True}
            )
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "mod_update_check"})

    async def test_update_engine_returns_job_enqueued_out(self) -> None:
        async with self.mcp_session() as session:
            result = await session.call_tool("update_engine", {"confirm": True})
        self.assertFalse(result.is_error, _first_text(result))
        self.assertEqual(_result_json(result), {"job_id": 7, "kind": "engine_update"})

    async def test_job_tools_are_confirm_gated_too(self) -> None:
        async with self.mcp_session() as session:
            refused = await session.call_tool("scan_mods", {})
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            # A path param must be present or the SDK refuses before the gate.
            refused_check = await session.call_tool(
                "check_server_mod_updates", {"server_id": 31337}
            )
        self.assertTrue(refused_check.is_error)
        self.assertIn("confirm", _first_text(refused_check))
        self.enqueue.assert_not_awaited()


class MutationToolSurfaceTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """list_tools: every mutation registered, confirm declared, docs complete."""

    async def test_every_mutation_tool_is_registered(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        self.assertTrue(MUTATION_TOOLS <= names)

    async def test_confirm_tools_declare_an_optional_false_default(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()
        by_name = {tool.name: tool for tool in tools.tools}
        for name in sorted(MUTATION_TOOLS):
            schema = by_name[name].input_schema
            confirm = schema["properties"].get("confirm")
            self.assertIsNotNone(confirm, name)
            self.assertEqual(confirm.get("type"), "boolean", name)
            self.assertIs(confirm.get("default"), False, name)
            self.assertNotIn("confirm", schema.get("required", []), name)

    async def test_job_tool_descriptions_document_wait_for_job(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()
        by_name = {tool.name: tool for tool in tools.tools}
        for name in sorted(JOB_RETURNING_TOOLS):
            description = by_name[name].description or ""
            self.assertIn("wait_for_job(job_id)", description, name)

    async def test_every_mutation_description_states_its_side_effect(self) -> None:
        async with self.mcp_session() as session:
            tools = await session.list_tools()
        for tool in tools.tools:
            if tool.name in MUTATION_TOOLS:
                self.assertGreater(len(tool.description or ""), 80, tool.name)


class FileToolMechanicsTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """The file tools' transports: query params beside a body + multipart."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="rfmr-mcp-files-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir()
        self._profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self._profiles_patch.start()

    async def asyncTearDown(self) -> None:
        self._profiles_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        await super().asyncTearDown()

    async def test_write_server_file_round_trips_body_and_query_param(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            made = await session.call_tool(
                "mkdir_server_dir", {"server_id": row.id, "path": "cfg", "confirm": True}
            )
            self.assertFalse(made.is_error, _first_text(made))
            done = await session.call_tool(
                "write_server_file",
                {
                    "server_id": row.id,
                    "path": "cfg/server.json",
                    "content": '{"a": 1}',
                    "confirm": True,
                },
            )
            self.assertFalse(done.is_error, _first_text(done))
            read = await session.call_tool(
                "read_server_file", {"server_id": row.id, "path": "cfg/server.json"}
            )
        self.assertFalse(read.is_error, _first_text(read))
        self.assertEqual(_result_json(read)["content"], '{"a": 1}')
        target = self.profiles / str(row.id) / "cfg" / "server.json"
        self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')

    async def test_mkdir_upload_delete_round_trip(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            made = await session.call_tool(
                "mkdir_server_dir", {"server_id": row.id, "path": "uploads", "confirm": True}
            )
            self.assertFalse(made.is_error, _first_text(made))
            uploaded = await session.call_tool(
                "upload_server_files",
                {
                    "server_id": row.id,
                    "path": "uploads",
                    "files": [{"filename": "notes.txt", "content": "hello"}],
                    "confirm": True,
                },
            )
            self.assertFalse(uploaded.is_error, _first_text(uploaded))
            self.assertEqual(_result_json(uploaded)[0]["name"], "notes.txt")
            deleted = await session.call_tool(
                "delete_server_file",
                {"server_id": row.id, "path": "uploads/notes.txt", "confirm": True},
            )
            self.assertFalse(deleted.is_error, _first_text(deleted))
            listing = await session.call_tool(
                "list_server_files", {"server_id": row.id, "path": "uploads"}
            )
        self.assertEqual(_result_json(listing)["entries"], [])
        self.assertFalse((self.profiles / str(row.id) / "uploads" / "notes.txt").exists())

    async def test_file_tools_are_confirm_gated(self) -> None:
        row = await self._seed_server("main")
        async with self.mcp_session() as session:
            refused = await session.call_tool(
                "write_server_file", {"server_id": row.id, "path": "x.txt", "content": "hi"}
            )
        self.assertTrue(refused.is_error)
        self.assertIn("confirm", _first_text(refused))
        self.assertFalse((self.profiles / str(row.id) / "x.txt").exists())


class BackupImportMechanicsTests(_Fixture, unittest.IsolatedAsyncioTestCase):
    """Query params (dry_run/on_conflict) beside a flattened document body."""

    async def test_import_without_confirm_is_refused_and_imports_nothing(self) -> None:
        async with self.mcp_session() as session:
            refused = await session.call_tool("import_backup", {"servers": [], "modpacks": []})
            self.assertTrue(refused.is_error)
            self.assertIn("confirm", _first_text(refused))
            rows = _result_json(await session.call_tool("list_servers", {}))
        self.assertEqual(rows, [])

    async def test_import_dry_run_default_returns_the_plan(self) -> None:
        async with self.mcp_session() as session:
            done = await session.call_tool(
                "import_backup", {"servers": [], "modpacks": [], "confirm": True}
            )
            self.assertFalse(done.is_error, _first_text(done))
            plan = _result_json(done)
            rows = _result_json(await session.call_tool("list_servers", {}))
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["on_conflict"], "skip")
        self.assertEqual(plan["servers"], [])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
