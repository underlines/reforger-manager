"""S2 — metadata self-heal: backfill ``Mod.size`` so the free-space guard can
proceed instead of hard-409-ing a never-downloaded mod.

Covered behaviours:
1. The update-check backfills ``size`` from the Workshop payload it already holds
   (and leaves an existing size untouched).
2. The download route self-heals a ``size = None`` row via one enrich before the
   free-space guard, then enqueues.
3. If that enrich raises, the original "no recorded size" 409 persists.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import mods as mods_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.jobs import job_manager
from app.core.security import get_current_user
from app.models import Mod
from app.mods import updates
from app.mods.workshop import WorkshopError

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
JOB_KIND_DOWNLOAD = "mod_download"


class _PayloadWorkshop:
    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads

    async def get_mod(self, guid: str) -> dict:
        return self.payloads[guid]


class UpdateCheckSizeBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all(
                [
                    Mod(guid=GUID_A, name="A", is_local=True, installed_version="1.0", size=None),
                    Mod(guid=GUID_B, name="B", is_local=True, installed_version="1.0", size=100),
                ]
            )
            await session.commit()
        self.workshop = _PayloadWorkshop(
            {
                GUID_A: {"version": "2.0", "name": "A", "size": 500},
                GUID_B: {"version": "2.0", "name": "B", "size": 999},
            }
        )
        self.patchers = [
            patch.object(updates, "SessionLocal", self.sessions),
            patch.object(updates, "workshop", self.workshop),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()

    async def test_update_check_backfills_size_and_leaves_existing_alone(self) -> None:
        await updates.check_updates("all")
        async with self.sessions() as session:
            a = await session.get(Mod, GUID_A)
            b = await session.get(Mod, GUID_B)
        self.assertEqual(a.size, 500)  # was None -> backfilled
        self.assertEqual(b.size, 100)  # was set -> untouched


class _FakeClient:
    def __init__(self, size: int | None = None, error: Exception | None = None) -> None:
        self._size = size
        self._error = error

    async def get_mod(self, guid: str) -> dict:
        if self._error is not None:
            raise self._error
        return {"id": guid, "name": "X", "size": self._size}

    async def get_versions(self, guid: str) -> list:
        return []

    async def get_scenarios(self, guid: str) -> list:
        return []

    async def get_dependencies(self, guid: str) -> list:
        return []


class DownloadSelfHealTests(unittest.IsolatedAsyncioTestCase):
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

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-selfheal-test-"))
        self.mods_dir_patch = patch.object(settings, "mods_dir", self.tmp)
        self.mods_dir_patch.start()
        self.enqueue_mock = AsyncMock(return_value=7)
        self.enqueue_patch = patch.object(job_manager, "enqueue", new=self.enqueue_mock)
        self.enqueue_patch.start()

    async def asyncTearDown(self) -> None:
        self.enqueue_patch.stop()
        self.mods_dir_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _seed(self, guid: str, *, size: int | None = None, is_local: bool = False) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=f"Mod {guid[-1]}", size=size, is_local=is_local))
            await session.commit()

    async def test_download_self_heals_null_size_and_enqueues(self) -> None:
        await self._seed(GUID_A, size=None)
        fake = _FakeClient(size=777)
        with patch("app.mods.freespace.workshop", fake):
            response = await self.client.post(f"/api/mods/{GUID_A}/download")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"job_id": 7, "kind": JOB_KIND_DOWNLOAD})
        self.enqueue_mock.assert_awaited_once()
        async with self.sessions() as session:
            row = await session.get(Mod, GUID_A)
            self.assertIsNotNone(row)
            self.assertEqual(row.size, 777)  # row was enriched

    async def test_download_409_persists_when_enrich_raises(self) -> None:
        await self._seed(GUID_A, size=None)
        fake = _FakeClient(error=WorkshopError("boom"))
        with patch("app.mods.freespace.workshop", fake):
            response = await self.client.post(f"/api/mods/{GUID_A}/download")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("manually", response.json()["detail"])
        self.enqueue_mock.assert_not_awaited()
        async with self.sessions() as session:
            row = await session.get(Mod, GUID_A)
            self.assertIsNone(row.size)


if __name__ == "__main__":
    unittest.main()
