"""Focused SQLite checks for the mod-pinning library."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models.engine import Engine
from app.models.mod import Mod
from app.models.server import Server, ServerMod
from app.mods.pinning import (
    CurrentEngineBuildMissing,
    PinRecordNotFound,
    is_stale,
    pin_mod,
    pin_server_mod,
    unpin_mod,
    unpin_server_mod,
)


class PinningTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_and_server_pins(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            session.add_all(
                [
                    Engine(id=1, installed_build="24501482"),
                    Mod(guid="ABCDEF0123456789", name="Library mod"),
                    Server(id=7, name="Test server"),
                    ServerMod(server_id=7, mod_guid="ABCDEF0123456789"),
                ]
            )
            await session.commit()

            library_pin = await pin_mod(session, "abcdef0123456789", "1.2.3", "known good")
            self.assertEqual(library_pin.guid, "ABCDEF0123456789")
            self.assertEqual(library_pin.pinned_version, "1.2.3")
            self.assertEqual(library_pin.pinned_at_build, "24501482")
            self.assertEqual(library_pin.pinned_reason, "known good")
            self.assertIsNotNone(library_pin.pinned_at)
            self.assertIsNotNone(library_pin.pinned_at.tzinfo)
            self.assertFalse(is_stale(library_pin, await session.get(Engine, 1)))
            self.assertTrue(is_stale(pinned_at_build="24501482", installed_build="24501483"))
            self.assertFalse(is_stale(pinned_at_build=None, installed_build="24501483"))
            self.assertFalse(is_stale(pinned_at_build="24501482", installed_build=None))

            await unpin_mod(session, "abcdef0123456789")
            self.assertIsNone(library_pin.pinned_version)
            self.assertIsNone(library_pin.pinned_at_build)
            self.assertIsNone(library_pin.pinned_reason)
            self.assertIsNone(library_pin.pinned_at)

            server_pin = await pin_server_mod(session, 7, "abcdef0123456789", "2.0.0", "server only")
            self.assertEqual(server_pin.pinned_version, "2.0.0")
            self.assertEqual(server_pin.pinned_at_build, "24501482")
            self.assertEqual(server_pin.pinned_reason, "server only")
            self.assertIsNotNone(server_pin.pinned_at)
            await unpin_server_mod(session, 7, "abcdef0123456789")
            self.assertIsNone(server_pin.pinned_version)
            self.assertIsNone(server_pin.pinned_at_build)
            self.assertIsNone(server_pin.pinned_reason)
            self.assertIsNone(server_pin.pinned_at)

            with self.assertRaisesRegex(PinRecordNotFound, "0000000000000000"):
                await pin_mod(session, "0000000000000000", "1.0.0")

            with self.assertRaisesRegex(PinRecordNotFound, "server 7"):
                await pin_server_mod(session, 7, "0000000000000000", "1.0.0")

            (await session.get(Engine, 1)).installed_build = None
            with self.assertRaisesRegex(CurrentEngineBuildMissing, "installed engine build is missing"):
                await pin_mod(session, "ABCDEF0123456789", "1.0.0")

        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
