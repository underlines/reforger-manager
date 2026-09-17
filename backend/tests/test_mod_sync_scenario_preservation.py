"""``_upsert_local`` scenario handling -- a local rescan (full ``mod_sync`` pass
1, or the targeted ``refresh_local_mods`` after a force-download) must never
clobber a ``mod_scenarios`` row that was already enriched from the Workshop
API. The offline scan's ``game_id`` is only a guess (``"{mod_guid}" + path``)
and is frequently wrong -- see ``scanner.parse_scenarios`` -- so a blind
delete-and-reinsert threw away correct, API-sourced scenario identity on every
rescan. Regression test for the WCS_Everon incident (root-caused 2026-09-17).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.core.db import Base
import app.core.db as db_mod
from app.models import Mod, ModScenario
from app.mods import sync as sync_mod
from app.mods.scanner import ScanResult, ScannedMod

GUID = "6148289172F86E7A"  # WCS_Everon, the mod that hit this bug live


def _scanned(guid=GUID, scenarios=None, version="1.0.0", size=12345):
    return ScannedMod(
        guid=guid, name="WCS Everon", summary="Conflict scenarios pack",
        tags=[], version=version, size=size, is_unlisted=False,
        is_deleted_local=False, dep_guids=[], scenarios=scenarios or [],
        has_thumbnail=False, dir_name=f"WCS_Everon_{guid}",
    )


@pytest_asyncio.fixture
async def sessionmaker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    monkeypatch.setattr(sync_mod, "SessionLocal", factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_enriched_scenario_survives_a_rescan(sessionmaker, monkeypatch):
    real_game_id = "{02778C5E44407C03}Missions/WCS_Everon_Conflict_NvS_Layout1.conf"
    path = "Missions/WCS_Everon_Conflict_NvS_Layout1.conf"
    wrong_game_id = "{" + GUID + "}" + path  # the offline scanner's guessed id

    async with sessionmaker() as s:
        s.add(Mod(guid=GUID, is_local=True, name="WCS Everon"))
        s.add(
            ModScenario(
                mod_guid=GUID,
                game_id=real_game_id,
                name="[WCS] New Everon - North/South #1",
                game_mode="CONFLICT",
                player_count=64,
            )
        )
        await s.commit()

    # A local rescan re-guesses the same scenario under the mod's own guid.
    monkeypatch.setattr(
        sync_mod, "scan_all",
        lambda: ScanResult(mods=[_scanned(scenarios=[(wrong_game_id, path)])]),
    )
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == [GUID]

    async with sessionmaker() as s:
        rows = (
            await s.execute(select(ModScenario).where(ModScenario.mod_guid == GUID))
        ).scalars().all()
        by_game_id = {r.game_id: r for r in rows}

        # The enriched row is untouched.
        assert real_game_id in by_game_id
        enriched = by_game_id[real_game_id]
        assert enriched.name == "[WCS] New Everon - North/South #1"
        assert enriched.game_mode == "CONFLICT"

        # No duplicate row was inserted under the wrong, mod-guid-prefixed id.
        assert wrong_game_id not in by_game_id
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_offline_only_placeholder_still_refreshed_normally(sessionmaker, monkeypatch):
    stale_game_id = "{" + GUID + "}Missions/Old_Removed.conf"
    fresh_game_id = "{" + GUID + "}Missions/New_Scenario.conf"

    async with sessionmaker() as s:
        s.add(Mod(guid=GUID, is_local=True, name="WCS Everon"))
        # An old offline placeholder -- no name/game_mode -- for a path that
        # no longer exists on disk.
        s.add(ModScenario(mod_guid=GUID, game_id=stale_game_id))
        await s.commit()

    monkeypatch.setattr(
        sync_mod, "scan_all",
        lambda: ScanResult(
            mods=[_scanned(scenarios=[(fresh_game_id, "Missions/New_Scenario.conf")])]
        ),
    )
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == [GUID]

    async with sessionmaker() as s:
        rows = (
            await s.execute(select(ModScenario).where(ModScenario.mod_guid == GUID))
        ).scalars().all()
        by_game_id = {r.game_id: r for r in rows}
        # The stale placeholder for the removed scenario is gone.
        assert stale_game_id not in by_game_id
        # The newly-scanned scenario's placeholder was inserted.
        assert fresh_game_id in by_game_id
        assert by_game_id[fresh_game_id].name is None
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_fresh_mod_with_no_existing_scenarios_gets_placeholders(sessionmaker, monkeypatch):
    game_id = "{" + GUID + "}Missions/Some_Scenario.conf"
    monkeypatch.setattr(
        sync_mod, "scan_all",
        lambda: ScanResult(
            mods=[_scanned(scenarios=[(game_id, "Missions/Some_Scenario.conf")])]
        ),
    )
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == [GUID]

    async with sessionmaker() as s:
        rows = (
            await s.execute(select(ModScenario).where(ModScenario.mod_guid == GUID))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].game_id == game_id
        assert rows[0].name is None
        assert rows[0].game_mode is None
