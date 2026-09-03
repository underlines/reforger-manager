from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    state: str
    progress: float
    current_step: str | None = None
    params: dict | None = None
    result: dict | None = None
    error: str | None = None
    log_tail: list | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobEnqueuedOut(BaseModel):
    job_id: int
    kind: str


class JobCancelOut(BaseModel):
    job_id: int
    cancelled: bool


class JobPruneIn(BaseModel):
    # Restricted to terminal states server-side; ``None`` means all of them.
    states: list[str] | None = None


class JobPruneOut(BaseModel):
    deleted: int
