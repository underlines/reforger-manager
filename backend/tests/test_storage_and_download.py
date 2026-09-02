"""S11 + S12 route tests — force re-download with a free-space guard, the
storage view, the orphan-closure rule, and ``DELETE /api/mods/{guid}/local``.

App-style route tests (the ``test_modpacks_routes`` pattern): a throwaway app
with ``get_session`` / ``get_current_user`` overridden and ``settings.mods_dir``
pointed at a temp dir so the addon cache is real but local. The ``mod_download``
enqueue is stubbed so no job factory / worker is required.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import mods as mods_api
from app.api import servers as servers_api
from app.api import storage as storage_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.jobs import job_manager
from app.core.security import get_current_user
from app.models import Mod, ModDependency, Server, ServerMod

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_C = "CCCCCCCCCCCCCCCC"

JOB_KIND_DOWNLOAD = "mod_download"


class StorageAndDownloadTests(unittest.IsolatedAsyncioTestCase):
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
        app.include_router(storage_api.router, prefix="/api")
        app.include_router(servers_api.router, prefix="/api")
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-storage-test-"))
        (self.tmp / "reforger" / "addons").mkdir(parents=True, exist_ok=True)
        self.addons = self.tmp / "reforger" / "addons"

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

    # ---------------------------------------------------------------- helpers
    async def _seed_mod(self, guid: str, *, size: int | None = 100, is_local: bool = True) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=f"Mod {guid[-1]}", size=size, is_local=is_local))
            await session.commit()

    def _make_addon_dir(self, guid: str, *, size_bytes: int = 0) -> Path:
        directory = self.addons / f"Mod_{guid}"
        directory.mkdir(parents=True, exist_ok=True)
        if size_bytes:
            (directory / "data.bin").write_bytes(b"x" * size_bytes)
        return directory

    async def _assign(self, guid: str, server_id: int = 1) -> None:
        async with self.sessions() as session:
            session.add(Server(id=server_id, name=f"srv{server_id}"))
            session.add(ServerMod(server_id=server_id, mod_guid=guid, load_order=0, enabled=True))
            await session.commit()

    async def _depend(self, parent: str, child: str) -> None:
        async with self.sessions() as session:
            session.add(ModDependency(mod_guid=parent, depends_on_guid=child, source="gproj"))
            await session.commit()

    # ------------------------------------------------ force re-download (S11)
    async def test_download_enqueues_mod_download_job(self) -> None:
        await self._seed_mod(GUID_A, size=1000)
        response = await self.client.post(f"/api/mods/{GUID_A}/download")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"job_id": 7, "kind": JOB_KIND_DOWNLOAD})
        kind, kwargs = self.enqueue_mock.call_args.args[0], self.enqueue_mock.call_args.kwargs
        self.assertEqual(kind, JOB_KIND_DOWNLOAD)
        self.assertEqual(kwargs["params"], {"guids": [GUID_A], "versions": {}})

    async def test_download_passes_optional_version(self) -> None:
        await self._seed_mod(GUID_A, size=1000)
        response = await self.client.post(f"/api/mods/{GUID_A}/download", json={"version": "1.2.3"})
        self.assertEqual(response.status_code, 202, response.text)
        kwargs = self.enqueue_mock.call_args.kwargs
        self.assertEqual(kwargs["params"], {"guids": [GUID_A], "versions": {GUID_A: "1.2.3"}})

    async def test_download_unknown_mod_is_404(self) -> None:
        response = await self.client.post(f"/api/mods/{GUID_B}/download")
        self.assertEqual(response.status_code, 404)

    async def test_download_refused_when_projected_exceeds_free_space(self) -> None:
        await self._seed_mod(GUID_A, size=1000)
        usage = SimpleNamespace(free=100, total=1000, used=900)
        with patch("shutil.disk_usage", return_value=usage):
            response = await self.client.post(f"/api/mods/{GUID_A}/download")
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertIn("1000", detail)  # projected
        self.assertIn("100", detail)   # available
        self.enqueue_mock.assert_not_awaited()

    async def test_download_refused_when_size_is_unknown(self) -> None:
        await self._seed_mod(GUID_A, size=None)
        response = await self.client.post(f"/api/mods/{GUID_A}/download")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("manually", response.json()["detail"])
        self.enqueue_mock.assert_not_awaited()

    async def test_apply_all_updates_guard_fires(self) -> None:
        await self._seed_mod(GUID_A, size=1000)
        usage = SimpleNamespace(free=100, total=1000, used=900)
        with patch("shutil.disk_usage", return_value=usage):
            response = await self.client.post("/api/mods/updates/apply")
        self.assertEqual(response.status_code, 409, response.text)
        self.enqueue_mock.assert_not_awaited()

    async def test_server_apply_updates_guard_fires(self) -> None:
        await self._seed_mod(GUID_A, size=1000)
        await self._assign(GUID_A)
        usage = SimpleNamespace(free=100, total=1000, used=900)
        with patch("shutil.disk_usage", return_value=usage):
            response = await self.client.post("/api/servers/1/mods/update/apply")
        self.assertEqual(response.status_code, 409, response.text)
        self.enqueue_mock.assert_not_awaited()

    # ------------------------------------------------------- storage view (S12)
    async def test_storage_orphan_closure_rule(self) -> None:
        # A is assigned to a server and depends on B (dependency-only, is_local,
        # in no server_mods row). C is a plain orphan.
        await self._seed_mod(GUID_A, size=100)
        await self._seed_mod(GUID_B, size=50)
        await self._seed_mod(GUID_C, size=10)
        await self._assign(GUID_A)
        await self._depend(GUID_A, GUID_B)

        response = await self.client.get("/api/storage")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()

        orphans = {o["guid"] for o in data["orphans"]}
        self.assertIn(GUID_C, orphans)
        self.assertNotIn(GUID_B, orphans)
        self.assertNotIn(GUID_A, orphans)

        kept = {k["guid"]: k for k in data["kept_as_dependency"]}
        self.assertIn(GUID_B, kept)
        self.assertEqual(kept[GUID_B]["required_by"], [GUID_A])
        self.assertNotIn(GUID_C, kept)

    async def test_storage_per_mod_sizes_sorted_desc(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        await self._seed_mod(GUID_B, size=50)
        self._make_addon_dir(GUID_A, size_bytes=500)
        self._make_addon_dir(GUID_B, size_bytes=200)

        response = await self.client.get("/api/storage")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual([p["guid"] for p in data["per_mod"]], [GUID_A, GUID_B])
        self.assertEqual(data["per_mod"][0]["bytes"], 500)
        self.assertEqual(data["per_mod"][1]["bytes"], 200)

    # ---------------------------------------------- DELETE /mods/{guid}/local
    async def test_delete_local_happy_path_keeps_row(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        self._make_addon_dir(GUID_A, size_bytes=100)

        response = await self.client.delete(f"/api/mods/{GUID_A}/local")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["is_local"])
        self.assertFalse((self.addons / f"Mod_{GUID_A}").exists())

        async with self.sessions() as session:
            row = await session.get(Mod, GUID_A)
            self.assertIsNotNone(row)
            self.assertFalse(row.is_local)

    async def test_delete_local_refused_while_server_running(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        self._make_addon_dir(GUID_A, size_bytes=100)
        with patch.object(mods_api.supervisor, "is_running", return_value=True):
            response = await self.client.delete(f"/api/mods/{GUID_A}/local")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue((self.addons / f"Mod_{GUID_A}").exists())

    async def test_delete_local_400_for_guid_without_resolvable_dir(self) -> None:
        await self._seed_mod(GUID_A, size=100)  # is_local but no addon dir on disk
        response = await self.client.delete(f"/api/mods/{GUID_A}/local")
        self.assertEqual(response.status_code, 400, response.text)

    async def test_delete_local_409_when_directly_referenced(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        await self._assign(GUID_A)
        self._make_addon_dir(GUID_A, size_bytes=100)

        response = await self.client.delete(f"/api/mods/{GUID_A}/local")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue((self.addons / f"Mod_{GUID_A}").exists())

    async def test_delete_local_409_for_kept_dependency(self) -> None:
        # B is only kept alive by the closure of assigned A — deleting it would
        # break a working definition, so the server-side re-check must refuse.
        await self._seed_mod(GUID_A, size=100)
        await self._seed_mod(GUID_B, size=50)
        await self._assign(GUID_A)
        await self._depend(GUID_A, GUID_B)
        self._make_addon_dir(GUID_B, size_bytes=100)

        response = await self.client.delete(f"/api/mods/{GUID_B}/local")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue((self.addons / f"Mod_{GUID_B}").exists())

    # ------------------------------------------------- DELETE /mods/{guid}
    async def test_delete_library_row_removes_unreferenced_nonlocal(self) -> None:
        await self._seed_mod(GUID_A, size=100, is_local=False)

        response = await self.client.delete(f"/api/mods/{GUID_A}")
        self.assertEqual(response.status_code, 204, response.text)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Mod, GUID_A))

    async def test_delete_library_row_removes_local_and_files(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        self._make_addon_dir(GUID_A, size_bytes=100)

        response = await self.client.delete(f"/api/mods/{GUID_A}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertFalse((self.addons / f"Mod_{GUID_A}").exists())
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Mod, GUID_A))

    async def test_delete_library_row_404_unknown(self) -> None:
        response = await self.client.delete(f"/api/mods/{GUID_B}")
        self.assertEqual(response.status_code, 404, response.text)

    async def test_delete_library_row_409_when_directly_referenced(self) -> None:
        await self._seed_mod(GUID_A, size=100, is_local=False)
        await self._assign(GUID_A)

        response = await self.client.delete(f"/api/mods/{GUID_A}")
        self.assertEqual(response.status_code, 409, response.text)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(Mod, GUID_A))

    async def test_delete_library_row_409_for_kept_dependency(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        await self._seed_mod(GUID_B, size=50, is_local=False)
        await self._assign(GUID_A)
        await self._depend(GUID_A, GUID_B)

        response = await self.client.delete(f"/api/mods/{GUID_B}")
        self.assertEqual(response.status_code, 409, response.text)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(Mod, GUID_B))

    async def test_delete_library_row_409_local_while_server_running(self) -> None:
        await self._seed_mod(GUID_A, size=100)
        self._make_addon_dir(GUID_A, size_bytes=100)
        with patch.object(mods_api.supervisor, "is_running", return_value=True):
            response = await self.client.delete(f"/api/mods/{GUID_A}")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue((self.addons / f"Mod_{GUID_A}").exists())
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(Mod, GUID_A))

    async def test_storage_lists_unreferenced_nonlocal_entries(self) -> None:
        # A: unreferenced, not local -> listed. B: assigned, not local -> not
        # listed. C: unreferenced but local -> a disk orphan, not listed here.
        await self._seed_mod(GUID_A, size=100, is_local=False)
        await self._seed_mod(GUID_B, size=50, is_local=False)
        await self._seed_mod(GUID_C, size=10, is_local=True)
        await self._assign(GUID_B)

        response = await self.client.get("/api/storage")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        entries = {e["guid"] for e in data["unreferenced_entries"]}
        self.assertEqual(entries, {GUID_A})
        self.assertIn(GUID_C, {o["guid"] for o in data["orphans"]})


if __name__ == "__main__":
    unittest.main()