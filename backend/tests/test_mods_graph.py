"""GET /api/mods/graph — the entire mod library's dependency graph from
Postgres in one pair of queries: no Workshop API calls, no BFS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.api import mods as mods_api
from app.core.db import Base
from app.models import Mod, ModDependency
from app.mods.resolve import ENGINE_BUILTIN_GUIDS
from app.schemas.mod import ModGraphNodeOut

MOD_A = "AAAAAAAAAAAAAAAA"
MOD_B = "BBBBBBBBBBBBBBBB"
MOD_C = "CCCCCCCCCCCCCCCC"
BUILTIN = next(iter(ENGINE_BUILTIN_GUIDS))  # e.g. 58D0FB3206B6F859
DANGLING = "DEADDEADDEADDEAD"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as result:
        yield result
    await engine.dispose()


@pytest.mark.asyncio
async def test_graph_includes_all_library_nodes_and_edges(session):
    session.add_all([
        Mod(guid=MOD_A, name="Mod A", is_local=True),
        Mod(guid=MOD_B, name="Mod B", is_local=True),
        Mod(guid=MOD_C, name="Mod C", is_local=False),
        ModDependency(mod_guid=MOD_A, depends_on_guid=MOD_B, source="api"),
        ModDependency(mod_guid=MOD_B, depends_on_guid=MOD_C, source="gproj"),
    ])
    await session.commit()

    result = await mods_api.get_mods_graph(session=session)

    assert {n.guid for n in result.nodes} == {MOD_A, MOD_B, MOD_C}
    edges = {(e["from"], e["to"], e["source"]) for e in result.edges}
    assert edges == {
        (MOD_A, MOD_B, "api"),
        (MOD_B, MOD_C, "gproj"),
    }


@pytest.mark.asyncio
async def test_graph_synthesizes_builtin_node_with_no_mod_row(session):
    session.add_all([
        Mod(guid=MOD_A, name="Mod A", is_local=True),
        ModDependency(mod_guid=MOD_A, depends_on_guid=BUILTIN, source="gproj"),
    ])
    await session.commit()

    result = await mods_api.get_mods_graph(session=session)

    builtin_nodes = [n for n in result.nodes if n.guid == BUILTIN]
    assert len(builtin_nodes) == 1
    assert builtin_nodes[0].is_builtin is True


@pytest.mark.asyncio
async def test_graph_leaves_dangling_dependency_without_a_node(session):
    session.add_all([
        Mod(guid=MOD_A, name="Mod A", is_local=True),
        ModDependency(mod_guid=MOD_A, depends_on_guid=DANGLING, source="gproj"),
    ])
    await session.commit()

    result = await mods_api.get_mods_graph(session=session)

    assert any(e["to"] == DANGLING for e in result.edges)
    assert all(n.guid != DANGLING for n in result.nodes)


@pytest.mark.asyncio
async def test_graph_never_touches_the_workshop_client(session):
    session.add_all([
        Mod(guid=MOD_A, name="Mod A", is_local=True),
        Mod(guid=MOD_B, name="Mod B", is_local=True),
        ModDependency(mod_guid=MOD_A, depends_on_guid=MOD_B, source="api"),
    ])
    await session.commit()

    with patch.object(
        mods_api.workshop, "get_mod", new=AsyncMock(side_effect=AssertionError("workshop called"))
    ), patch.object(
        mods_api.workshop,
        "get_dependencies",
        new=AsyncMock(side_effect=AssertionError("workshop called")),
    ), patch.object(
        mods_api.workshop, "get_versions", new=AsyncMock(side_effect=AssertionError("workshop called"))
    ), patch.object(
        mods_api.workshop, "search", new=AsyncMock(side_effect=AssertionError("workshop called"))
    ):
        result = await mods_api.get_mods_graph(session=session)

    assert {n.guid for n in result.nodes} == {MOD_A, MOD_B}


def test_graph_node_schema_stays_minimal():
    dumped = ModGraphNodeOut(guid=MOD_A, name="Mod A").model_dump()
    for forbidden in ("size", "tags", "thumbnail", "versions"):
        assert forbidden not in dumped
