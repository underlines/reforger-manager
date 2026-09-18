"""``_log_scenario_discrepancies`` — the mod_sync repair-warning pass.

After Workshop enrichment, a ``ModScenario`` row with a ``name`` is
Workshop-verified; the offline scanner's guessed ``game_id`` (``"{mod_guid}"``
+ path) is frequently wrong (the WCS_Everon incident). This pass compares each
server's configured ``scenario_game_id`` against the verified library and logs
a warning when exactly one verified row shares its path portion. It is
strictly read-only: ``Server.scenario_game_id`` must never be rewritten here.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.core.db import Base
import app.core.db as db_mod
from app.models import Mod, ModScenario, Server
from app.mods import sync as sync_mod

MOD_GUID = "6148289172F86E7A"       # the mod the offline scanner guesses from
ALT_GUID = "02778C5E44407C03"       # the real Workshop scenario guid
MIRROR_GUID = "AAAABBBBCCCCDDDD"    # a second upload of the same mission
PATH = "Missions/WCS_Everon_Conflict_NvS_Layout1.conf"
WRONG_ID = "{" + MOD_GUID + "}" + PATH   # the scanner-guessed, stale id
RIGHT_ID = "{" + ALT_GUID + "}" + PATH   # the Workshop-verified id

SCENARIO_NAME = "[WCS] New Everon - North/South #1"


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


def _warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "reforger.mods.sync" and r.levelno == logging.WARNING
    ]


async def _configured_scenario_id(sessionmaker) -> str | None:
    """The server's ``scenario_game_id`` as it stands in the DB right now."""
    async with sessionmaker() as s:
        row = await s.get(Server, 1)
        return row.scenario_game_id


@pytest.mark.asyncio
async def test_wrong_guid_logs_one_warning_naming_both_ids(sessionmaker, caplog):
    caplog.set_level(logging.WARNING, logger="reforger.mods.sync")
    async with sessionmaker() as s:
        s.add(Mod(guid=ALT_GUID, is_local=True, name="WCS Everon"))
        s.add(
            ModScenario(
                mod_guid=ALT_GUID,
                game_id=RIGHT_ID,
                name=SCENARIO_NAME,
                game_mode="CONFLICT",
            )
        )
        s.add(Server(id=1, name="Everon 64", scenario_game_id=WRONG_ID))
        await s.commit()

    async with sessionmaker() as s:
        await sync_mod._log_scenario_discrepancies(s)

    recs = _warnings(caplog)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert WRONG_ID in msg
    assert RIGHT_ID in msg
    assert await _configured_scenario_id(sessionmaker) == WRONG_ID


@pytest.mark.asyncio
async def test_exact_match_logs_nothing(sessionmaker, caplog):
    caplog.set_level(logging.WARNING, logger="reforger.mods.sync")
    async with sessionmaker() as s:
        s.add(Mod(guid=ALT_GUID, is_local=True, name="WCS Everon"))
        s.add(
            ModScenario(
                mod_guid=ALT_GUID,
                game_id=RIGHT_ID,
                name=SCENARIO_NAME,
                game_mode="CONFLICT",
            )
        )
        s.add(Server(id=1, name="Everon 64", scenario_game_id=RIGHT_ID))
        await s.commit()

    async with sessionmaker() as s:
        await sync_mod._log_scenario_discrepancies(s)

    assert _warnings(caplog) == []
    assert await _configured_scenario_id(sessionmaker) == RIGHT_ID


@pytest.mark.asyncio
async def test_ambiguous_path_candidates_log_nothing(sessionmaker, caplog):
    caplog.set_level(logging.WARNING, logger="reforger.mods.sync")
    async with sessionmaker() as s:
        s.add(Mod(guid=ALT_GUID, is_local=True, name="WCS Everon"))
        s.add(Mod(guid=MIRROR_GUID, is_local=True, name="WCS Everon (mirror)"))
        s.add(
            ModScenario(
                mod_guid=ALT_GUID, game_id=RIGHT_ID, name=SCENARIO_NAME,
            )
        )
        s.add(
            ModScenario(
                mod_guid=MIRROR_GUID,
                game_id="{" + MIRROR_GUID + "}" + PATH,
                name=SCENARIO_NAME + " (alt upload)",
            )
        )
        s.add(Server(id=1, name="Everon 64", scenario_game_id=WRONG_ID))
        await s.commit()

    async with sessionmaker() as s:
        await sync_mod._log_scenario_discrepancies(s)

    assert _warnings(caplog) == []
    assert await _configured_scenario_id(sessionmaker) == WRONG_ID
