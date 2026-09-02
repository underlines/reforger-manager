"""GET /api/jobs            -> recent jobs
GET /api/jobs/{id}       -> one job
WS  /api/jobs/stream     -> live progress for every job (token in ?token=)
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from ..core.db import SessionLocal, get_session
from ..core.events import CH_JOBS, broadcaster, job_channel
from ..core.security import get_current_user
from ..models import Job
from ..schemas.job import JobOut
from .ws import ws_authenticate

router = APIRouter(prefix="/jobs", tags=["jobs"])


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


@router.get("/{job_id}", response_model=JobOut, dependencies=[Depends(get_current_user)])
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


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
