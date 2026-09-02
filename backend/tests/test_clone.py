"""S7 -- clone a definition: deep-copy the row + its mod set (including pins),
excluding the config snapshot, the revision counter and all runtime state.

Deterministic SQLite tests exercising the route coroutine directly (the
``test_config_preview_and_pins`` style), no network and no app lifespan.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import servers
from app.core.db import Base
from app.models.server import Server, ServerMod
from app.schemas.server import ServerCloneIn, ServerModIn, ServerUpdate

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
            server = Server(
                id=1,
                name="srv",
                is_favourite=True,
                scenario_game_id="{ECC61978EDCC2B5A}Missions/23_Campaign.conf",
                game_name="srv browser title",
                game_password="game-pass",
                admin_password="admin-pass",
                max_players=24,
                visible=False,
                game_properties={"serverMaxViewDistance": 4000},
                extra_config={"recipientProfile": {"name": "ops"}},
                bind_address="127.0.0.1",
                bind_port=2101,
                public_address="10.0.0.5",
                public_port=2102,
                a2s_address="127.0.0.1",
                a2s_port=18777,
                rcon_enabled=True,
                rcon_address="127.0.0.1",
                rcon_port=19999,
                rcon_password="rcon-pass",
                rcon_permission="monitor",
                rcon_max_clients=4,
                config={"game": {"name": "snapshot"}},
                config_revision=7,
                # Runtime state that must NOT survive the copy.
                is_running=True,
                pid=4242,
                last_state="running",
                last_exit_code=0,
                last_diagnosis={"verdict": "green"},
                last_started_at=PINNED_AT,
                last_stopped_at=None,
            )
            server.mods.append(
                ServerMod(
                    mod_guid=GUID_A,
                    mod_name="A",
                    load_order=3,
                    enabled=False,
                    pinned_version="1.2.3",
                    pinned_at=PINNED_AT,
                    pinned_at_build="24501482",
                    pinned_reason="known good",
                )
            )
            server.mods.append(ServerMod(mod_guid=GUID_B, mod_name="B", load_order=9, enabled=True))
            session.add(server)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _clone(self, name: str = "srv (copy)") -> Server:
        async with self.sessions() as session:
            out = await servers.clone_server(
                server_id=1, body=ServerCloneIn(name=name), session=session
            )
            return await servers._load(session, out.id)


class CloneTests(_ServerFixture):
    async def test_clone_copies_config_fields_and_drops_runtime_state(self) -> None:
        clone = await self._clone()
        self.assertNotEqual(clone.id, 1)
        self.assertEqual(clone.name, "srv (copy)")
        # Config / network / RCON / game fields, copied verbatim.
        self.assertTrue(clone.is_favourite)
        self.assertEqual(clone.scenario_game_id, "{ECC61978EDCC2B5A}Missions/23_Campaign.conf")
        self.assertEqual(clone.game_name, "srv browser title")
        self.assertEqual(clone.game_password, "game-pass")
        self.assertEqual(clone.admin_password, "admin-pass")
        self.assertEqual(clone.max_players, 24)
        self.assertFalse(clone.visible)
        self.assertEqual(clone.game_properties, {"serverMaxViewDistance": 4000})
        self.assertEqual(clone.extra_config, {"recipientProfile": {"name": "ops"}})
        self.assertEqual(clone.bind_address, "127.0.0.1")
        self.assertEqual(clone.bind_port, 2101)
        self.assertEqual(clone.public_address, "10.0.0.5")
        self.assertEqual(clone.public_port, 2102)
        self.assertEqual(clone.a2s_address, "127.0.0.1")
        self.assertEqual(clone.a2s_port, 18777)
        self.assertTrue(clone.rcon_enabled)
        self.assertEqual(clone.rcon_address, "127.0.0.1")
        self.assertEqual(clone.rcon_port, 19999)
        self.assertEqual(clone.rcon_password, "rcon-pass")
        self.assertEqual(clone.rcon_permission, "monitor")
        self.assertEqual(clone.rcon_max_clients, 4)
        # No snapshot, no revision counter, no runtime state of the source.
        self.assertIsNone(clone.config)
        self.assertEqual(clone.config_revision, 0)
        self.assertFalse(clone.is_running)
        self.assertIsNone(clone.pid)
        self.assertIsNone(clone.last_state)
        self.assertIsNone(clone.last_exit_code)
        self.assertIsNone(clone.last_diagnosis)
        self.assertIsNone(clone.last_started_at)
        self.assertIsNone(clone.last_stopped_at)

    async def test_clone_recreates_every_mod_row_with_pins_and_order(self) -> None:
        clone = await self._clone()
        mods = {m.mod_guid: m for m in clone.mods}
        self.assertEqual(sorted(mods), sorted([GUID_A, GUID_B]))
        a, b = mods[GUID_A], mods[GUID_B]
        self.assertEqual([a.load_order, b.load_order], [3, 9])
        self.assertEqual([a.enabled, b.enabled], [False, True])
        self.assertEqual(a.pinned_version, "1.2.3")
        self.assertEqual(a.pinned_at_build, "24501482")
        self.assertEqual(a.pinned_reason, "known good")
        self.assertEqual(_naive(a.pinned_at), _naive(PINNED_AT))
        self.assertIsNone(b.pinned_version)
        self.assertIsNone(b.pinned_at)
        # Fresh rows: the clone shares no ServerMod identity with the source.
        async with self.sessions() as session:
            source = await servers._load(session, 1)
        self.assertNotIn(a.id, {m.id for m in source.mods})
        self.assertNotIn(b.id, {m.id for m in source.mods})

    async def test_editing_the_clone_does_not_change_the_source(self) -> None:
        clone = await self._clone()
        async with self.sessions() as session:
            await servers.update_server(
                server_id=clone.id,
                body=ServerUpdate(
                    name="edited copy",
                    max_players=64,
                    mods=[
                        ServerModIn(mod_guid=GUID_C, mod_name="C", load_order=0),
                        ServerModIn(
                            mod_guid=GUID_A,
                            mod_name="A",
                            load_order=1,
                            pinned_version="9.9.9",
                            pinned_reason="clone-only repin",
                        ),
                    ],
                ),
                session=session,
            )
        async with self.sessions() as session:
            source = await servers._load(session, 1)
        self.assertEqual(source.name, "srv")
        self.assertEqual(source.max_players, 24)
        self.assertEqual(source.config_revision, 7)
        mods = {m.mod_guid: m for m in source.mods}
        self.assertEqual(sorted(mods), sorted([GUID_A, GUID_B]))
        a = mods[GUID_A]
        self.assertEqual(a.load_order, 3)
        self.assertEqual(a.pinned_version, "1.2.3")
        self.assertEqual(a.pinned_reason, "known good")
        self.assertEqual(_naive(a.pinned_at), _naive(PINNED_AT))

    async def test_clone_of_a_missing_server_is_404(self) -> None:
        async with self.sessions() as session:
            with self.assertRaises(HTTPException) as ctx:
                await servers.clone_server(
                    server_id=999, body=ServerCloneIn(name="ghost"), session=session
                )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
