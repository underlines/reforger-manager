"""S2 + S3 — pin preservation across a mod-set replace, and the draft config
preview endpoint.

Deterministic SQLite tests exercising the route coroutines directly (the
``test_updates`` / ``test_pinning`` style), no network and no app lifespan.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import servers
from app.core.config import settings
from app.core.db import Base
from app.models.server import Server, ServerMod
from app.schemas.server import ServerModIn, ServerUpdate

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_C = "CCCCCCCCCCCCCCCC"
PINNED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


class _ServerFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            server = Server(id=1, name="srv")
            server.mods.append(
                ServerMod(
                    mod_guid=GUID_A,
                    mod_name="A",
                    load_order=0,
                    enabled=True,
                    pinned_version="1.2.3",
                    pinned_at=PINNED_AT,
                    pinned_at_build="24501482",
                    pinned_reason="known good",
                )
            )
            server.mods.append(
                ServerMod(mod_guid=GUID_B, mod_name="B", load_order=1, enabled=True)
            )
            session.add(server)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _mods_by_guid(self, session) -> dict[str, ServerMod]:
        server = await servers._load(session, 1)
        return {m.mod_guid: m for m in server.mods}

    async def _patch(self, body: ServerUpdate) -> None:
        async with self.sessions() as session:
            await servers.update_server(server_id=1, body=body, session=session)


class PinPreservationTests(_ServerFixture):
    async def test_omitted_pin_fields_are_preserved(self) -> None:
        await self._patch(
            ServerUpdate(
                mods=[
                    ServerModIn(mod_guid=GUID_A, mod_name="A", load_order=0),
                    ServerModIn(mod_guid=GUID_B, mod_name="B", load_order=1),
                ]
            )
        )
        async with self.sessions() as session:
            a = (await self._mods_by_guid(session))[GUID_A]
        self.assertEqual(a.pinned_version, "1.2.3")
        self.assertEqual(a.pinned_at_build, "24501482")
        self.assertEqual(a.pinned_reason, "known good")
        self.assertEqual(_naive(a.pinned_at), _naive(PINNED_AT))

    async def test_explicit_pinned_version_overwrites_the_pin(self) -> None:
        await self._patch(
            ServerUpdate(
                mods=[
                    ServerModIn(
                        mod_guid=GUID_A,
                        mod_name="A",
                        load_order=0,
                        pinned_version="9.9.9",
                        pinned_reason="deliberate",
                    ),
                    ServerModIn(mod_guid=GUID_B, mod_name="B", load_order=1),
                ]
            )
        )
        async with self.sessions() as session:
            a = (await self._mods_by_guid(session))[GUID_A]
        self.assertEqual(a.pinned_version, "9.9.9")
        self.assertEqual(a.pinned_reason, "deliberate")
        # Not echoed by the caller -> not resurrected from the old row.
        self.assertIsNone(a.pinned_at_build)
        self.assertIsNone(a.pinned_at)

    async def test_dropping_a_guid_removes_its_pin(self) -> None:
        await self._patch(
            ServerUpdate(mods=[ServerModIn(mod_guid=GUID_B, mod_name="B", load_order=0)])
        )
        async with self.sessions() as session:
            self.assertNotIn(GUID_A, await self._mods_by_guid(session))
        # Re-adding the GUID with no pin fields must not bring the pin back.
        await self._patch(
            ServerUpdate(
                mods=[
                    ServerModIn(mod_guid=GUID_B, mod_name="B", load_order=0),
                    ServerModIn(mod_guid=GUID_A, mod_name="A", load_order=1),
                ]
            )
        )
        async with self.sessions() as session:
            a = (await self._mods_by_guid(session))[GUID_A]
        self.assertIsNone(a.pinned_version)
        self.assertIsNone(a.pinned_at)
        self.assertIsNone(a.pinned_at_build)
        self.assertIsNone(a.pinned_reason)

    async def test_pure_reorder_keeps_every_pin_and_timestamp(self) -> None:
        await self._patch(
            ServerUpdate(
                mods=[
                    ServerModIn(mod_guid=GUID_B, mod_name="B", load_order=0),
                    ServerModIn(mod_guid=GUID_A, mod_name="A", load_order=1),
                ]
            )
        )
        async with self.sessions() as session:
            mods = await self._mods_by_guid(session)
        a = mods[GUID_A]
        self.assertEqual(a.load_order, 1)
        self.assertEqual(a.pinned_version, "1.2.3")
        self.assertEqual(a.pinned_at_build, "24501482")
        self.assertEqual(a.pinned_reason, "known good")
        self.assertEqual(_naive(a.pinned_at), _naive(PINNED_AT))


class ConfigPreviewTests(_ServerFixture):
    async def test_preview_reflects_the_body(self) -> None:
        async with self.sessions() as session:
            out = await servers.preview_generated_config(
                server_id=1,
                body=ServerUpdate(
                    name="draft-name",
                    max_players=99,
                    game_properties={"serverMaxViewDistance": 4000},
                ),
                session=session,
            )
        game = out.config["game"]
        self.assertEqual(game["name"], "draft-name")
        self.assertEqual(game["maxPlayers"], 99)
        self.assertEqual(game["gameProperties"]["serverMaxViewDistance"], 4000)
        # merge, not replace — untouched defaults survive
        self.assertEqual(game["gameProperties"]["networkViewDistance"], 1000)

    async def test_preview_honours_the_mods_working_copy(self) -> None:
        async with self.sessions() as session:
            out = await servers.preview_generated_config(
                server_id=1,
                body=ServerUpdate(
                    mods=[ServerModIn(mod_guid=GUID_C, mod_name="C", load_order=0)]
                ),
                session=session,
            )
        self.assertEqual([m["modId"] for m in out.config["game"]["mods"]], [GUID_C])

    async def test_preview_does_not_touch_the_db_row(self) -> None:
        async with self.sessions() as session:
            await servers.preview_generated_config(
                server_id=1,
                body=ServerUpdate(
                    name="draft-name",
                    max_players=99,
                    game_properties={"serverMaxViewDistance": 4000},
                    mods=[ServerModIn(mod_guid=GUID_C, mod_name="C", load_order=0)],
                ),
                session=session,
            )
        async with self.sessions() as session:
            server = await servers._load(session, 1)
            self.assertEqual(server.name, "srv")
            self.assertEqual(server.max_players, 32)
            self.assertIsNone(server.game_properties)
            self.assertEqual(
                sorted(m.mod_guid for m in server.mods), [GUID_A, GUID_B]
            )
            a = {m.mod_guid: m for m in server.mods}[GUID_A]
            self.assertEqual(a.pinned_version, "1.2.3")

    async def test_preview_does_not_write_the_config_file(self) -> None:
        path = settings.config_path(1)
        existed = path.exists()
        before = path.read_bytes() if existed else None
        async with self.sessions() as session:
            await servers.preview_generated_config(
                server_id=1, body=ServerUpdate(name="draft"), session=session
            )
        self.assertEqual(path.exists(), existed)
        if existed:
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
