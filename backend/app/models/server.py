"""Server definitions, their mod lists, and config revisions.

Many definitions can be stored; the supervisor enforces that only one runs at a
time. ``config`` holds the last generated Reforger config.json snapshot (JSONB);
``server_config_revisions`` keeps versioned snapshots for diff / rollback.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, JSONVariant
from .base import TimestampMixin, _utcnow


class Server(Base, TimestampMixin):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    is_favourite: Mapped[bool] = mapped_column(Boolean, default=False)

    # Reforger game settings (also folded into the generated config).
    scenario_game_id: Mapped[str | None] = mapped_column(String(255))
    game_name: Mapped[str | None] = mapped_column(String(255))
    game_password: Mapped[str | None] = mapped_column(String(128))
    admin_password: Mapped[str | None] = mapped_column(String(128))
    max_players: Mapped[int] = mapped_column(Integer, default=32)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    game_properties: Mapped[dict | None] = mapped_column(JSONVariant)
    # Verbatim keys merged last into the generated config (escape hatch).
    extra_config: Mapped[dict | None] = mapped_column(JSONVariant)

    # Networking.
    bind_address: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    bind_port: Mapped[int] = mapped_column(Integer, default=2001)
    public_address: Mapped[str | None] = mapped_column(String(128))
    public_port: Mapped[int] = mapped_column(Integer, default=2001)
    a2s_address: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    a2s_port: Mapped[int] = mapped_column(Integer, default=17777)

    # RCON (native BattlEye block in config.json).
    rcon_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rcon_address: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    rcon_port: Mapped[int] = mapped_column(Integer, default=19999)
    rcon_password: Mapped[str | None] = mapped_column(String(128))
    rcon_permission: Mapped[str] = mapped_column(String(16), default="admin")
    rcon_max_clients: Mapped[int] = mapped_column(Integer, default=16)

    # Last generated config.json + revision pointer.
    config: Mapped[dict | None] = mapped_column(JSONVariant)
    config_revision: Mapped[int] = mapped_column(Integer, default=0)

    # Runtime state (single-server rule is also enforced against is_running).
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    last_state: Mapped[str | None] = mapped_column(String(32))  # stopped|running|crashed|exited
    last_exit_code: Mapped[int | None] = mapped_column(Integer)
    last_diagnosis: Mapped[dict | None] = mapped_column(JSONVariant)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mods: Mapped[list["ServerMod"]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        order_by="ServerMod.load_order",
    )
    revisions: Mapped[list["ServerConfigRevision"]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        order_by="ServerConfigRevision.revision",
    )


class ServerMod(Base):
    __tablename__ = "server_mods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    mod_guid: Mapped[str] = mapped_column(String(32), index=True)
    mod_name: Mapped[str | None] = mapped_column(String(255))
    load_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-server pin. Overrides the library-wide pin on models.mod.Mod.
    pinned_version: Mapped[str | None] = mapped_column(String(64))
    pinned_at_build: Mapped[str | None] = mapped_column(String(32))
    pinned_reason: Mapped[str | None] = mapped_column(Text)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("server_id", "mod_guid", name="uq_server_mod"),
    )

    server: Mapped["Server"] = relationship(back_populates="mods")


class ServerConfigRevision(Base):
    __tablename__ = "server_config_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("server_id", "revision", name="uq_server_config_revision"),
    )

    server: Mapped["Server"] = relationship(back_populates="revisions")
