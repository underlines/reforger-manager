"""Background jobs (steamcmd install/update, engine update, verify/repair, ...)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, JSONVariant
from .base import JobState, TimestampMixin


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[JobState] = mapped_column(
        SAEnum(JobState, native_enum=False, length=16),
        default=JobState.queued,
        nullable=False,
        index=True,
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_step: Mapped[str | None] = mapped_column(String(255))

    params: Mapped[dict | None] = mapped_column(JSONVariant)
    result: Mapped[dict | None] = mapped_column(JSONVariant)
    error: Mapped[str | None] = mapped_column(Text)
    # Ring buffer of recent output lines (see core.jobs.JobManager).
    log_tail: Mapped[list | None] = mapped_column(JSONVariant)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
