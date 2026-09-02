"""Focused coverage for installed-engine display-version provenance."""
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
from app.models.engine import Engine
from app.steam import engine as engine_module


@pytest_asyncio.fixture
async def session():
    database = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(database, expire_on_commit=False)
    async with factory() as result:
        yield result
    await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["seed", "refresh", "updated"])
async def test_current_build_cold_start_uses_verified_fallback(session, monkeypatch, operation):
    monkeypatch.setattr(engine_module, "read_installed_build", lambda: {"buildid": "24501482"})
    if operation == "seed":
        row = await engine_module.seed_engine(session)
    elif operation == "refresh":
        monkeypatch.setattr(engine_module, "fetch_latest_build", _no_latest_build)
        row = await engine_module.refresh_engine(session)
    else:
        row = await engine_module.mark_engine_updated(session)

    assert row.installed_build == "24501482"
    assert row.installed_version == "1.8.0.10"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["seed", "refresh", "updated"])
async def test_unknown_changed_build_clears_stale_display_version(session, monkeypatch, operation):
    session.add(Engine(id=1, installed_build="24501482", installed_version="1.8.0.10"))
    await session.commit()
    monkeypatch.setattr(engine_module, "read_installed_build", lambda: {"buildid": "99999999"})
    if operation == "seed":
        row = await engine_module.seed_engine(session)
    elif operation == "refresh":
        monkeypatch.setattr(engine_module, "fetch_latest_build", _no_latest_build)
        row = await engine_module.refresh_engine(session)
    else:
        row = await engine_module.mark_engine_updated(session)

    assert row.installed_build == "99999999"
    assert row.installed_version is None


async def _no_latest_build():
    return {}
