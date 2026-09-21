"""GET /api/mods/{guid} — fully DB-backed detail view (S1, Sprint 13).

No live Workshop-API calls belong in the detail route any more: version
history moved to a new on-demand ``/{guid}/versions`` route, and the
dependency tree is resolved from the DB only (``use_api=False, use_disk=True``).
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

from fastapi import HTTPException

from app.api import mods as mods_api
from app.core.db import Base
from app.models import Mod, ModDependency
from app.mods import resolve as resolve_mod
from app.mods.workshop import WorkshopError

MOD_A = "AAAAAAAAAAAAAAAA"
MOD_B = "BBBBBBBBBBBBBBBB"
MOD_C = "CCCCCCCCCCCCCCCC"


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
async def test_get_mod_detail_is_db_only(session):
    session.add_all([
        Mod(guid=MOD_A, name="Mod A", is_local=True),
        Mod(guid=MOD_B, name="Mod B", is_local=True),
        Mod(guid=MOD_C, name="Mod C", is_local=False),
        ModDependency(mod_guid=MOD_A, depends_on_guid=MOD_B, source="api"),
        ModDependency(mod_guid=MOD_B, depends_on_guid=MOD_C, source="gproj"),
    ])
    await session.commit()

    # resolve_dependencies is called with use_api=False, so the workshop
    # client it looks up (app.mods.resolve.workshop) must never be touched.
    with patch.object(
        resolve_mod.workshop,
        "get_dependencies",
        new=AsyncMock(side_effect=AssertionError("workshop API called")),
    ):
        result = await mods_api.get_mod_detail(MOD_A, session=session)

    assert result.versions == []
    node_guids = {n.guid for n in result.dependency_tree.nodes}
    edge_pairs = {(e["from"], e["to"]) for e in result.dependency_tree.edges}
    assert MOD_A in node_guids and MOD_B in node_guids
    assert (MOD_A, MOD_B) in edge_pairs
    assert len(result.dependency_tree.nodes) >= 2
    assert len(result.dependency_tree.edges) >= 1


@pytest.mark.asyncio
async def test_get_mod_versions_returns_workshop_result():
    fixed = [{"version": "1.0.0", "changelog": "initial"}]
    with patch.object(mods_api.workshop, "get_versions", new=AsyncMock(return_value=fixed)):
        result = await mods_api.get_mod_versions(MOD_A)

    assert result == fixed


@pytest.mark.asyncio
async def test_get_mod_versions_raises_502_on_workshop_error():
    with patch.object(
        mods_api.workshop, "get_versions", new=AsyncMock(side_effect=WorkshopError("boom"))
    ):
        with pytest.raises(HTTPException) as exc_info:
            await mods_api.get_mod_versions(MOD_A)

    assert exc_info.value.status_code == 502
