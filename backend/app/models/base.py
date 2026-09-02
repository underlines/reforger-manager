"""Shared model bits: the timestamp mixin and enums."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    # Both a Python-side default/onupdate (so the ORM object is populated without
    # a post-commit lazy reload, which async sessions cannot do) and a SQL
    # server_default (so raw inserts still get a value).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
        nullable=False,
    )


class ApiState(str, enum.Enum):
    """Whether the unofficial Workshop API resolves a mod.

    A deleted, blocked or private mod all return a bare HTTP 404 with no reason
    code, so we record a single honest ``not_found`` rather than guessing which.
    """

    ok = "ok"
    not_found = "not_found"
    unchecked = "unchecked"


class JobState(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_JOB_STATES = {JobState.succeeded, JobState.failed, JobState.cancelled}
