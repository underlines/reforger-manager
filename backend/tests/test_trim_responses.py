"""S7 — trimmed mutation and listing responses.

* ``PATCH /api/servers/{id}`` drops the full ``mods`` array by default and
  reports ``mod_count``; ``?include=mods`` restores the old full body.
* ``GET /api/mods?compact=true`` drops ``required_by`` / ``versions`` /
  ``thumbnail``; the default (no param) keeps the full shape.
* The MCP ``list_mods`` filter model defaults to ``compact=True``.

Self-contained app-style route tests (no conftest): a throwaway FastAPI app with
``get_session`` / ``get_current_user`` overridden, as in ``test_mod_refs``.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import mods as mods_api
from app.api import servers as servers_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Mod, Server, ServerMod

GUID_A = "A" * 16
GUID_B = "B" * 16


class TrimResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(servers_api.router, prefix="/api")
        app.include_router(mods_api.router, prefix="/api")
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-trim-test-"))
        self.mods_dir_patch = patch.object(settings, "mods_dir", self.tmp)
        self.mods_dir_patch.start()

    async def asyncTearDown(self) -> None:
        self.mods_dir_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    async def _seed_server_with_mods(self) -> int:
        async with self.sessions() as session:
            server = Server(name="trim-me", max_players=32)
            session.add(server)
            await session.flush()
            session.add(ServerMod(server_id=server.id, mod_guid=GUID_A, load_order=0, enabled=True))
            session.add(ServerMod(server_id=server.id, mod_guid=GUID_B, load_order=1, enabled=True))
            await session.commit()
            return server.id

    async def _seed_mods(self) -> None:
        async with self.sessions() as session:
            session.add(
                Mod(guid=GUID_A, name="Alpha", size=100, thumbnail="http://x/a.png", is_local=True)
            )
            session.add(
                Mod(guid=GUID_B, name="Bravo", size=200, thumbnail="http://x/b.png", is_local=True)
            )
            await session.commit()

    # ------------------------------------------------- update_server trim
    async def test_update_server_default_trims_mods_and_counts(self) -> None:
        server_id = await self._seed_server_with_mods()

        response = await self.client.patch(f"/api/servers/{server_id}", json={"max_players": 48})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("mods", body)
        self.assertEqual(body["mod_count"], 2)
        self.assertEqual(body["max_players"], 48)

    async def test_update_server_include_mods_restores_full_array(self) -> None:
        server_id = await self._seed_server_with_mods()

        response = await self.client.patch(
            f"/api/servers/{server_id}",
            params={"include": "mods"},
            json={"max_players": 48},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("mods", body)
        self.assertEqual(len(body["mods"]), 2)

    # --------------------------------------------------- list_mods compact
    async def test_list_mods_compact_drops_bulky_fields(self) -> None:
        await self._seed_mods()

        response = await self.client.get("/api/mods", params={"compact": "true"})
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn("required_by", row)
            self.assertNotIn("versions", row)
            self.assertNotIn("thumbnail", row)

    async def test_list_mods_default_keeps_full_shape(self) -> None:
        await self._seed_mods()

        response = await self.client.get("/api/mods")
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn("required_by", row)
            self.assertIn("thumbnail", row)
            self.assertIn("guid", row)
            self.assertIn("installed_version", row)

    # ------------------------------------------------ MCP default compact
    async def test_mcp_list_mods_filter_defaults_compact(self) -> None:
        from app.mcp.tools import ModListFilters

        self.assertTrue(ModListFilters().compact)
        self.assertFalse(ModListFilters(compact=False).compact)


if __name__ == "__main__":
    unittest.main()