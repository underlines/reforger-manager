"""Supervisor save-mode argv flags + selection consumption (Sprint S4).

No real Reforger binary is run: ``asyncio.create_subprocess_exec`` is patched
with a fake process that never exits during a test (mirrors the pattern in
``test_verify.py`` / ``test_schedule_restart.py``). Filesystem-backed pieces
(the binary existence check, ``saves.discover()``'s on-disk save-point scan)
use a real temp directory so ``supervisor.start()`` runs unmodified end to
end.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
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
from app.servers.supervisor import Supervisor


class FakeProcess:
    """Just enough of asyncio.subprocess.Process for a successful start().

    ``wait()`` never returns during a test (real ``console.log`` tailing and
    exit-watching tasks are allowed to run in the background, but must not
    race the test's own assertions/DB reads).
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None

    async def wait(self) -> int | None:
        await asyncio.sleep(3600)
        return self.returncode

    def send_signal(self, _sig: object) -> None:
        self.returncode = 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


class SupervisorSaveSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # ``ignore_cleanup_errors``: the fake process never "exits" during a
        # test, so the console.log write handle opened by start() is still
        # held open at teardown time (matches real behaviour — it's only
        # closed when the child process goes away) and Windows refuses to
        # unlink an open file.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)

        server_dir = root / "server"
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "ArmaReforgerServer").touch()

        self._settings_patches = [
            patch.object(settings, "server_dir", server_dir),
            patch.object(settings, "mods_dir", root / "mods"),
            patch.object(settings, "profiles_dir", root / "profiles"),
            patch.object(settings, "configs_dir", root / "configs"),
        ]
        for p in self._settings_patches:
            p.start()

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.sup = Supervisor(session_factory=self.sessions)

        self.last_argv: list[str] = []

        async def fake_spawn(*args, **kwargs):
            self.last_argv = list(args)
            return FakeProcess()

        self._spawn_patch = patch(
            "app.servers.supervisor.asyncio.create_subprocess_exec",
            side_effect=fake_spawn,
        )
        self._spawn_patch.start()

    async def asyncTearDown(self) -> None:
        self._spawn_patch.stop()
        active = self.sup._active
        if active is not None:
            for task in (active.tail_task, active.wait_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.sleep(0)
        for p in self._settings_patches:
            p.stop()
        await self.engine.dispose()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ helpers
    async def _create_server(self, **kwargs) -> int:
        async with self.sessions() as session:
            server = Server(name="Test", **kwargs)
            session.add(server)
            await session.commit()
            return server.id

    async def _reload(self, server_id: int) -> Server:
        async with self.sessions() as session:
            return await session.get(Server, server_id)

    def _write_savepoint(self, server_id: int, uuid: str) -> None:
        """Creates a minimal on-disk save point ``saves.discover()`` will find."""
        savepoint_dir = (
            Path(settings.profiles_dir)
            / str(server_id)
            / "profile"
            / ".save"
            / "game"
            / "SomeScenario"
            / "playthrough001"
            / "savepoint001"
        )
        savepoint_dir.mkdir(parents=True, exist_ok=True)
        (savepoint_dir / "meta-info.json").write_text(
            json.dumps({"m_Id": uuid}), encoding="utf-8"
        )

    # -------------------------------------------------------------------- tests
    async def test_latest_mode_appends_no_save_flags(self) -> None:
        server_id = await self._create_server(save_mode="latest")
        await self.sup.start(server_id)

        self.assertNotIn("-loadSessionSave", self.last_argv)
        self.assertNotIn("-backendFreshSession", self.last_argv)

    async def test_pinned_valid_uuid_appends_flag_and_resets_non_sticky(self) -> None:
        server_id = await self._create_server(
            save_mode="pinned", save_pinned_uuid="save-uuid-1", save_selection_sticky=False
        )
        self._write_savepoint(server_id, "save-uuid-1")

        await self.sup.start(server_id)

        idx = self.last_argv.index("-loadSessionSave")
        self.assertEqual(self.last_argv[idx + 1], "save-uuid-1")

        server = await self._reload(server_id)
        self.assertEqual(server.save_mode, "latest")
        self.assertIsNone(server.save_pinned_uuid)

    async def test_pinned_sticky_selection_survives_a_successful_start(self) -> None:
        server_id = await self._create_server(
            save_mode="pinned", save_pinned_uuid="save-uuid-2", save_selection_sticky=True
        )
        self._write_savepoint(server_id, "save-uuid-2")

        await self.sup.start(server_id)

        idx = self.last_argv.index("-loadSessionSave")
        self.assertEqual(self.last_argv[idx + 1], "save-uuid-2")

        server = await self._reload(server_id)
        self.assertEqual(server.save_mode, "pinned")
        self.assertEqual(server.save_pinned_uuid, "save-uuid-2")

    async def test_fresh_mode_appends_flag_and_resets_non_sticky(self) -> None:
        server_id = await self._create_server(save_mode="fresh", save_selection_sticky=False)

        await self.sup.start(server_id)

        self.assertIn("-backendFreshSession", self.last_argv)

        server = await self._reload(server_id)
        self.assertEqual(server.save_mode, "latest")
        self.assertIsNone(server.save_pinned_uuid)

    async def test_pinned_missing_uuid_falls_back_and_clears_selection(self) -> None:
        server_id = await self._create_server(
            save_mode="pinned", save_pinned_uuid="ghost-uuid", save_selection_sticky=False
        )
        # No save point written on disk for "ghost-uuid" at all.

        await self.sup.start(server_id)

        self.assertNotIn("-loadSessionSave", self.last_argv)
        self.assertNotIn("-backendFreshSession", self.last_argv)

        server = await self._reload(server_id)
        self.assertEqual(server.save_mode, "latest")
        self.assertIsNone(server.save_pinned_uuid)


if __name__ == "__main__":
    unittest.main()
