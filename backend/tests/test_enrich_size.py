"""`enrich_one` must record the Workshop package size.

Before the fix only the on-disk scanner set ``Mod.size``, so a mod added via
``POST /api/mods/add`` but never downloaded kept ``size = NULL`` — and the
free-space guard (S11/S13) then rejected every first-time download with
"cannot verify free space: ... no recorded size".
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
from app.models import Mod
from app.mods import sync as sync_mod

GUID = "5965550F24A0C152"


@pytest_asyncio.fixture
async def session():
    database = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with database.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(database, expire_on_commit=False)
    async with factory() as s:
        yield s
    await database.dispose()


class _FakeClient:
    def __init__(self, mod: dict, versions: list[dict]):
        self._mod = mod
        self._versions = versions

    async def get_mod(self, guid):  # noqa: ANN001
        return dict(self._mod, id=guid)

    async def get_versions(self, guid):  # noqa: ANN001
        return list(self._versions)

    async def get_scenarios(self, guid):  # noqa: ANN001
        return []

    async def get_dependencies(self, guid):  # noqa: ANN001
        return []


@pytest.mark.asyncio
async def test_enrich_one_records_size_from_mod_object(session):
    client = _FakeClient(
        mod={"name": "Where Am I", "summary": "x", "size": 201671},
        versions=[{"version": "1.2.0", "game_version": "1.8.0", "size": 199999}],
    )
    row = await sync_mod.enrich_one(session, GUID, client=client)
    assert row.size == 201671  # the mod object's size wins


@pytest.mark.asyncio
async def test_enrich_one_falls_back_to_latest_version_size(session):
    client = _FakeClient(
        mod={"name": "Where Am I", "summary": "x"},  # no top-level size
        versions=[{"version": "1.2.0", "size": 199999}],
    )
    row = await sync_mod.enrich_one(session, GUID, client=client)
    assert row.size == 199999


@pytest.mark.asyncio
async def test_enrich_one_leaves_size_none_when_api_has_none(session):
    client = _FakeClient(mod={"name": "n", "summary": "s"}, versions=[{"version": "1.0.0"}])
    row = await sync_mod.enrich_one(session, GUID, client=client)
    assert row.size is None


@pytest.mark.asyncio
async def test_enrich_one_ignores_a_non_numeric_size(session):
    client = _FakeClient(mod={"name": "n", "summary": "s", "size": "not-a-number"}, versions=[])
    row = await sync_mod.enrich_one(session, GUID, client=client)
    assert row.size is None
