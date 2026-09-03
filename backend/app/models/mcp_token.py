"""MCP administration tokens (sprint 4): clients send
``Authorization: Bearer rfm_<token>`` to ``/mcp``. Only the sha256 hex digest is
stored, so the raw token is unrecoverable — it is shown exactly once in the 201
create response. Single-admin model: no user column (PLAN non-goal).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base
from .base import TimestampMixin


class McpToken(Base, TimestampMixin):
    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(255))
    # sha256 hex of the raw ``rfm_...`` token; the plaintext is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
