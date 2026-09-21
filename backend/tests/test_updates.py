"""Deterministic SQLite tests for mod update application."""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.mod import Mod
from app.models.server import Server, ServerMod

# ``app/mods/__init__.py`` still imports check_updates/MOD_UPDATE_CHECK_JOB_KIND
# et al. from ``updates`` -- names this story deletes. A separate, later story
# (S4) rewires that package init; until then, importing ``app.mods`` normally
# would fail here for a reason unrelated to this module. Stub the package in
# ``sys.modules`` (with the real ``__path__`` so submodule/relative imports
# still resolve) to load ``app.mods.updates`` directly without executing the
# currently-broken package ``__init__.py``.
if "app.mods" not in sys.modules:
    _stub = types.ModuleType("app.mods")
    _stub.__path__ = [str(Path(__file__).parents[1] / "app" / "mods")]
    sys.modules["app.mods"] = _stub

from app.mods import updates as updates_module
from app.mods.updates import refresh_mods


GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all(
                [
                    Mod(guid=GUID_A, name="A", is_local=True, installed_version="1.0"),
                    Mod(
                        guid=GUID_B,
                        name="B",
                        is_local=True,
                        installed_version="1.0",
                        pinned_version="1.5",
                    ),
                    Server(id=7, name="Test"),
                    ServerMod(server_id=7, mod_guid=GUID_A, load_order=0, pinned_version="2.0"),
                    # GUID_B carries both a server pin and a library pin; the
                    # server pin must win for this scope.
                    ServerMod(server_id=7, mod_guid=GUID_B, load_order=1, pinned_version="1.9"),
                ]
            )
            await session.commit()
        self.patchers = [patch.object(updates_module, "SessionLocal", self.sessions)]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        await self.engine.dispose()

    async def test_all_scope_passes_every_local_guid_and_only_pinned_versions(self) -> None:
        downloader = AsyncMock(return_value={"guids": [GUID_A, GUID_B], "progress": 100.0})
        refresher = AsyncMock(return_value=[GUID_A, GUID_B])
        with patch.object(updates_module, "run_mod_download", downloader), patch.object(
            updates_module, "refresh_local_mods", refresher
        ):
            result = await refresh_mods("all")

        downloader.assert_awaited_once_with(None, [GUID_A, GUID_B], {GUID_B: "1.5"})
        self.assertEqual(result["scope"], "all")
        self.assertEqual(result["requested"], [GUID_A, GUID_B])
        self.assertEqual(result["pinned"], {GUID_B: "1.5"})
        self.assertEqual(result["unavailable"], [])

    async def test_server_scope_server_pin_beats_library_pin(self) -> None:
        downloader = AsyncMock(return_value={"guids": [GUID_A, GUID_B], "progress": 100.0})
        refresher = AsyncMock(return_value=[GUID_A, GUID_B])
        with patch.object(updates_module, "run_mod_download", downloader), patch.object(
            updates_module, "refresh_local_mods", refresher
        ):
            result = await refresh_mods("7")

        # GUID_A: server pin "2.0" only. GUID_B: both a server pin ("1.9") and a
        # library pin ("1.5") exist -- the server pin must win.
        downloader.assert_awaited_once_with(None, [GUID_A, GUID_B], {GUID_A: "2.0", GUID_B: "1.9"})
        self.assertEqual(result["scope"], 7)

    async def test_refresh_local_mods_called_with_same_guid_list_as_downloader(self) -> None:
        downloader = AsyncMock(return_value={"guids": [GUID_A, GUID_B], "progress": 100.0})
        refresher = AsyncMock(return_value=[GUID_A, GUID_B])
        with patch.object(updates_module, "run_mod_download", downloader), patch.object(
            updates_module, "refresh_local_mods", refresher
        ):
            await refresh_mods("all")

        downloaded_guids = downloader.await_args.args[1]
        refresher.assert_awaited_once_with(downloaded_guids)

    def test_no_workshop_import(self) -> None:
        self.assertFalse(hasattr(updates_module, "workshop"))

    async def test_empty_scope_does_not_call_downloader(self) -> None:
        # Delete the two library mods so "all" resolves to zero targets.
        async with self.sessions() as session:
            for guid in (GUID_A, GUID_B):
                mod = await session.get(Mod, guid)
                await session.delete(mod)
            await session.commit()

        downloader = AsyncMock()
        refresher = AsyncMock()
        with patch.object(updates_module, "run_mod_download", downloader), patch.object(
            updates_module, "refresh_local_mods", refresher
        ):
            result = await refresh_mods("all")

        downloader.assert_not_awaited()
        refresher.assert_not_awaited()
        self.assertEqual(result["scope"], "all")
        self.assertEqual(result["requested"], [])
        self.assertEqual(result["pinned"], {})
        self.assertEqual(result["downloaded"], {"guids": [], "progress": 100.0})
        self.assertEqual(result["unavailable"], [])

    def test_scope_validation(self) -> None:
        self.assertEqual(updates_module.normalize_scope(" ALL "), "all")
        self.assertEqual(updates_module.normalize_scope("7"), 7)
        with self.assertRaises(updates_module.UpdateScopeError):
            updates_module.normalize_scope(0)


if __name__ == "__main__":
    unittest.main()
