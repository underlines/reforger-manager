"""The ``engine`` singleton (id = 1): which Reforger build are we on, and is a
newer public-branch build available.

Detection keys on the numeric Steam ``buildid`` (string-compared), never the
display version. ``installed_*`` come from the local ``appmanifest_<appid>.acf``;
``latest_*`` from ``api.steamcmd.net`` (steamcmd ``app_info_print`` fallback).
The display versions are best-effort strings only.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base
from .base import TimestampMixin

ENGINE_SINGLETON_ID = 1


class Engine(Base, TimestampMixin):
    __tablename__ = "engine"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=ENGINE_SINGLETON_ID)

    installed_build: Mapped[str | None] = mapped_column(String(32))
    installed_version: Mapped[str | None] = mapped_column(String(64))
    target_build: Mapped[str | None] = mapped_column(String(32))
    beta_key: Mapped[str | None] = mapped_column(String(64))

    latest_build: Mapped[str | None] = mapped_column(String(32))
    latest_version: Mapped[str | None] = mapped_column(String(64))
    latest_time_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    update_available: Mapped[bool] = mapped_column(Boolean, default=False)

    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
