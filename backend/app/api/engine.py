"""Engine (Reforger server binary) build status + update.

GET  /api/engine         -> singleton status
POST /api/engine/check   -> refresh installed/latest build now (cheap, sync)
POST /api/engine/update  -> enqueue a steamcmd update job
                            (refused 409 while any server is running)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.jobs import job_manager
from ..core.security import get_current_user
from ..models import ENGINE_SINGLETON_ID, Engine, Server
from ..schemas.engine import EngineOut
from ..schemas.job import JobEnqueuedOut
from ..servers.supervisor import supervisor
from ..steam.engine import get_or_create_engine, refresh_engine

router = APIRouter(prefix="/engine", tags=["engine"], dependencies=[Depends(get_current_user)])

JOB_KIND_ENGINE_UPDATE = "engine_update"


@router.get("", response_model=EngineOut)
async def get_engine(session: AsyncSession = Depends(get_session)) -> Engine:
    row = await session.get(Engine, ENGINE_SINGLETON_ID)
    if row is None:
        row = await get_or_create_engine(session)
        await session.commit()
    return row


@router.post("/check", response_model=EngineOut)
async def check_engine(session: AsyncSession = Depends(get_session)) -> Engine:
    return await refresh_engine(session)


async def _running_server_id(session: AsyncSession) -> int | None:
    if supervisor.is_running():
        return supervisor.active_server_id
    return (
        await session.execute(select(Server.id).where(Server.is_running.is_(True)))
    ).scalars().first()


@router.post("/update", response_model=JobEnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def update_engine(session: AsyncSession = Depends(get_session)) -> JobEnqueuedOut:
    running = await _running_server_id(session)
    if running is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"server {running} is running; update after it stops",
        )
    job_id = await job_manager.enqueue(JOB_KIND_ENGINE_UPDATE)
    return JobEnqueuedOut(job_id=job_id, kind=JOB_KIND_ENGINE_UPDATE)
