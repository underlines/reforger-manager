"""Storage view schemas (S12): on-disk sizes, free space, orphan lists."""

from __future__ import annotations

from pydantic import BaseModel


class StoragePerMod(BaseModel):
    guid: str
    name: str | None = None
    bytes: int


class StorageOrphan(BaseModel):
    guid: str
    name: str | None = None
    bytes: int


class StorageKeptDependency(BaseModel):
    guid: str
    name: str | None = None
    bytes: int
    required_by: list[str]


class StorageUnreferencedEntry(BaseModel):
    """A library row with no on-disk files and no references — safe to delete
    from the library entirely (``DELETE /api/mods/{guid}``)."""

    guid: str
    name: str | None = None


class StorageOut(BaseModel):
    mods_path: str
    free_bytes: int
    total_bytes: int
    per_mod: list[StoragePerMod]
    orphans: list[StorageOrphan]
    kept_as_dependency: list[StorageKeptDependency]
    unreferenced_entries: list[StorageUnreferencedEntry] = []