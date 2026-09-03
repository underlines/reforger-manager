"""Job queue management: cancel / delete / prune routes and the runtime
watchdog that auto-cancels an over-running job."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.jobs import JobManager
from app.core.security import get_current_user
from app.models import Job, JobState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _RouteFixture:
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.main import app

        self.app = app
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

        async def override_session() -> AsyncSession:
            async with self.sessions() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_current_user] = lambda: None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for key in (get_session, get_current_user):
            self.app.dependency_overrides.pop(key, None)
        await self.engine.dispose()

    async def _add(self, **fields) -> int:
        async with self.sessions() as session:
            job = Job(kind=fields.pop("kind", "verify_repair"), **fields)
            session.add(job)
            await session.commit()
            return job.id


class CancelRouteTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    async def test_missing_job_is_404(self) -> None:
        response = await self.client.post("/api/jobs/999/cancel")
        self.assertEqual(response.status_code, 404, response.text)

    async def test_terminal_job_is_409(self) -> None:
        job_id = await self._add(state=JobState.succeeded)
        response = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_running_job_delegates_to_manager(self) -> None:
        job_id = await self._add(state=JobState.running)
        with patch(
            "app.api.jobs.job_manager.request_cancel", return_value=True
        ) as request_cancel:
            response = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"job_id": job_id, "cancelled": True})
        request_cancel.assert_awaited_once()
        self.assertEqual(request_cancel.await_args.args[0], job_id)

    async def test_untracked_running_job_is_marked_cancelled_directly(self) -> None:
        job_id = await self._add(state=JobState.running)
        with patch(
            "app.api.jobs.job_manager.request_cancel", return_value=False
        ):
            response = await self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        async with self.sessions() as session:
            job = await session.get(Job, job_id)
            self.assertEqual(job.state, JobState.cancelled)
            self.assertIsNotNone(job.finished_at)


class DeleteAndPruneRouteTests(_RouteFixture, unittest.IsolatedAsyncioTestCase):
    async def test_delete_refuses_running_job(self) -> None:
        job_id = await self._add(state=JobState.running)
        response = await self.client.delete(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 409, response.text)

    async def test_delete_removes_finished_job(self) -> None:
        job_id = await self._add(state=JobState.failed)
        response = await self.client.delete(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 204, response.text)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Job, job_id))

    async def test_prune_deletes_only_terminal_rows(self) -> None:
        running = await self._add(state=JobState.running)
        queued = await self._add(state=JobState.queued)
        await self._add(state=JobState.succeeded)
        await self._add(state=JobState.failed)
        await self._add(state=JobState.cancelled)

        response = await self.client.post("/api/jobs/prune")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": 3})
        async with self.sessions() as session:
            remaining = {
                job.id for job in (await session.execute(select(Job))).scalars()
            }
        self.assertEqual(remaining, {running, queued})

    async def test_prune_honours_explicit_state_filter(self) -> None:
        await self._add(state=JobState.succeeded)
        keep_failed = await self._add(state=JobState.failed)

        response = await self.client.post(
            "/api/jobs/prune", json={"states": ["succeeded"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": 1})
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(Job, keep_failed))

    async def test_prune_rejects_non_terminal_state(self) -> None:
        response = await self.client.post(
            "/api/jobs/prune", json={"states": ["running"]}
        )
        self.assertEqual(response.status_code, 400, response.text)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_over_running_job_is_auto_cancelled(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        cancelled = asyncio.Event()

        async def slow_job(ctx) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        manager = JobManager(sessions)
        manager.register("slow", slow_job)
        with patch("app.core.jobs.settings") as cfg:
            cfg.job_max_runtime_seconds = 1
            cfg.job_watchdog_interval_seconds = 5  # floored to 5 in the loop
            cfg.job_log_ring = 50
            await manager.start()
            job_id = await manager.enqueue("slow")
            await asyncio.wait_for(cancelled.wait(), timeout=15)
            # cancelled.set() fires inside the job body before _execute has
            # persisted the terminal mark — poll generously for the commit.
            row = None
            for _ in range(200):
                await asyncio.sleep(0.05)
                async with sessions() as session:
                    row = await session.get(Job, job_id)
                if row.state == JobState.cancelled:
                    break
        await manager.stop()

        self.assertEqual(row.state, JobState.cancelled)
        self.assertIn("max runtime", (row.error or "") + (row.current_step or ""))
        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
