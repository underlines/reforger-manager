"""Tests for the idempotent-refresh free-space guard."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.mod import Mod
from app.models.server import Server, ServerMod
from app.mods import freespace


GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_C = "CCCCCCCCCCCCCCCC"


def _usage(free: int) -> SimpleNamespace:
    return SimpleNamespace(free=free, total=free, used=0)


class RefreshGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all(
                [
                    Mod(guid=GUID_A, name="A", is_local=True, size=1000),
                    Mod(guid=GUID_B, name="B", is_local=True, size=None),
                    Mod(guid=GUID_C, name="C", is_local=False, size=400),
                    Server(id=7, name="Seven"),
                    Server(id=8, name="Eight"),
                    Server(id=99, name="Empty"),
                    ServerMod(server_id=7, mod_guid=GUID_A, load_order=0, enabled=True),
                    ServerMod(server_id=7, mod_guid=GUID_C, load_order=1, enabled=True),
                    ServerMod(server_id=7, mod_guid=GUID_B, load_order=2, enabled=True),
                    ServerMod(server_id=8, mod_guid=GUID_B, load_order=0, enabled=True),
                ]
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_passes_when_free_space_exceeds_projection(self) -> None:
        with patch.object(freespace.shutil, "disk_usage", return_value=_usage(5000)):
            async with self.sessions() as session:
                await freespace.guard_refresh_scope(session, "all")

    async def test_refuses_when_free_space_below_projection(self) -> None:
        with patch.object(freespace.shutil, "disk_usage", return_value=_usage(100)):
            async with self.sessions() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await freespace.guard_refresh_scope(session, "all")
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_none_sizes_are_skipped_and_largest_addon_drives_projection(self) -> None:
        # Server 7: A=1000, C=400, B=None. If the guard summed the scope the
        # projection would be 1400 and free=1500 would pass; max*2 is 2000, so it
        # must refuse.
        with patch.object(freespace.shutil, "disk_usage", return_value=_usage(1500)):
            async with self.sessions() as session:
                with self.assertRaises(HTTPException) as ctx:
                    await freespace.guard_refresh_scope(session, 7)
        self.assertEqual(ctx.exception.status_code, 409)

        # Free above max*2 passes, proving the None size did not force a refusal.
        with patch.object(freespace.shutil, "disk_usage", return_value=_usage(2500)):
            async with self.sessions() as session:
                await freespace.guard_refresh_scope(session, 7)

    async def test_no_known_sizes_returns_without_raising(self) -> None:
        # Server 8 holds only a size-less mod; server 99 has no mods at all.
        for scope in (8, 99):
            with patch.object(freespace.shutil, "disk_usage", return_value=_usage(0)):
                async with self.sessions() as session:
                    await freespace.guard_refresh_scope(session, scope)


if __name__ == "__main__":
    unittest.main()