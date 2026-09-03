"""``resolved_mod_entries`` — the dependency-closure expansion that feeds
``config.json`` ``game.mods[]``.

A modded Reforger server whose config lists only the leaf mods (not their
dependencies) starts and registers but rejects every client join
(``RoomsAcceptPlayerS2S`` / ``InvalidSessionTicket``). The generated config must
carry the full, de-duplicated, load-ordered closure.
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

from app.api import servers as servers_api
from app.core.db import Base
from app.models.base import ApiState
from app.models.mod import Mod, ModDependency
from app.models.server import Server, ServerMod
from app.servers import config_gen
import app.mods.resolve as resolve_module

LEAF = "AAAAAAAAAAAAAAAA"
DEP1 = "BBBBBBBBBBBBBBBB"
DEP2 = "CCCCCCCCCCCCCCCC"
BASE_GAME = "58D0FB3206B6F859"  # engine builtin — never emitted
MISSING = "DDDDDDDDDDDDDDDD"


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _entries(factory, server_id):
    async with factory() as session:
        server = await servers_api._load(session, server_id)
        return await config_gen.resolved_mod_entries(session, server)


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    # Keep the closure DB-only unless a test opts into the on-disk gproj fallback.
    monkeypatch.setattr(resolve_module, "resolve_addon_dir", lambda guid: None)
    monkeypatch.setattr(resolve_module, "_disk_gproj_children", lambda guid: [])


@pytest.mark.asyncio
async def test_transitive_closure_is_ordered_deps_first(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True),
            Mod(guid=DEP2, name="Dep Two", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
            ModDependency(mod_guid=DEP1, depends_on_guid=DEP2, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [DEP2, DEP1, LEAF]
    assert [e.name for e in entries] == ["Dep Two", "Dep One", "Leaf"]


@pytest.mark.asyncio
async def test_explicit_and_dependency_overlap_is_deduped(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            ServerMod(server_id=1, mod_guid=DEP1, mod_name="Dep One", load_order=1, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [DEP1, LEAF]


@pytest.mark.asyncio
async def test_engine_builtin_guid_is_dropped(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=BASE_GAME, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [LEAF]


@pytest.mark.asyncio
async def test_unloadable_dependency_is_dropped_but_leaf_survives(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            # MISSING: no Mod row, no disk dir, not resolvable -> must not be emitted.
            ModDependency(mod_guid=LEAF, depends_on_guid=MISSING, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [LEAF]


@pytest.mark.asyncio
async def test_pins_come_from_server_mod_then_library(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(
                server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True,
                pinned_version="1.2.3",
            ),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True, pinned_version="9.9"),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
        ])
        await session.commit()

    entries = {e.mod_id: e for e in await _entries(sessions, 1)}
    assert entries[LEAF].version == "1.2.3"      # per-server pin
    assert entries[DEP1].version == "9.9"        # library pin, dependency-only mod


@pytest.mark.asyncio
async def test_disabled_assignment_contributes_nothing(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=False),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
        ])
        await session.commit()

    assert await _entries(sessions, 1) == []


@pytest.mark.asyncio
async def test_dependency_cycle_terminates(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
            ModDependency(mod_guid=DEP1, depends_on_guid=LEAF, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert sorted(e.mod_id for e in entries) == sorted([LEAF, DEP1])


@pytest.mark.asyncio
async def test_api_state_ok_makes_a_non_local_dependency_loadable(sessions):
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=False, api_state=ApiState.ok),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
        ])
        await session.commit()

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [DEP1, LEAF]


@pytest.mark.asyncio
async def test_on_disk_gproj_fills_a_gap_the_db_never_enriched(sessions, monkeypatch):
    """DEP1 has no persisted edges, but its addon.gproj on disk needs DEP2."""
    async with sessions() as session:
        session.add_all([
            Server(id=1, name="srv"),
            ServerMod(server_id=1, mod_guid=LEAF, mod_name="Leaf", load_order=0, enabled=True),
            Mod(guid=LEAF, name="Leaf", is_local=True),
            Mod(guid=DEP1, name="Dep One", is_local=True),
            Mod(guid=DEP2, name="Dep Two", is_local=True),
            ModDependency(mod_guid=LEAF, depends_on_guid=DEP1, source="gproj"),
        ])
        await session.commit()

    monkeypatch.setattr(resolve_module, "resolve_addon_dir", lambda guid: Path("/fake") / guid)
    monkeypatch.setattr(
        resolve_module,
        "_disk_gproj_children",
        lambda guid: [(DEP2, None)] if guid == DEP1 else [],
    )

    entries = await _entries(sessions, 1)
    assert [e.mod_id for e in entries] == [DEP2, DEP1, LEAF]
