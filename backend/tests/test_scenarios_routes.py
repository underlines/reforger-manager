"""S6 scenario-picker route tests — DB-first resolve with API fallback.

Route-style tests (the ``test_storage_and_download`` pattern): a throwaway app
with ``get_session`` / ``get_current_user`` overridden so the scenario router is
exercised against a fresh in-memory sqlite schema. ``workshop.get_scenarios`` is
monkeypatched at the router's import site; the tests never touch the live API.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import scenarios as scenarios_api
from app.core.db import Base, get_session
from app.core.security import get_current_user
from app.models import Mod, ModScenario
from app.mods.workshop import ModNotFound, WorkshopError

GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
GUID_404 = "658756C5760E94DE"  # Sprint 1 server 9's known-404 mod


class ScenarioResolveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app = FastAPI()
        app.include_router(scenarios_api.router, prefix="/api")
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()

    # ---------------------------------------------------------------- helpers
    async def _seed_mod(self, guid: str, name: str | None) -> None:
        async with self.sessions() as session:
            session.add(Mod(guid=guid, name=name))
            await session.commit()

    async def _seed_scenario(
        self,
        guid: str,
        game_id: str,
        *,
        name: str | None = None,
        game_mode: str | None = None,
        player_count: int | None = None,
    ) -> None:
        async with self.sessions() as session:
            session.add(
                ModScenario(
                    mod_guid=guid,
                    game_id=game_id,
                    name=name,
                    game_mode=game_mode,
                    player_count=player_count,
                )
            )
            await session.commit()

    async def _resolve(self, guids: list[str]) -> dict:
        response = await self.client.post(
            "/api/scenarios/resolve", json={"guids": guids}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ----------------------------------------------------------------- tests
    async def test_all_cached_guids_resolve_from_db_without_any_api_call(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_scenario(
            GUID_A, "{A}Missions/one.conf", name="One", game_mode="Conflict", player_count=16
        )
        await self._seed_mod(GUID_B, "Mod B")
        await self._seed_scenario(GUID_B, "{B}Missions/two.conf", name="Two")

        with patch(
            "app.api.scenarios.get_scenarios",
            new=AsyncMock(side_effect=AssertionError("API must not be called for cached GUIDs")),
        ):
            body = await self._resolve([GUID_A, GUID_B])

        self.assertEqual(body["failed"], [])
        self.assertEqual(len(body["scenarios"]), 2)
        self.assertEqual({s["source"] for s in body["scenarios"]}, {"db"})
        self.assertEqual({s["mod_guid"] for s in body["scenarios"]}, {GUID_A, GUID_B})
        by_guid = {s["mod_guid"]: s for s in body["scenarios"]}
        self.assertEqual(by_guid[GUID_A]["mod_name"], "Mod A")
        self.assertEqual(by_guid[GUID_A]["game_id"], "{A}Missions/one.conf")
        self.assertEqual(by_guid[GUID_A]["player_count"], 16)
        self.assertEqual(by_guid[GUID_B]["mod_name"], "Mod B")

    async def test_uncached_guid_calls_api_once_then_next_request_is_db(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        fixture = [
            {"game_id": "{A}Missions/alpha.conf", "name": "Alpha", "game_mode": "Conflict", "player_count": 32},
            {"game_id": "{A}Missions/beta.conf", "name": "Beta"},
        ]

        with patch("app.api.scenarios.get_scenarios", new=AsyncMock(return_value=fixture)) as mocked:
            first = await self._resolve([GUID_A])

        mocked.assert_awaited_once_with(GUID_A)
        self.assertEqual(first["failed"], [])
        self.assertEqual(len(first["scenarios"]), 2)
        self.assertEqual({s["source"] for s in first["scenarios"]}, {"api"})

        # The API scenarios were persisted, so the same request is now DB-only.
        with patch(
            "app.api.scenarios.get_scenarios",
            new=AsyncMock(side_effect=AssertionError("API must not be called again")),
        ):
            second = await self._resolve([GUID_A])

        self.assertEqual(len(second["scenarios"]), 2)
        self.assertEqual({s["source"] for s in second["scenarios"]}, {"db"})
        self.assertEqual({s["game_id"] for s in second["scenarios"]}, {"{A}Missions/alpha.conf", "{A}Missions/beta.conf"})
        self.assertEqual({s["mod_name"] for s in second["scenarios"]}, {"Mod A"})

    async def test_failed_guid_is_reported_without_failing_the_request(self) -> None:
        await self._seed_mod(GUID_404, "Missing mod")
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_scenario(GUID_A, "{A}Missions/one.conf", name="One")

        def _side_effect(mod_id: str) -> None:
            if mod_id == GUID_404:
                raise ModNotFound(GUID_404)
            raise AssertionError(f"unexpected API call for {mod_id}")

        with patch("app.api.scenarios.get_scenarios", new=AsyncMock(side_effect=_side_effect)):
            body = await self._resolve([GUID_404, GUID_A])

        self.assertEqual([f["guid"] for f in body["failed"]], [GUID_404])
        self.assertIn("not found", body["failed"][0]["reason"].lower())
        self.assertEqual([s["mod_guid"] for s in body["scenarios"]], [GUID_A])
        self.assertEqual({s["source"] for s in body["scenarios"]}, {"db"})

    async def test_workshop_error_is_reported_as_failed(self) -> None:
        await self._seed_mod(GUID_A, "Mod A")
        await self._seed_scenario(GUID_A, "{A}Missions/one.conf", name="One")

        with patch(
            "app.api.scenarios.get_scenarios",
            new=AsyncMock(side_effect=WorkshopError("GET /mods/X/scenarios failed: timeout")),
        ):
            body = await self._resolve([GUID_A, GUID_B])

        self.assertEqual(len(body["scenarios"]), 1)
        self.assertEqual([f["guid"] for f in body["failed"]], [GUID_B])
        self.assertEqual(body["failed"][0]["reason"], "GET /mods/X/scenarios failed: timeout")

    async def test_unknown_guid_persists_with_bare_mod_row(self) -> None:
        fixture = [{"game_id": "{C}Missions/gamma.conf", "name": "Gamma"}]

        with patch("app.api.scenarios.get_scenarios", new=AsyncMock(return_value=fixture)):
            body = await self._resolve([GUID_B])

        self.assertEqual(len(body["scenarios"]), 1)
        self.assertEqual(body["scenarios"][0]["source"], "api")
        self.assertEqual(body["scenarios"][0]["mod_name"], None)

        # The GUID had no library row, but the cache is still persisted (the
        # bare Mod row satisfies the FK) so the next request is DB-only.
        async with self.sessions() as session:
            mod = await session.get(Mod, GUID_B)
            self.assertIsNotNone(mod)
            rows = (
                await session.execute(
                    select(ModScenario).where(ModScenario.mod_guid == GUID_B)
                )
            ).scalars().all()
            self.assertEqual([r.game_id for r in rows], ["{C}Missions/gamma.conf"])

        with patch(
            "app.api.scenarios.get_scenarios",
            new=AsyncMock(side_effect=AssertionError("API must not be called again")),
        ):
            second = await self._resolve([GUID_B])
        self.assertEqual({s["source"] for s in second["scenarios"]}, {"db"})

    async def test_empty_or_blank_guids_is_an_empty_result(self) -> None:
        with patch(
            "app.api.scenarios.get_scenarios",
            new=AsyncMock(side_effect=AssertionError("API must not be called")),
        ):
            body = await self._resolve([])
        self.assertEqual(body, {"scenarios": [], "failed": []})


if __name__ == "__main__":
    unittest.main()