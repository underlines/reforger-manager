"""GET  /api/jobs                 -> recent jobs
GET  /api/jobs/{id}            -> one job
POST /api/jobs/prune          -> bulk-delete finished (terminal) job rows
POST /api/jobs/{id}/cancel    -> request cancel of a running / queued job
DELETE /api/jobs/{id}         -> delete one finished job row
WS   /api/jobs/stream         -> live progress for every job (token in ?token=)
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from ..core.db import SessionLocal, get_session
from ..core.events import CH_JOBS, broadcaster, job_channel
from ..core.jobs import job_manager
from ..core.security import get_current_user
from ..models import TERMINAL_JOB_STATES, Job, JobState
from ..schemas.job import JobCancelOut, JobOut, JobPruneIn, JobPruneOut
from .ws import ws_authenticate

router = APIRouter(prefix="/jobs", tags=["jobs"])
authed = [Depends(get_current_user)]


@router.get("", response_model=list[JobOut], dependencies=[Depends(get_current_user)])
async def list_jobs(
    limit: int = Query(50, ge=1, le=500),
    state: str | None = None,
    kind: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[Job]:
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if state:
        stmt = stmt.where(Job.state == state)
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return list((await session.execute(stmt)).scalars().all())


@router.post("/prune", response_model=JobPruneOut, dependencies=authed)
async def prune_jobs(
    body: JobPruneIn | None = None,
    session: AsyncSession = Depends(get_session),
) -> JobPruneOut:
    """Delete finished job rows. Defaults to every terminal state; an explicit
    ``states`` list is intersected with the terminal set (a non-terminal state
    is a 400)."""
    wanted = set(TERMINAL_JOB_STATES)
    if body and body.states is not None:
        try:
            requested = {JobState(value) for value in body.states}
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown job state: {exc}")
        illegal = requested - set(TERMINAL_JOB_STATES)
        if illegal:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"cannot prune non-terminal state(s): {sorted(s.value for s in illegal)}",
            )
        wanted = requested
    result = await session.execute(delete(Job).where(Job.state.in_(wanted)))
    await session.commit()
    return JobPruneOut(deleted=result.rowcount or 0)


@router.get("/{job_id}", response_model=JobOut, dependencies=[Depends(get_current_user)])
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobCancelOut, dependencies=authed)
async def cancel_job(
    job_id: int, session: AsyncSession = Depends(get_session)
) -> JobCancelOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.state in TERMINAL_JOB_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"job already {job.state.value}"
        )
    reason = "cancelled by user"
    tracked = await job_manager.request_cancel(job_id, reason=reason)
    if not tracked:
        # Row says running/queued but the manager no longer tracks it (e.g. it
        # was orphaned by a restart and not recovered). Mark it terminal here.
        job.state = JobState.cancelled
        job.finished_at = datetime.now(timezone.utc)
        job.current_step = reason
        job.error = job.error or "cancelled while untracked by the job manager"
        await session.commit()
    return JobCancelOut(job_id=job_id, cancelled=True)


@router.delete(
    "/{job_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=authed
)
async def delete_job(job_id: int, session: AsyncSession = Depends(get_session)) -> None:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.state not in TERMINAL_JOB_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "job is still queued or running — cancel it before deleting",
        )
    await session.delete(job)
    await session.commit()


@router.websocket("/stream")
async def jobs_stream(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    if not await ws_authenticate(websocket, token):
        return
    await websocket.accept()

    # Replay current in-flight / recent jobs once on connect.
    async with SessionLocal() as session:
        recent = list(
            (
                await session.execute(select(Job).order_by(Job.id.desc()).limit(20))
            ).scalars().all()
        )
    def _iso(value):
        return value.isoformat() if value else None

    for job in reversed(recent):
        await websocket.send_json(
            {
                "id": job.id,
                "type": "job",
                "kind": job.kind,
                "state": job.state.value if hasattr(job.state, "value") else job.state,
                "progress": job.progress,
                "current_step": job.current_step,
                "error": job.error,
                "log_tail": job.log_tail or [],
                "started_at": _iso(job.started_at),
                "finished_at": _iso(job.finished_at),
                "created_at": _iso(getattr(job, "created_at", None)),
                "updated_at": _iso(getattr(job, "updated_at", None)),
            }
        )

    async with broadcaster.subscribe(CH_JOBS) as queue:
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:
            with contextlib.suppress(Exception):
                await websocket.close()
            return


@router.websocket("/{job_id}/stream")
async def single_job_stream(
    websocket: WebSocket, job_id: int, token: str | None = Query(default=None)
) -> None:
    if not await ws_authenticate(websocket, token):
        return
    await websocket.accept()
    async with broadcaster.subscribe(job_channel(job_id)) as queue:
        try:
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
