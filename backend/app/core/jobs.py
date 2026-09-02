"""JobManager: a single-worker async job queue with DB persistence and live
progress fan-out over the broadcaster.

- ``enqueue(kind, ...)`` inserts a ``jobs`` row (state=queued) and schedules it.
- one worker task drains the queue; it sets ``running``, runs the job's
  coroutine, then a terminal state (``succeeded`` / ``failed`` / ``cancelled``).
- the job body reports progress through ``JobContext`` (pct, step, log line);
  the log tail is a ~200-line ring buffer persisted on the row.
 - on startup, interrupted ``running`` jobs are marked failed; registered queued
   jobs are restored from their persisted parameters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .events import CH_JOBS, broadcaster, job_channel

logger = logging.getLogger("reforger.jobs")

JobFactory = Callable[["JobContext"], Awaitable[Any]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobContext:
    """Handed to a job body so it can report progress."""

    def __init__(self, manager: "JobManager", job_id: int) -> None:
        self._manager = manager
        self.job_id = job_id

    async def update(
        self,
        *,
        pct: float | None = None,
        step: str | None = None,
        log_line: str | None = None,
    ) -> None:
        await self._manager._update(self.job_id, pct=pct, step=step, log_line=log_line)

    async def log(self, line: str) -> None:
        await self.update(log_line=line)

    async def progress(self, pct: float, step: str | None = None) -> None:
        await self.update(pct=pct, step=step)

    @property
    def cancelled(self) -> bool:
        return self.job_id in self._manager._cancel_requested


class JobManager:
    def __init__(self, session_factory=SessionLocal) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._factories: dict[str, JobFactory] = {}
        self._pending: dict[int, JobFactory] = {}
        self._logs: dict[int, deque] = {}
        self._last_flush: dict[int, float] = {}
        self._worker: asyncio.Task | None = None
        self._current_job_id: int | None = None
        self._current_task: asyncio.Task | None = None
        self._cancel_requested: set[int] = set()

    # ---------------------------------------------------------------- lifecycle
    def register(self, kind: str, factory: JobFactory) -> None:
        self._factories[kind] = factory

    async def start(self) -> None:
        await self._fail_orphans()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="job-worker")

    async def stop(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _fail_orphans(self) -> None:
        from ..models import Job, JobState

        recover: list[int] = []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Job).where(
                        Job.state.in_([JobState.running, JobState.queued])
                    )
                )
            ).scalars().all()
            for job in rows:
                factory = self._factories.get(job.kind)
                if job.state == JobState.queued and factory is not None:
                    self._pending[job.id] = factory
                    self._logs[job.id] = deque(job.log_tail or [], maxlen=settings.job_log_ring)
                    recover.append(job.id)
                    continue
                job.state = JobState.failed
                job.error = "interrupted by a backend restart"
                job.finished_at = _utcnow()
            if rows:
                await session.commit()
                logger.info(
                    "recovered %d queued and marked %d orphaned job(s) failed on startup",
                    len(recover), len(rows) - len(recover),
                )
        for job_id in recover:
            await self._queue.put(job_id)

    # ------------------------------------------------------------------ enqueue
    async def enqueue(
        self,
        kind: str,
        *,
        params: dict | None = None,
        factory: JobFactory | None = None,
    ) -> int:
        from ..models import Job, JobState

        factory = factory or self._factories.get(kind)
        if factory is None:
            raise KeyError(f"no job factory registered for kind {kind!r}")

        async with self._session_factory() as session:
            job = Job(kind=kind, state=JobState.queued, params=params, log_tail=[])
            session.add(job)
            await session.commit()
            await session.refresh(job)
            job_id = job.id
            snap = self._snapshot(job)

        self._pending[job_id] = factory
        self._logs[job_id] = deque(maxlen=settings.job_log_ring)
        await self._publish(job_id, snap)
        await self._queue.put(job_id)
        return job_id

    async def request_cancel(self, job_id: int) -> bool:
        """Best-effort cancel. Returns True if the job was running or queued."""
        from ..models import JobState

        if job_id == self._current_job_id:
            self._cancel_requested.add(job_id)
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            return True
        if job_id in self._pending:
            self._pending.pop(job_id, None)
            await self._mark(
                job_id,
                state=JobState.cancelled,
                finished_at=_utcnow(),
                current_step="cancelled before start",
            )
            return True
        return False

    # ------------------------------------------------------------------- worker
    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("dispatcher error on job %s", job_id)
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: int) -> None:
        from ..models import JobState

        factory = self._pending.pop(job_id, None)
        if factory is None:
            return

        self._current_job_id = job_id
        ctx = JobContext(self, job_id)
        await self._mark(
            job_id,
            state=JobState.running,
            started_at=_utcnow(),
            current_step="starting",
        )

        task = asyncio.create_task(factory(ctx))
        self._current_task = task
        try:
            result = await task
        except asyncio.CancelledError:
            await self._mark(
                job_id,
                state=JobState.cancelled,
                finished_at=_utcnow(),
                current_step="cancelled",
            )
        except Exception as exc:
            logger.exception("job %s (%s) failed", job_id, factory)
            await self._mark(
                job_id,
                state=JobState.failed,
                finished_at=_utcnow(),
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if job_id in self._cancel_requested:
                await self._mark(
                    job_id, state=JobState.cancelled, finished_at=_utcnow()
                )
            else:
                await self._mark(
                    job_id,
                    state=JobState.succeeded,
                    progress=100.0,
                    finished_at=_utcnow(),
                    current_step="done",
                    result=self._coerce_result(result),
                )
        finally:
            self._current_job_id = None
            self._current_task = None
            self._cancel_requested.discard(job_id)
            self._last_flush.pop(job_id, None)

    # ------------------------------------------------------------- persistence
    async def _update(
        self,
        job_id: int,
        *,
        pct: float | None = None,
        step: str | None = None,
        log_line: str | None = None,
    ) -> None:
        from ..models import Job

        ring = self._logs.setdefault(job_id, deque(maxlen=settings.job_log_ring))
        if log_line is not None:
            for line in (str(log_line).splitlines() or [""]):
                ring.append(line)

        only_log = pct is None and step is None
        now_t = time.monotonic()
        flush_db = (not only_log) or (
            now_t - self._last_flush.get(job_id, 0.0) > 1.0
        )

        snap: dict | None = None
        if flush_db:
            async with self._session_factory() as session:
                job = await session.get(Job, job_id)
                if job is None:
                    return
                if pct is not None:
                    job.progress = max(0.0, min(100.0, float(pct)))
                if step is not None:
                    job.current_step = step[:255]
                job.log_tail = list(ring)
                await session.commit()
                snap = self._snapshot(job)
            self._last_flush[job_id] = now_t

        if snap is None:
            snap = {
                "id": job_id,
                "type": "log",
                "log_tail": list(ring),
            }
        if log_line is not None:
            snap["last_line"] = ring[-1] if ring else ""
        await self._publish(job_id, snap)

    async def _mark(self, job_id: int, **fields: Any) -> None:
        from ..models import Job

        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            if job_id in self._logs:
                job.log_tail = list(self._logs[job_id])
            await session.commit()
            snap = self._snapshot(job)
        await self._publish(job_id, snap)

    # --------------------------------------------------------------- utilities
    @staticmethod
    def _coerce_result(result: Any) -> dict | None:
        if result is None:
            return None
        if isinstance(result, dict):
            return result
        return {"value": result}

    @staticmethod
    def _snapshot(job) -> dict:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        state = job.state.value if hasattr(job.state, "value") else job.state
        return {
            "id": job.id,
            "type": "job",
            "kind": job.kind,
            "state": state,
            "progress": job.progress,
            "current_step": job.current_step,
            "error": job.error,
            "result": job.result,
            "log_tail": job.log_tail or [],
            "started_at": iso(job.started_at),
            "finished_at": iso(job.finished_at),
            "created_at": iso(getattr(job, "created_at", None)),
            "updated_at": iso(getattr(job, "updated_at", None)),
        }

    async def _publish(self, job_id: int, snap: dict) -> None:
        await broadcaster.publish(CH_JOBS, snap)
        await broadcaster.publish(job_channel(job_id), snap)


# Process-wide singleton.
job_manager = JobManager()
