"""Deterministic SQLite tests for mod update intelligence."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.mod import Mod
from app.models.server import Server, ServerMod
from app.mods import updates


GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"


class _Workshop:
    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.calls: list[str] = []

    async def get_mod(self, guid: str) -> dict:
        self.calls.append(guid)
        return {"version": self.versions[guid], "name": f"Workshop {guid[-1]}"}


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
                        pinned_version="1.0",
                    ),
                    Server(id=7, name="Test"),
                    ServerMod(server_id=7, mod_guid=GUID_A, load_order=0, pinned_version="1.5"),
                    ServerMod(server_id=7, mod_guid=GUID_B, load_order=1, pinned_version="1.7"),
                ]
            )
            await session.commit()
        self.workshop = _Workshop({GUID_A: "2.0", GUID_B: "2.0"})
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

    async def test_global_scope_skips_any_pin_and_reports_update(self) -> None:
        # A server pin protects the shared cache during a global update.
        async with self.sessions() as session:
            server_mod = await session.scalar(
                select(ServerMod).where(ServerMod.mod_guid == GUID_A)
            )
            server_mod.pinned_version = None
            await session.commit()

        result = await updates.check_updates("all")

        self.assertEqual(result["scope"], "all")
        self.assertEqual([row["guid"] for row in result["available_updates"]], [GUID_A])
        self.assertEqual([row["guid"] for row in result["skipped_pins"]], [GUID_B])
        self.assertEqual(result["skipped_pins"][0]["pin"]["source"], "library")
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["errors"], [])

    async def test_apply_passes_only_unpinned_latest_targets_to_downloader(self) -> None:
        async with self.sessions() as session:
            server_mod = await session.scalar(
                select(ServerMod).where(ServerMod.mod_guid == GUID_A)
            )
            server_mod.pinned_version = None
            await session.commit()
        downloader = AsyncMock(return_value={"guids": [GUID_A], "progress": 100.0})

        with patch.object(updates, "run_mod_download", downloader):
            result = await updates.apply_updates("all")

        downloader.assert_awaited_once_with(None, [GUID_A], {GUID_A: "2.0"})
        self.assertEqual(result["applied"]["guids"], [GUID_A])

    async def test_server_scope_uses_server_pin_before_library_pin(self) -> None:
        result = await updates.check_updates("7")

        self.assertEqual(result["scope"], 7)
        self.assertEqual(result["available_updates"], [])
        pins = {row["guid"]: row["pin"] for row in result["skipped_pins"]}
        self.assertEqual(pins[GUID_A], {"source": "server", "version": "1.5", "server_id": 7})
        # B has both pins; the server-specific one is the effective pin.
        self.assertEqual(pins[GUID_B], {"source": "server", "version": "1.7", "server_id": 7})

    async def test_apply_no_update_does_not_call_downloader(self) -> None:
        self.workshop.versions[GUID_A] = "1.0"
        self.workshop.versions[GUID_B] = "1.0"
        downloader = AsyncMock()
        with patch.object(updates, "run_mod_download", downloader):
            result = await updates.apply_updates("all")

        downloader.assert_not_awaited()
        self.assertEqual(result["available_updates"], [])
        self.assertEqual(result["applied"]["guids"], [])

    def test_scope_validation(self) -> None:
        self.assertEqual(updates.normalize_scope(" ALL "), "all")
        self.assertEqual(updates.normalize_scope("7"), 7)
        with self.assertRaises(updates.UpdateScopeError):
            updates.normalize_scope(0)


if __name__ == "__main__":
    unittest.main()
