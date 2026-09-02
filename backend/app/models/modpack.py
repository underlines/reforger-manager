"""Modpacks: named, reusable, load-ordered mod lists."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.db import Base
from .base import TimestampMixin


class Modpack(Base, TimestampMixin):
    __tablename__ = "modpacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["ModpackItem"]] = relationship(
        back_populates="modpack",
        cascade="all, delete-orphan",
        order_by="ModpackItem.load_order",
    )


class ModpackItem(Base):
    __tablename__ = "modpack_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    modpack_id: Mapped[int] = mapped_column(
        ForeignKey("modpacks.id", ondelete="CASCADE"), index=True
    )
    mod_guid: Mapped[str] = mapped_column(String(32), index=True)
    load_order: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("modpack_id", "mod_guid", name="uq_modpack_item"),
    )

    modpack: Mapped["Modpack"] = relationship(back_populates="items")
