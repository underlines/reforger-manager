"""Route-level integration checks for the Phase 3b API wiring."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import mods, servers
from app.schemas.server import ServerOut
from app.schemas.server import ServerOut


def test_mod_actions_precede_dynamic_guid_route() -> None:
    paths = [route.path for route in mods.router.routes]

    dynamic = paths.index("/mods/{guid}")
    assert paths.index("/mods/updates/check") < dynamic
    assert paths.index("/mods/updates/apply") < dynamic
    assert paths.index("/mods/verify") < dynamic
    assert paths.index("/mods/{guid}/pin") < dynamic


def test_server_phase3b_routes_are_registered() -> None:
    paths = {route.path for route in servers.router.routes}

    assert {
        "/servers/{server_id}/preflight",
        "/servers/{server_id}/mods/update/check",
        "/servers/{server_id}/mods/update/apply",
        "/servers/{server_id}/mods/{guid}/pin",
        "/servers/{server_id}/rcon",
        "/servers/{server_id}/players",
        "/servers/{server_id}/stats",
        "/servers/{server_id}/log",
    } <= paths


def test_server_response_exposes_json_safe_diagnosis() -> None:
    server = ServerOut.model_validate({
        "id": 1,
        "name": "test",
        "config_revision": 0,
        "is_running": False,
        "last_diagnosis": {"kind": "addon_blocked", "implicated_guids": ["A" * 16]},
    })

    assert server.model_dump(mode="json")["last_diagnosis"]["kind"] == "addon_blocked"


class QueuedJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_job_recovers_persisted_params(self) -> None:
        from app import main
        from app.core.db import Base
        from app.core.jobs import JobManager
        from app.models import Job, JobState

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            job = Job(kind="test_params", state=JobState.queued, params={"scope": "all"})
            session.add(job)
            await session.commit()

        manager = JobManager(sessions)
        manager.register("test_params", main._job_params)
        with patch.object(main, "SessionLocal", sessions):
            await manager.start()
            await manager._queue.join()
        async with sessions() as session:
            recovered = await session.get(Job, job.id)
            assert recovered is not None
            assert recovered.state == JobState.succeeded
            assert recovered.result == {"scope": "all"}
        await manager.stop()
        await engine.dispose()
