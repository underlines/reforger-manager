"""S10 -- persistent per-server admin list.

``Server.game_admins`` (identity UUID strings) round-trips through the
POST/PATCH/GET server routes and is emitted as ``config.json`` ``game.admins``.

Deterministic SQLite tests exercising the route coroutines directly (the
``test_config_preview_and_pins`` / ``test_clone`` style), no network and no app
lifespan.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import servers
from app.core.db import Base
from app.models.server import Server
from app.schemas.server import ServerCreate, ServerUpdate
from app.servers import config_gen

UUID_A = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
UUID_B = "7d444840-9dc0-11d1-b245-5ffdce74fad2"


class BuildConfigAdminsTests(unittest.TestCase):
    def test_admins_emitted_from_game_admins(self) -> None:
        server = Server(name="srv", game_admins=[UUID_A, UUID_B])
        config = config_gen.build_config(server, [])
        self.assertEqual(config["game"]["admins"], [UUID_A, UUID_B])

    def test_admins_default_to_empty_list_when_unset(self) -> None:
        server = Server(name="srv")
        config = config_gen.build_config(server, [])
        self.assertEqual(config["game"]["admins"], [])
        self.assertIsInstance(config["game"]["admins"], list)


class GameAdminsRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _create(self, **kwargs) -> int:
        async with self.sessions() as session:
            out = await servers.create_server(
                body=ServerCreate(name="srv", **kwargs), session=session
            )
            return out.id

    async def _patch(self, server_id: int, body: ServerUpdate):
        async with self.sessions() as session:
            return await servers.update_server(
                server_id=server_id, body=body, session=session
            )

    async def _get(self, server_id: int):
        async with self.sessions() as session:
            return await servers.get_server(server_id=server_id, session=session)

    async def test_create_with_admins_then_get(self) -> None:
        sid = await self._create(game_admins=[UUID_A])
        self.assertEqual((await self._get(sid)).game_admins, [UUID_A])

    async def test_patch_only_game_admins_grows_the_list(self) -> None:
        sid = await self._create(game_admins=[UUID_A])
        await self._patch(sid, ServerUpdate(game_admins=[UUID_A, UUID_B]))
        self.assertEqual((await self._get(sid)).game_admins, [UUID_A, UUID_B])

    async def test_patch_to_empty_list_clears_admins(self) -> None:
        sid = await self._create(game_admins=[UUID_A, UUID_B])
        await self._patch(sid, ServerUpdate(game_admins=[]))
        self.assertEqual((await self._get(sid)).game_admins, [])

    async def test_patch_without_game_admins_leaves_it_untouched(self) -> None:
        sid = await self._create(game_admins=[UUID_A])
        await self._patch(sid, ServerUpdate(name="renamed"))
        got = await self._get(sid)
        self.assertEqual(got.name, "renamed")
        self.assertEqual(got.game_admins, [UUID_A])

    async def test_unset_admins_reach_config_as_empty_list(self) -> None:
        sid = await self._create()
        async with self.sessions() as session:
            server = await servers._load(session, sid)
            config = config_gen.build_config(server, [])
        self.assertEqual(config["game"]["admins"], [])


if __name__ == "__main__":
    unittest.main()
