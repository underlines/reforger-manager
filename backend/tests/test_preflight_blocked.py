"""Pre-flight regressions: engine-builtin GUIDs must never be blocked, and
only a *newer* declared game version is fatal, not an older one.

Story S1: ``preflight.py`` never imported
``mods.resolve.ENGINE_BUILTIN_GUIDS``, so the vanilla base-game GUID
(``58D0FB3206B6F859`` — present in nearly every addon's dependency tree) got
looked up on the Workshop like a real mod and could fail its availability /
compat checks, dragging the verdict to "blocked" for every modded server.
Separately, ``_version_compat`` blocked any declared game version behind the
installed engine by major or by 3+ minors; only a *newer* declared version is
actually fatal — an older one is a built-against marker, not a compat gate.
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
from app.models import Engine, Server, ServerMod
from app.mods.resolve import ENGINE_BUILTIN_GUIDS, ResolvedNode, ResolvedTree
from app.servers import preflight as preflight_mod

ROOT_GUID = "1111111111111111"
ENGINE_GUID = next(iter(ENGINE_BUILTIN_GUIDS))  # "58D0FB3206B6F859"


@pytest_asyncio.fixture
async def sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_engine_builtin_guid_never_blocks(sessionmaker, monkeypatch):
    """A dependency tree that reaches the base-game GUID must skip it
    entirely: no Workshop lookup, no check naming it, no resolved_mods entry,
    and it must never be the reason the verdict is "blocked"."""
    async with sessionmaker() as s:
        s.add(Server(id=1, name="Test Server"))
        s.add(ServerMod(server_id=1, mod_guid=ROOT_GUID, load_order=0, enabled=True))
        s.add(Engine(installed_version="1.8.0.10"))
        await s.commit()

    tree = ResolvedTree(
        roots=[ROOT_GUID],
        nodes=[
            ResolvedNode(guid=ROOT_GUID, name="Test Mod", via="api", state="ok", depth=0),
            ResolvedNode(guid=ENGINE_GUID, name=None, via="gproj", state="ok", depth=1),
        ],
        edges=[(ROOT_GUID, ENGINE_GUID)],
    )

    async def fake_resolve(session, roots, **kwargs):
        return tree

    async def fake_get_mod(guid):
        assert guid != ENGINE_GUID, "engine-builtin GUID must never hit the Workshop"
        return {"name": "Test Mod", "version": "1.0.0"}

    async def fake_get_versions(guid):
        assert guid != ENGINE_GUID, "engine-builtin GUID must never hit the Workshop"
        return [{"version": "1.0.0", "game_version": "1.8.0.10", "size": 1000}]

    monkeypatch.setattr(preflight_mod, "resolve_dependencies", fake_resolve)
    monkeypatch.setattr(preflight_mod.workshop, "get_mod", fake_get_mod)
    monkeypatch.setattr(preflight_mod.workshop, "get_versions", fake_get_versions)

    async with sessionmaker() as s:
        report = await preflight_mod.preflight(s, 1)

    assert not any(check.guid == ENGINE_GUID for check in report.checks)
    assert not any(ENGINE_GUID in check.detail for check in report.checks)
    assert not any(mod["guid"] == ENGINE_GUID for mod in report.resolved_mods)
    assert all(mod["local"] is False for mod in report.resolved_mods)
    assert report.verdict != "blocked"


def test_older_declared_version_warns_not_blocks():
    # Same major, 4 minors behind the installed engine — used to hard-block.
    assert preflight_mod._version_compat("1.4.0.0", "1.8.0.10") == "warn"


def test_newer_declared_version_still_blocks():
    # Regression guard: the addon needs a newer engine than is installed —
    # that direction stays genuinely fatal.
    assert preflight_mod._version_compat("1.9.0.0", "1.8.0.10") == "blocked"


def test_verdict_is_warn_when_only_warnings_are_present():
    checks = [
        preflight_mod.PreflightCheck("a", "green", "fine"),
        preflight_mod.PreflightCheck("b", "warn", "careful"),
    ]
    assert preflight_mod._verdict(checks) == "warn"
