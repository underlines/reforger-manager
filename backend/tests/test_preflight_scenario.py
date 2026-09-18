"""Pre-flight scenario validation (story S2).

``server.scenario_game_id`` is checked against the ``mod_scenarios`` rows of
the server's *enabled* mods. A scenario id the manager cannot verify is only
ever a ``warn`` -- an un-enriched library is not proof of a broken server.
The real incident: server 32 was set to the mod's own GUID
(``{6148289172F86E7A}``) for a mission the Workshop verifies under a different
resource GUID (``{02778C5E44407C03}``); the check must name that correction.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.core.db import Base
from app.models import Engine, Mod, ModScenario, Server, ServerMod
from app.mods.resolve import ResolvedNode, ResolvedTree
from app.servers import preflight as preflight_mod

ROOT_GUID = "1111111111111111"
WRONG_GUID = "6148289172F86E7A"
RIGHT_GUID = "02778C5E44407C03"
MISSION_PATH = "Missions/WCS_Everon_Conflict_NvS_Layout1.conf"


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _run(sessionmaker, monkeypatch, scenario_game_id, scenarios):
    """Run preflight with one enabled root mod and the given ModScenario rows."""
    async with sessionmaker() as s:
        s.add(Server(id=1, name="Test Server", scenario_game_id=scenario_game_id))
        s.add(ServerMod(server_id=1, mod_guid=ROOT_GUID, load_order=0, enabled=True))
        s.add(Engine(installed_version="1.8.0.10"))
        s.add(Mod(guid=ROOT_GUID, name="Test Mod", latest_version="1.0.0", is_local=True))
        for mod_guid, game_id, name in scenarios:
            s.add(ModScenario(mod_guid=mod_guid, game_id=game_id, name=name))
        await s.commit()

    tree = ResolvedTree(
        roots=[ROOT_GUID],
        nodes=[ResolvedNode(guid=ROOT_GUID, name="Test Mod", via="api", state="ok", depth=0)],
        edges=[],
    )

    async def fake_resolve(session, roots, **kwargs):
        return tree

    async def fake_get_mod(guid):
        return {"name": "Test Mod", "version": "1.0.0"}

    async def fake_get_versions(guid):
        return [{"version": "1.0.0", "game_version": "1.8.0.10", "size": 1000}]

    monkeypatch.setattr(preflight_mod, "resolve_dependencies", fake_resolve)
    monkeypatch.setattr(preflight_mod.workshop, "get_mod", fake_get_mod)
    monkeypatch.setattr(preflight_mod.workshop, "get_versions", fake_get_versions)

    async with sessionmaker() as s:
        return await preflight_mod.preflight(s, 1)


def _scenario_checks(report):
    return [check for check in report.checks if check.name == "Scenario"]


def _assert_never_blocked(report):
    assert report.verdict != "blocked"
    assert not any(check.level == "blocked" for check in _scenario_checks(report))


@pytest.mark.asyncio
async def test_api_verified_scenario_emits_no_check(sessionmaker, monkeypatch):
    verified = f"{{{RIGHT_GUID}}}{MISSION_PATH}"
    report = await _run(
        sessionmaker, monkeypatch, verified,
        [(ROOT_GUID, verified, "Everon Conflict")],
    )
    assert _scenario_checks(report) == []
    _assert_never_blocked(report)


@pytest.mark.asyncio
async def test_guess_scenario_warns_for_mod_sync(sessionmaker, monkeypatch):
    guess = f"{{{WRONG_GUID}}}{MISSION_PATH}"
    report = await _run(
        sessionmaker, monkeypatch, guess,
        [(ROOT_GUID, guess, None)],
    )
    checks = _scenario_checks(report)
    assert len(checks) == 1
    assert checks[0].level == "warn"
    assert "mod sync" in (checks[0].fix or "").lower()
    _assert_never_blocked(report)


@pytest.mark.asyncio
async def test_server_32_shape_names_verified_id(sessionmaker, monkeypatch):
    wrong = f"{{{WRONG_GUID}}}{MISSION_PATH}"
    verified = f"{{{RIGHT_GUID}}}{MISSION_PATH}"
    report = await _run(
        sessionmaker, monkeypatch, wrong,
        [(ROOT_GUID, verified, "Everon Conflict")],
    )
    checks = _scenario_checks(report)
    assert len(checks) == 1
    assert checks[0].level == "warn"
    assert verified in checks[0].detail
    assert wrong in checks[0].detail
    _assert_never_blocked(report)


@pytest.mark.asyncio
async def test_no_match_warns_generic(sessionmaker, monkeypatch):
    configured = f"{{{WRONG_GUID}}}{MISSION_PATH}"
    other = f"{{{RIGHT_GUID}}}Missions/Some_Other_Mission.conf"
    report = await _run(
        sessionmaker, monkeypatch, configured,
        [(ROOT_GUID, other, "Some Other Mission")],
    )
    checks = _scenario_checks(report)
    assert len(checks) == 1
    assert checks[0].level == "warn"
    assert "disabled or absent" in checks[0].detail
    _assert_never_blocked(report)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_game_id,rows",
    [
        (
            f"{{{RIGHT_GUID}}}{MISSION_PATH}",
            [(ROOT_GUID, f"{{{RIGHT_GUID}}}{MISSION_PATH}", "Everon Conflict")],
        ),
        (
            f"{{{WRONG_GUID}}}{MISSION_PATH}",
            [(ROOT_GUID, f"{{{WRONG_GUID}}}{MISSION_PATH}", None)],
        ),
        (
            f"{{{WRONG_GUID}}}{MISSION_PATH}",
            [(ROOT_GUID, f"{{{RIGHT_GUID}}}{MISSION_PATH}", "Everon Conflict")],
        ),
        (
            f"{{{WRONG_GUID}}}{MISSION_PATH}",
            [(ROOT_GUID, f"{{{RIGHT_GUID}}}Missions/Other.conf", "Other")],
        ),
    ],
)
async def test_scenario_validation_is_never_blocked(
    sessionmaker, monkeypatch, scenario_game_id, rows
):
    report = await _run(sessionmaker, monkeypatch, scenario_game_id, rows)
    _assert_never_blocked(report)
