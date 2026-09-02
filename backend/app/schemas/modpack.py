"""Modpack request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class ModpackItemIn(BaseModel):
    mod_guid: str
    load_order: int = 0


def _reject_duplicate_guids(items: list[ModpackItemIn]) -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for item in items:
        if item.mod_guid in seen and item.mod_guid not in dupes:
            dupes.append(item.mod_guid)
        seen.add(item.mod_guid)
    if dupes:
        raise ValueError(f"duplicate mod_guid in items: {', '.join(dupes)}")


class ModpackCreate(BaseModel):
    name: str
    description: str | None = None
    items: list[ModpackItemIn] = []

    @model_validator(mode="after")
    def _no_duplicate_items(self) -> "ModpackCreate":
        _reject_duplicate_guids(self.items)
        return self


class ModpackUpdate(BaseModel):
    """All fields optional (PATCH semantics); `items` replaces the pack's item set."""

    name: str | None = None
    description: str | None = None
    items: list[ModpackItemIn] | None = None

    @model_validator(mode="after")
    def _no_duplicate_items(self) -> "ModpackUpdate":
        if self.items is not None:
            _reject_duplicate_guids(self.items)
        return self


class ModpackItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mod_guid: str
    load_order: int
    mod_name: str | None = None


class ModpackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    items: list[ModpackItemOut] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --------------------------------------------------------------------- transfer


class ModpackApplyIn(BaseModel):
    mode: Literal["replace", "append"] = "replace"


class DroppedPin(BaseModel):
    mod_guid: str
    mod_name: str | None = None


class ModpackApplyOut(BaseModel):
    applied: int
    mode: str
    dropped_pins: list[DroppedPin] = []


class ModpackFromServerIn(BaseModel):
    name: str
    description: str | None = None


class ModpackFromServerOut(ModpackOut):
    """A freshly created pack plus a note when the source server had pins.

    ``pins_note`` is non-null only when the source definition carried at least
    one pinned mod; pins are never snapshotted into a pack (a pack is a mod
    list, not a version lock).
    """

    pins_note: str | None = None


class ModpackTransfer(BaseModel):
    """Portable pack document for GET .../export and POST /import (no ids/timestamps)."""

    name: str
    description: str | None = None
    items: list[ModpackItemIn] = []

    @model_validator(mode="after")
    def _no_duplicate_items(self) -> "ModpackTransfer":
        _reject_duplicate_guids(self.items)
        return self
