"""``refresh_local_mods`` — the targeted rescan run after a force-download.

After ``POST /api/mods/{guid}/download`` the addon is on disk but the ``Mod``
row would stay ``is_local = False`` (and absent from ``/api/storage``) until the
next full ``mod_sync``. ``_job_mod_download`` calls this to close that gap.
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
import app.core.db as db_mod
from app.models import Mod
from app.mods import sync as sync_mod
from app.mods.scanner import ScanResult, ScannedMod

GUID = "5965550F24A0C152"


def _scanned(guid=GUID, version="1.2.0", size=201671):
    return ScannedMod(
        guid=guid, name="Where Am I", summary="Shows where you are on the map",
        tags=["GPS"], version=version, size=size, is_unlisted=False,
        is_deleted_local=False, dep_guids=[], scenarios=[], has_thumbnail=True,
        dir_name=f"WhereAmI_{guid}",
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
async def test_refresh_flips_is_local_and_records_size(sessionmaker, monkeypatch):
    # a URL-added row that has never been downloaded
    async with sessionmaker() as s:
        s.add(Mod(guid=GUID, is_local=False, name="Where Am I"))
        await s.commit()

    monkeypatch.setattr(sync_mod, "scan_all", lambda: ScanResult(mods=[_scanned()]))
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == [GUID]

    async with sessionmaker() as s:
        row = await s.get(Mod, GUID)
        assert row.is_local is True
        assert row.size == 201671
        assert row.installed_version == "1.2.0"


@pytest.mark.asyncio
async def test_refresh_ignores_mods_not_asked_for(sessionmaker, monkeypatch):
    other = "0123456789ABCDEF"
    monkeypatch.setattr(
        sync_mod, "scan_all",
        lambda: ScanResult(mods=[_scanned(), _scanned(guid=other)]),
    )
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == [GUID]
    async with sessionmaker() as s:
        assert await s.get(Mod, other) is None


@pytest.mark.asyncio
async def test_refresh_no_guids_is_a_noop(sessionmaker, monkeypatch):
    called = False

    def _scan():
        nonlocal called
        called = True
        return ScanResult(mods=[])

    monkeypatch.setattr(sync_mod, "scan_all", _scan)
    assert await sync_mod.refresh_local_mods([]) == []
    assert called is False


@pytest.mark.asyncio
async def test_refresh_when_addon_dir_not_found_leaves_row_untouched(sessionmaker, monkeypatch):
    async with sessionmaker() as s:
        s.add(Mod(guid=GUID, is_local=False))
        await s.commit()
    monkeypatch.setattr(sync_mod, "scan_all", lambda: ScanResult(mods=[]))  # nothing on disk
    found = await sync_mod.refresh_local_mods([GUID])
    assert found == []
    async with sessionmaker() as s:
        assert (await s.get(Mod, GUID)).is_local is False
