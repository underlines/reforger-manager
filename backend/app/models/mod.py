"""Mod library tables.

Population logic (disk scanner + Workshop API enrichment) is Phase 3; this is
the schema of record. Columns mirror PLAN.md "Data model (core tables)".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base, JSONVariant
from .base import ApiState, TimestampMixin


class Mod(Base, TimestampMixin):
    __tablename__ = "mods"

    guid: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)

    installed_version: Mapped[str | None] = mapped_column(String(64))
    latest_version: Mapped[str | None] = mapped_column(String(64))
    # gameVersion the latest Workshop release declares (from API /versions only;
    # local meta always reports "" for this).
    latest_game_version: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int | None] = mapped_column(BigInteger)
    thumbnail: Mapped[str | None] = mapped_column(String(512))
    tags: Mapped[list | None] = mapped_column(JSONVariant)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Flags the API actually returns.
    is_unlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    is_obsolete: Mapped[bool] = mapped_column(Boolean, default=False)

    api_state: Mapped[ApiState] = mapped_column(
        SAEnum(ApiState, native_enum=False, length=16),
        default=ApiState.unchecked,
        nullable=False,
    )
    api_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Library-wide pin. Records which engine build the version was known good
    # against, so pre-flight can call it stale once the engine moves past it.
    pinned_version: Mapped[str | None] = mapped_column(String(64))
    pinned_at_build: Mapped[str | None] = mapped_column(String(32))
    pinned_reason: Mapped[str | None] = mapped_column(Text)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Whether a dir for this mod exists on disk under MODS_DIR/reforger/addons.
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)

    dependencies: Mapped[list["ModDependency"]] = relationship(
        back_populates="mod",
        cascade="all, delete-orphan",
        foreign_keys="ModDependency.mod_guid",
    )
    scenarios: Mapped[list["ModScenario"]] = relationship(
        back_populates="mod", cascade="all, delete-orphan"
    )


class ModDependency(Base):
    __tablename__ = "mod_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_guid: Mapped[str] = mapped_column(
        ForeignKey("mods.guid", ondelete="CASCADE"), index=True
    )
    # Not an FK: a dependency GUID can have neither a local dir nor an API record
    # (an unresolved dependency, not a parser error).
    depends_on_guid: Mapped[str] = mapped_column(String(32), index=True)
    depends_on_name: Mapped[str | None] = mapped_column(String(255))
    required_version: Mapped[str | None] = mapped_column(String(64))
    # "api" (mod resolves) or "gproj" (local addon.gproj fallback for a 404'd parent).
    source: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        UniqueConstraint("mod_guid", "depends_on_guid", name="uq_mod_dependency"),
    )

    mod: Mapped["Mod"] = relationship(
        back_populates="dependencies", foreign_keys=[mod_guid]
    )


class ModScenario(Base):
    __tablename__ = "mod_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mod_guid: Mapped[str] = mapped_column(
        ForeignKey("mods.guid", ondelete="CASCADE"), index=True
    )
    # e.g. "{806FFA8093F22A71}Missions/REAPER_Kingmaker.conf"
    game_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    # camelCase gameMode / playerCount from the API mapped to snake_case here.
    game_mode: Mapped[str | None] = mapped_column(String(64))
    player_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("mod_guid", "game_id", name="uq_mod_scenario"),
    )

    mod: Mapped["Mod"] = relationship(back_populates="scenarios")
