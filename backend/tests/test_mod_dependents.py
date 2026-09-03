"""Dependency visibility + delete protection for library mods.

* ``enrich_one`` gives every dependency its own ``mods`` row so a dependency-only
  addon shows up in the library and the server / modpack pickers.
* ``GET /api/mods`` reports ``required_by`` (the mods that declare this one).
* deleting a mod that another assigned mod depends on is refused with a message
  that names what is holding it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.api import mods as mods_api
from app.core.db import Base
from app.models import Mod, ModDependency, Server, ServerMod
from app.mods import sync as sync_mod

PARENT = "AAAAAAAAAAAAAAAA"
CHILD = "BBBBBBBBBBBBBBBB"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as result:
        yield result
    await engine.dispose()


class _FakeClient:
    async def get_mod(self, guid):
        return {"id": guid, "name": "Parent Mod", "summary": "s", "size": 10}

    async def get_versions(self, guid):
        return [{"version": "1.0", "game_version": "1.8.0.10", "size": 10}]

    async def get_scenarios(self, guid):
        return []

    async def get_dependencies(self, guid):
        return [{"id": CHILD, "name": "Shared Core"}]


@pytest.mark.asyncio
async def test_enrich_one_creates_a_row_for_each_dependency(session):
    await sync_mod.enrich_one(session, PARENT, client=_FakeClient())
    await session.commit()

    child = await session.get(Mod, CHILD)
    assert child is not None
    assert child.name == "Shared Core"
    assert child.is_local is False  # no addon dir in this test env


@pytest.mark.asyncio
async def test_list_mods_reports_required_by(session):
    session.add_all([
        Mod(guid=PARENT, name="Parent Mod", is_local=True),
        Mod(guid=CHILD, name="Shared Core", is_local=True),
        ModDependency(mod_guid=PARENT, depends_on_guid=CHILD, source="gproj"),
    ])
    await session.commit()

    listed = await mods_api.list_mods(
        local=None, q=None, update=None, state=None, session=session
    )
    rows = {m.guid: m for m in listed}
    assert [ref.guid for ref in rows[CHILD].required_by] == [PARENT]
    assert rows[PARENT].required_by == []


@pytest.mark.asyncio
async def test_delete_is_refused_and_names_the_dependent(session):
    session.add_all([
        Server(id=1, name="Live Server"),
        ServerMod(server_id=1, mod_guid=PARENT),
        Mod(guid=PARENT, name="Parent Mod", is_local=False),
        Mod(guid=CHILD, name="Shared Core", is_local=False),
        ModDependency(mod_guid=PARENT, depends_on_guid=CHILD, source="gproj"),
    ])
    await session.commit()

    with pytest.raises(HTTPException) as caught:
        await mods_api.delete_library_mod(CHILD, session=session)
    assert caught.value.status_code == 409
    assert "Parent Mod" in caught.value.detail


@pytest.mark.asyncio
async def test_unreferenced_dependency_row_still_deletes(session):
    session.add_all([
        Mod(guid=CHILD, name="Shared Core", is_local=False),
    ])
    await session.commit()

    await mods_api.delete_library_mod(CHILD, session=session)
    assert await session.get(Mod, CHILD) is None
