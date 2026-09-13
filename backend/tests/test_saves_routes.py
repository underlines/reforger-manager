"""S14 route tests — HTTP layer for saves discovery + manager snapshots.

Routing/HTTP-layer tests only (discovery + snapshot logic already have their
own tests elsewhere). Harness copied from ``test_server_files_routes.py``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import server_saves as server_saves_api
from app.core.config import settings
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Server


class SavesRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(server_saves_api.router)
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-saves-test-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self.profiles_patch.start()

        async with self.sessions() as session:
            session.add(Server(id=1, name="srv1", scenario_game_id="{ECC61978EDCC2B5A}Missions/Scenario.conf"))
            await session.commit()

        self.supervisor_patches: list[patch] = []
        self.is_running_patch = patch.object(
            server_saves_api.supervisor, "is_running", lambda: False
        )
        self.is_running_patch.start()
        self.supervisor_patches.append(self.is_running_patch)

    async def asyncTearDown(self) -> None:
        for p in self.supervisor_patches:
            p.stop()
        self.profiles_patch.stop()
        await self.client.aclose()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _profile(self, server_id: int = 1) -> Path:
        return settings.profiles_dir / str(server_id)

    def _patch_running(self, server_id: int = 1) -> None:
        active_patch = patch.object(
            type(server_saves_api.supervisor),
            "active_server_id",
            PropertyMock(return_value=server_id),
        )
        active_patch.start()
        self.supervisor_patches.append(active_patch)
        self.is_running_patch.stop()
        running_patch = patch.object(
            server_saves_api.supervisor, "is_running", lambda: True
        )
        running_patch.start()
        self.supervisor_patches.append(running_patch)

    def _seed_save_point(
        self,
        server_id: int = 1,
        scenario_dir: str = "ScenarioDir",
        playthrough_nr: int = 0,
        save_point_nr: int = 0,
        uuid: str = "save-uuid-1",
        mission_resource: str = "{ECC61978EDCC2B5A}Missions/Scenario.conf",
    ) -> Path:
        sp_dir = (
            self._profile(server_id)
            / "profile"
            / ".save"
            / "game"
            / scenario_dir
            / f"playthrough{playthrough_nr:03d}"
            / f"savepoint{save_point_nr:03d}"
        )
        (sp_dir / "WorldState").mkdir(parents=True, exist_ok=True)
        (sp_dir / "WorldState" / "data.bin").write_bytes(b"data")
        meta = {
            "m_Id": uuid,
            "m_iSavedAtUnix": int(time.time()),
            "m_iPlaytimeSeconds": 100,
            "m_sGameVersion": "1.8.0",
            "m_sMissionResource": mission_resource,
            "m_sSavePointDisplayName": "Save 1",
            "m_sPlaythroughDisplayName": "Playthrough 1",
            "m_iStartedUnix": int(time.time()) - 1000,
            "m_iSavePointNr": save_point_nr,
            "m_iPlaythroughNr": playthrough_nr,
        }
        (sp_dir / "meta-info.json").write_text(json.dumps(meta), encoding="utf-8")
        return sp_dir

    # ------------------------------------------------------------------ tests
    async def test_arm_unknown_uuid_is_400(self) -> None:
        response = await self.client.post(
            "/servers/1/saves/not-a-real-uuid/arm", json={"sticky": False}
        )
        self.assertEqual(response.status_code, 400, response.text)

    async def test_running_guard_matrix_arming_exempt(self) -> None:
        self._seed_save_point(uuid="save-uuid-1")
        self._patch_running()

        # Mutating routes must 409 while running.
        snapshot_resp = await self.client.post(
            "/servers/1/saves/save-uuid-1/snapshot", json={"label": "snap-a"}
        )
        self.assertEqual(snapshot_resp.status_code, 409, snapshot_resp.text)

        delete_resp = await self.client.delete("/servers/1/saves/save-uuid-1")
        self.assertEqual(delete_resp.status_code, 409, delete_resp.text)

        upload_resp = await self.client.post(
            "/servers/1/saves/snapshots/upload",
            data={"label": "up", "restore_now": "false", "arm": "true"},
            files={"file": ("snap.tar.gz", b"not-a-real-archive")},
        )
        self.assertEqual(upload_resp.status_code, 409, upload_resp.text)

        restore_resp = await self.client.post(
            "/servers/1/saves/snapshots/some-id/restore", json={"arm": True}
        )
        self.assertEqual(restore_resp.status_code, 409, restore_resp.text)

        # Arming routes must NOT 409 while running.
        arm_resp = await self.client.post(
            "/servers/1/saves/save-uuid-1/arm", json={"sticky": False}
        )
        self.assertEqual(arm_resp.status_code, 200, arm_resp.text)

        arm_fresh_resp = await self.client.post(
            "/servers/1/saves/arm-fresh", json={"sticky": True}
        )
        self.assertEqual(arm_fresh_resp.status_code, 200, arm_fresh_resp.text)

        clear_resp = await self.client.delete("/servers/1/saves/selection")
        self.assertEqual(clear_resp.status_code, 200, clear_resp.text)

    async def test_delete_armed_save_point_clears_selection(self) -> None:
        self._seed_save_point(uuid="save-uuid-1")

        arm_resp = await self.client.post(
            "/servers/1/saves/save-uuid-1/arm", json={"sticky": False}
        )
        self.assertEqual(arm_resp.status_code, 200, arm_resp.text)
        self.assertEqual(arm_resp.json()["pinned_uuid"], "save-uuid-1")

        delete_resp = await self.client.delete("/servers/1/saves/save-uuid-1")
        self.assertEqual(delete_resp.status_code, 204, delete_resp.text)

        list_resp = await self.client.get("/servers/1/saves")
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        selection = list_resp.json()["selection"]
        self.assertEqual(selection["mode"], "latest")
        self.assertIsNone(selection["pinned_uuid"])


if __name__ == "__main__":
    unittest.main()
