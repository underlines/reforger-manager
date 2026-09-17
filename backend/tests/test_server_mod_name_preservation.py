"""``PATCH /api/servers/{id}`` must not blank ``mod_name`` when a caller's
``mods[]`` payload omits it.

Regression: an MCP/API caller that replaces a server's mod set with bare
``{mod_guid, load_order, enabled}`` entries (no ``mod_name``) used to blank
every surviving row's display name to ``NULL`` -- ``_apply_mods`` took
``mod_name`` straight from the payload with none of the "carry forward on
omission" protection it already gave the ``pinned_*`` columns. The generated
config (``resolved_mod_entries``) papered over this with its own
library-name fallback, so the server ran and joined fine the whole time; only
the raw ``GET /api/servers/{id}`` response -- what the Mods tab renders --
ever showed the gap.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Mod, Server, ServerMod

GUID_A = "AAAABBBBCCCCDDDD"
GUID_B = "1111222233334444"
GUID_NEW = "9999888877776666"


class ServerModNamePreservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
        app.dependency_overrides[get_current_user] = lambda: None

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in self._overridden:
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    async def _seed_mod(self, guid: str, name: str) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=name))
            await session.commit()

    async def _seed_server(self, mods: list[dict]) -> int:
        async with self.sessions() as session:
            server = Server(name="srv")
            for spec in mods:
                server.mods.append(
                    ServerMod(
                        mod_guid=spec["mod_guid"],
                        mod_name=spec.get("mod_name"),
                        load_order=spec.get("load_order", 0),
                        enabled=spec.get("enabled", True),
                    )
                )
            session.add(server)
            await session.commit()
            return server.id

    async def _patch_mods(self, server_id: int, mods: list[dict]) -> httpx.Response:
        return await self.client.patch(f"/api/servers/{server_id}", json={"mods": mods})

    # ---------------------------------------------------------------- tests
    async def test_surviving_row_keeps_its_name_when_payload_omits_it(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        server_id = await self._seed_server(
            [{"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0}]
        )

        # The exact regression shape: reorder/toggle via bare guid+load_order,
        # no mod_name echoed back.
        response = await self._patch_mods(
            server_id, [{"mod_guid": GUID_A, "load_order": 5, "enabled": True}]
        )
        self.assertEqual(response.status_code, 200, response.text)
        mods = {m["mod_guid"]: m for m in response.json()["mods"]}
        self.assertEqual(mods[GUID_A]["mod_name"], "Mod A")
        self.assertEqual(mods[GUID_A]["load_order"], 5)

    async def test_explicit_name_in_payload_still_overwrites(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        server_id = await self._seed_server(
            [{"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0}]
        )
        response = await self._patch_mods(
            server_id, [{"mod_guid": GUID_A, "mod_name": "Renamed", "load_order": 0}]
        )
        self.assertEqual(response.status_code, 200, response.text)
        mods = {m["mod_guid"]: m for m in response.json()["mods"]}
        self.assertEqual(mods[GUID_A]["mod_name"], "Renamed")

    async def test_new_row_with_no_name_resolves_from_library(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_mod(GUID_B, "Mod B")
        server_id = await self._seed_server(
            [{"mod_guid": GUID_A, "mod_name": "Mod A", "load_order": 0}]
        )
        # Add GUID_B (in the library) via a bare guid+load_order entry, same
        # shape a script would build from a list of GUIDs it doesn't have
        # display names for.
        response = await self._patch_mods(
            server_id,
            [
                {"mod_guid": GUID_A, "load_order": 0},
                {"mod_guid": GUID_B, "load_order": 1},
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        mods = {m["mod_guid"]: m for m in response.json()["mods"]}
        self.assertEqual(mods[GUID_A]["mod_name"], "Mod A")
        self.assertEqual(mods[GUID_B]["mod_name"], "Mod B")

    async def test_new_row_with_no_name_and_not_in_library_stays_null(self) -> None:
        server_id = await self._seed_server([])
        response = await self._patch_mods(
            server_id, [{"mod_guid": GUID_NEW, "load_order": 0}]
        )
        self.assertEqual(response.status_code, 200, response.text)
        mods = {m["mod_guid"]: m for m in response.json()["mods"]}
        self.assertIsNone(mods[GUID_NEW]["mod_name"])


if __name__ == "__main__":
    unittest.main()
