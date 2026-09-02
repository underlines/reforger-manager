"""Focused SQLite coverage for read-only server preflight."""
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
from app.core.config import settings
from app.models.base import ApiState
from app.models.engine import Engine
from app.models.mod import Mod, ModDependency
from app.models.server import Server, ServerMod
from app.mods.resolve import ResolvedNode, ResolvedTree
import app.mods.resolve as resolve_module
from app.mods.workshop import ModNotFound
from app.servers import preflight as preflight_module
from app.steam import engine as engine_module


GUID_A = "AAAAAAAAAAAAAAAA"
GUID_B = "BBBBBBBBBBBBBBBB"
LOG_GUIDS = ["6512CC017515F9EB", "65906C6513A8D3D4", "64869009DD4637C4", "64863EE1C8CF7512"]


class FakeWorkshop:
    def __init__(self, missing=()):
        self.missing = set(missing)

    async def get_mod(self, guid):
        if guid in self.missing:
            raise ModNotFound(guid)
        return {"id": guid, "name": f"Mod {guid}", "version": "1.0", "size": 10}

    async def get_versions(self, guid):
        if guid in self.missing:
            raise ModNotFound(guid)
        return [{"version": "1.0", "game_version": "1.8.0.10", "size": 10}]

    async def get_dependencies(self, guid):
        if guid in self.missing:
            raise ModNotFound(guid)
        return []


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as result:
        yield result
    await engine.dispose()


async def _tree(_session, roots):
    return ResolvedTree(roots=roots, nodes=[ResolvedNode(roots[0], None, "api", "ok", 0)])


@pytest.mark.asyncio
async def test_green_report_and_exact_json_shape(session, monkeypatch):
    session.add_all([
        Engine(id=1, installed_build="24501482", installed_version="1.8.0.10"),
        Server(id=1, name="green"),
        ServerMod(server_id=1, mod_guid=GUID_A),
        Mod(guid=GUID_A, is_local=True, latest_version="1.0"),
    ])
    await session.commit()
    monkeypatch.setattr(preflight_module, "resolve_dependencies", _tree)
    monkeypatch.setattr(preflight_module, "workshop", FakeWorkshop())

    report = await preflight_module.preflight(session, 1)
    assert report.as_dict() == {
        "verdict": "green",
        "checks": [{"name": "preflight", "level": "green", "detail": "All enabled addons and resolved dependencies passed the available checks."}],
        "resolved_mods": [{"guid": GUID_A, "name": f"Mod {GUID_A}", "source": "api", "state": "ok", "version": "1.0", "availability": "ok", "game_version": "1.8.0.10"}],
    }


@pytest.mark.asyncio
async def test_engine_mismatch_is_blocked(session, monkeypatch):
    session.add_all([
        Engine(id=1, installed_build="24501482", installed_version="1.8.0.10"),
        Server(id=2, name="mismatch"), ServerMod(server_id=2, mod_guid=GUID_A),
        Mod(guid=GUID_A, is_local=True),
    ])
    await session.commit()
    fake = FakeWorkshop()
    async def old_versions(_guid):
        return [{"version": "1.0", "game_version": "1.2.1.173", "size": 10}]
    fake.get_versions = old_versions
    monkeypatch.setattr(preflight_module, "resolve_dependencies", _tree)
    monkeypatch.setattr(preflight_module, "workshop", fake)

    report = await preflight_module.preflight(session, 2)
    assert report.verdict == "blocked"
    assert any(check.name == f"Engine compatibility {GUID_A}" and check.level == "blocked" for check in report.checks)


@pytest.mark.asyncio
async def test_ronin_is_blocked_with_current_build_fallback_before_server_run(session, monkeypatch):
    session.add_all([
        Server(id=7, name="ronin"), ServerMod(server_id=7, mod_guid=GUID_A),
        Mod(guid=GUID_A, is_local=True),
    ])
    await session.commit()
    monkeypatch.setattr(engine_module, "read_installed_build", lambda: {"buildid": "24501482"})
    await engine_module.seed_engine(session)
    fake = FakeWorkshop()

    async def ronin_versions(_guid):
        return [{"version": "1.0.27", "game_version": "1.2.1.173", "size": 10}]

    fake.get_versions = ronin_versions
    monkeypatch.setattr(preflight_module, "resolve_dependencies", _tree)
    monkeypatch.setattr(preflight_module, "workshop", fake)

    report = await preflight_module.preflight(session, 7)

    assert report.verdict == "blocked"
    assert any(
        check.name == f"Engine compatibility {GUID_A}"
        and check.level == "blocked"
        and "1.2.1.173" in check.detail
        and "1.8.0.10" in check.detail
        for check in report.checks
    )


@pytest.mark.asyncio
async def test_per_server_stale_pin_overrides_library_pin(session, monkeypatch):
    session.add_all([
        Engine(id=1, installed_build="new", installed_version="1.8.0.10"),
        Server(id=3, name="pin"),
        ServerMod(server_id=3, mod_guid=GUID_A, pinned_version="0.9", pinned_at_build="old"),
        Mod(guid=GUID_A, is_local=True, pinned_version="1.0", pinned_at_build="new"),
    ])
    await session.commit()
    fake = FakeWorkshop()
    async def versions(_guid):
        return [{"version": "0.9", "game_version": "1.8.0.10", "size": 10}, {"version": "1.0", "game_version": "1.8.0.10", "size": 10}]
    fake.get_versions = versions
    monkeypatch.setattr(preflight_module, "resolve_dependencies", _tree)
    monkeypatch.setattr(preflight_module, "workshop", fake)

    report = await preflight_module.preflight(session, 3)
    stale = next(check for check in report.checks if check.name == f"Stale pin {GUID_A}")
    assert report.verdict == "warn"
    assert "0.9" in stale.detail and stale.fix == "Unpin and take latest."


@pytest.mark.asyncio
async def test_404_gproj_and_profile_log_fallback(session, monkeypatch, tmp_path):
    """The API fallback keeps gproj edges, then a server-9 log supplies the rest."""
    session.add_all([
        Engine(id=1, installed_version="1.8.0.10"),
        Server(id=9, name="fallback"),
        ServerMod(server_id=9, mod_guid=GUID_A),
        ServerMod(server_id=9, mod_guid="CCCCCCCCCCCCCCCC"),
        Mod(guid=GUID_A, is_local=False, api_state=ApiState.not_found),
        Mod(guid=GUID_B, is_local=True, api_state=ApiState.ok),
        ModDependency(mod_guid=GUID_A, depends_on_guid=GUID_B, source="gproj"),
    ])
    await session.commit()
    profile = tmp_path / "9" / "logs"
    profile.mkdir(parents=True)
    (profile / "console.log").write_text("\n".join(f"Addon {guid} - Addon was not found on workshop." for guid in LOG_GUIDS), encoding="utf-8")
    monkeypatch.setattr(settings, "profiles_dir", tmp_path)
    fake = FakeWorkshop(missing={GUID_A, "CCCCCCCCCCCCCCCC", *LOG_GUIDS})
    monkeypatch.setattr(preflight_module, "workshop", fake)
    monkeypatch.setattr(resolve_module, "workshop", fake)

    report = await preflight_module.preflight(session, 9)
    guids = {item["guid"] for item in report.resolved_mods}
    assert report.verdict == "blocked"
    assert GUID_B in guids  # persisted addon.gproj edge
    assert set(LOG_GUIDS) <= guids  # REFORGER_9-style recovery
    assert any(check.name == f"Workshop {GUID_A}" and check.level == "blocked" for check in report.checks)
