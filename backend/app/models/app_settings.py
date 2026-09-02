"""Runtime-mutable settings singleton (id = 1): nightly-check toggle, log spam
patterns. Absent rows are lazily seeded from env defaults by
:mod:`app.core.app_settings`; the bootstrap schema ships an empty table.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, JSONVariant
from .base import TimestampMixin

APP_SETTINGS_SINGLETON_ID = 1


class AppSettings(Base, TimestampMixin):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=APP_SETTINGS_SINGLETON_ID)

    nightly_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    nightly_check_hour: Mapped[int] = mapped_column(Integer, default=3)
    # Lowercase substring patterns matched against log lines by logview.
    log_spam_patterns: Mapped[list | None] = mapped_column(JSONVariant)
