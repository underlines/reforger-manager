"""Sprint 8 S1 — reference/orphan annotations on the mod library.

* ``GET /api/mods?refs=1`` adds ``cache_bytes`` / ``is_orphan`` /
  ``is_unreferenced`` / ``kept_by`` to each row.
* ``GET /api/mods/{guid}/references`` reports the servers, modpacks, and
  dependent mods that reference one guid.

App-style route tests, copied from ``test_storage_and_download``: a throwaway
FastAPI app with ``get_session`` / ``get_current_user`` overridden and
``settings.mods_dir`` pointed at a temp dir.
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
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import (
    Mod,
    ModDependency,
    Modpack,
    ModpackItem,
    Server,
    ServerMod,
)

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_C = "CCCCCCCCCCCCCCCC"

_REFS_FIELDS = ("cache_bytes", "is_orphan", "is_unreferenced", "kept_by")


class ModRefsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(mods_api.router, prefix="/api")
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-modrefs-test-"))
        (self.tmp / "reforger" / "addons").mkdir(parents=True, exist_ok=True)
        self.mods_dir_patch = patch.object(settings, "mods_dir", self.tmp)
        self.mods_dir_patch.start()

    async def asyncTearDown(self) -> None:
        self.mods_dir_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    async def _seed_mod(self, guid: str, name: str, *, is_local: bool = True) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=name, size=100, is_local=is_local))
            await session.commit()

    async def _assign(self, guid: str, server_id: int = 1, name: str = "Live Server") -> None:
        async with self.sessions() as session:
            session.add(Server(id=server_id, name=name))
            session.add(ServerMod(server_id=server_id, mod_guid=guid, load_order=0, enabled=True))
            await session.commit()

    async def _pack(self, guid: str, pack_id: int = 1, name: str = "Weekend Pack") -> None:
        async with self.sessions() as session:
            session.add(Modpack(id=pack_id, name=name))
            session.add(ModpackItem(modpack_id=pack_id, mod_guid=guid, load_order=0))
            await session.commit()

    async def _depend(self, parent: str, child: str) -> None:
        async with self.sessions() as session:
            session.add(ModDependency(mod_guid=parent, depends_on_guid=child, source="gproj"))
            await session.commit()

    # ------------------------------------------------------------- ?refs=1
    async def test_refs_marks_downloaded_unassigned_mod_as_orphan(self) -> None:
        await self._seed_mod(GUID_C, "Orphan Mod", is_local=True)

        response = await self.client.get("/api/mods", params={"refs": 1})
        self.assertEqual(response.status_code, 200, response.text)
        row = {m["guid"]: m for m in response.json()}[GUID_C]
        self.assertTrue(row["is_orphan"])
        self.assertFalse(row["is_unreferenced"])
        self.assertIsNone(row["kept_by"])
        self.assertEqual(row["cache_bytes"], 0)

    async def test_refs_assigned_mod_is_not_orphan(self) -> None:
        await self._seed_mod(GUID_A, "Assigned Mod", is_local=True)
        await self._assign(GUID_A)

        response = await self.client.get("/api/mods", params={"refs": 1})
        self.assertEqual(response.status_code, 200, response.text)
        row = {m["guid"]: m for m in response.json()}[GUID_A]
        self.assertFalse(row["is_orphan"])
        self.assertFalse(row["is_unreferenced"])
        self.assertIsNone(row["kept_by"])

    async def test_refs_dependency_only_local_mod_reports_kept_by(self) -> None:
        await self._seed_mod(GUID_A, "Parent Mod", is_local=True)
        await self._seed_mod(GUID_B, "Shared Core", is_local=True)
        await self._assign(GUID_A)
        await self._depend(GUID_A, GUID_B)

        response = await self.client.get("/api/mods", params={"refs": 1})
        self.assertEqual(response.status_code, 200, response.text)
        row = {m["guid"]: m for m in response.json()}[GUID_B]
        self.assertFalse(row["is_orphan"])
        self.assertEqual(row["kept_by"], ["Parent Mod"])

    async def test_list_without_refs_leaves_ref_fields_at_defaults(self) -> None:
        await self._seed_mod(GUID_C, "Orphan Mod", is_local=True)

        response = await self.client.get("/api/mods")
        self.assertEqual(response.status_code, 200, response.text)
        row = {m["guid"]: m for m in response.json()}[GUID_C]
        self.assertIsNone(row["cache_bytes"])
        self.assertFalse(row["is_orphan"])
        self.assertFalse(row["is_unreferenced"])
        self.assertIsNone(row["kept_by"])

    # ------------------------------------------------ GET /mods/{guid}/references
    async def test_references_lists_server_and_modpack(self) -> None:
        await self._seed_mod(GUID_A, "Assigned Mod", is_local=True)
        await self._assign(GUID_A, name="Live Server")
        await self._pack(GUID_A, name="Weekend Pack")

        response = await self.client.get(f"/api/mods/{GUID_A}/references")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["servers"], ["Live Server"])
        self.assertEqual(data["modpacks"], ["Weekend Pack"])
        self.assertEqual(data["required_by"], [])

    async def test_references_404_for_unknown_guid(self) -> None:
        response = await self.client.get(f"/api/mods/{GUID_B}/references")
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
