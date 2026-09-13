"""S5 — manager-owned save snapshots (``app.servers.save_snapshots``).

Service-layer only (no HTTP routes yet). In-memory sqlite, self-contained (no
``conftest.py`` in this repo) — harness copied from ``test_saves_discovery.py``
/ ``test_server_files_routes.py``: a real temp dir stands in for
``settings.profiles_dir``, so a seeded save-point directory is real on disk.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base
from app.models import Server
from app.servers.save_snapshots import (
    InvalidArchiveError,
    SnapshotCapExceededError,
    create_snapshot,
    restore_snapshot,
    store_upload,
    validate_archive,
)
from app.servers.saves import discover


class SaveSnapshotsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-save-snapshots-test-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self.profiles_patch.start()

    async def asyncTearDown(self) -> None:
        self.profiles_patch.stop()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _game_root(self, server_id: int = 1) -> Path:
        return self.profiles / str(server_id) / "profile" / ".save" / "game"

    def _snapshots_dir(self, server_id: int = 1) -> Path:
        return self.profiles / str(server_id) / "snapshots"

    def _write_savepoint(
        self,
        server_id: int,
        scenario_dir: str,
        playthrough_nr: int,
        save_point_nr: int,
        *,
        meta: dict | None = None,
    ) -> Path:
        pt_name = f"playthrough{playthrough_nr:03d}"
        sp_name = f"savepoint{save_point_nr:03d}"
        path = self._game_root(server_id) / scenario_dir / pt_name / sp_name
        path.mkdir(parents=True, exist_ok=True)
        ws = path / "WorldState"
        ws.mkdir(exist_ok=True)
        (ws / "11111111-1111-1111-1111-111111111111.blob").write_bytes(os.urandom(32))

        default = {
            "m_Id": f"uuid-{scenario_dir}-{playthrough_nr}-{save_point_nr}",
            "m_eType": 2,
            "m_iSavedAtUnix": 1_700_000_000 + playthrough_nr * 10_000 + save_point_nr * 100,
            "m_iSavePointNr": save_point_nr,
            "m_sSavePointDisplayName": "",
            "m_sMissionResource": "{5F5F49B8F5D935A2}Missions/Test.conf",
            "m_iPlaythroughNr": playthrough_nr,
            "m_sPlaythroughDisplayName": "",
            "m_iStartedUnix": 1_700_000_000,
            "m_iPlaytimeSeconds": 100,
            "m_sGameVersion": "1.8.0.13",
            "m_aUsedAddons": [],
        }
        if meta:
            default.update(meta)
        (path / "meta-info.json").write_text(json.dumps(default), encoding="utf-8")
        return path

    async def _add_server(self, **kwargs) -> Server:
        server = Server(id=kwargs.pop("id", 1), name=kwargs.pop("name", "srv"), **kwargs)
        async with self.sessions() as session:
            session.add(server)
            await session.commit()
            await session.refresh(server)
        return server

    async def _first_save_uuid(self, session: AsyncSession, server: Server) -> str:
        scenarios = await discover(session, server)
        return scenarios[0].playthroughs[0].save_points[0].uuid

    @staticmethod
    def _bad_archive_bytes(build) -> io.BytesIO:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            build(tf)
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------ tests
    async def test_snapshot_round_trip_byte_for_byte(self) -> None:
        await self._add_server()
        sp_path = self._write_savepoint(1, "ScenA", 0, 1)
        world_file = sp_path / "WorldState" / "11111111-1111-1111-1111-111111111111.blob"
        original_world_bytes = world_file.read_bytes()
        original_meta = (sp_path / "meta-info.json").read_text(encoding="utf-8")

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            save_uuid = await self._first_save_uuid(session, server)
            snap = await create_snapshot(session, server, save_uuid, "before wipe")

        # Prove restore recreates the directory from the archive, not by luck.
        shutil.rmtree(sp_path)
        self.assertFalse(sp_path.exists())

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            result = await restore_snapshot(session, server, snap.snapshot_id, arm=True)
            await session.commit()

        self.assertEqual(result.restored_uuid, save_uuid)
        self.assertTrue(sp_path.is_dir())
        self.assertEqual(
            (sp_path / "WorldState" / "11111111-1111-1111-1111-111111111111.blob").read_bytes(),
            original_world_bytes,
        )
        self.assertEqual((sp_path / "meta-info.json").read_text(encoding="utf-8"), original_meta)

    async def test_create_snapshot_at_cap_raises_and_creates_no_new_files(self) -> None:
        await self._add_server()
        self._write_savepoint(1, "ScenA", 0, 1)

        with patch.object(settings, "save_snapshot_max_per_server", 1):
            async with self.sessions() as session:
                server = await session.get(Server, 1)
                save_uuid = await self._first_save_uuid(session, server)
                await create_snapshot(session, server, save_uuid, "first")

            before = sorted(p.name for p in self._snapshots_dir().iterdir())

            async with self.sessions() as session:
                server = await session.get(Server, 1)
                with self.assertRaises(SnapshotCapExceededError):
                    await create_snapshot(session, server, save_uuid, "second")

            after = sorted(p.name for p in self._snapshots_dir().iterdir())

        self.assertEqual(before, after)
        self.assertEqual(len(before), 2)  # one .tar.gz + one .json

    async def test_restore_arm_true_pins_arm_false_leaves_selection_unchanged(self) -> None:
        await self._add_server()
        self._write_savepoint(1, "ScenA", 0, 1)

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            save_uuid = await self._first_save_uuid(session, server)
            snap = await create_snapshot(session, server, save_uuid, "snap")

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            await restore_snapshot(session, server, snap.snapshot_id, arm=False)
            await session.commit()

        async with self.sessions() as session:
            unchanged = await session.get(Server, 1)
            self.assertEqual(unchanged.save_mode, "latest")
            self.assertIsNone(unchanged.save_pinned_uuid)

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            result = await restore_snapshot(session, server, snap.snapshot_id, arm=True)
            await session.commit()
        self.assertEqual(result.restored_uuid, save_uuid)

        async with self.sessions() as session:
            armed = await session.get(Server, 1)
            self.assertEqual(armed.save_mode, "pinned")
            self.assertEqual(armed.save_pinned_uuid, save_uuid)

    async def test_validate_archive_rejects_absolute_traversal_and_symlink_members(self) -> None:
        def add_absolute(tf: tarfile.TarFile) -> None:
            data = b"x"
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        def add_traversal(tf: tarfile.TarFile) -> None:
            data = b"x"
            info = tarfile.TarInfo(name="../../evil.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        def add_symlink(tf: tarfile.TarFile) -> None:
            info = tarfile.TarInfo(name="link")
            info.type = tarfile.SYMTYPE
            info.linkname = "some/target"
            tf.addfile(info)

        for build in (add_absolute, add_traversal, add_symlink):
            with self.subTest(build=build.__name__):
                buf = self._bad_archive_bytes(build)
                with self.assertRaises(InvalidArchiveError):
                    validate_archive(buf)

    async def test_store_upload_restore_now_on_invalid_archive_has_no_side_effects(self) -> None:
        await self._add_server()

        def add_absolute(tf: tarfile.TarFile) -> None:
            data = b"x"
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        buf = self._bad_archive_bytes(add_absolute)

        async with self.sessions() as session:
            server = await session.get(Server, 1)
            with self.assertRaises(InvalidArchiveError):
                await store_upload(session, server, buf, "bad upload", restore_now=True, arm=True)
            await session.commit()

        snapshots_dir = self._snapshots_dir()
        if snapshots_dir.exists():
            self.assertEqual(list(snapshots_dir.iterdir()), [])

        async with self.sessions() as session:
            unchanged = await session.get(Server, 1)
            self.assertEqual(unchanged.save_mode, "latest")
            self.assertIsNone(unchanged.save_pinned_uuid)


if __name__ == "__main__":
    unittest.main()
