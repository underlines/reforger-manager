from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class FileEntryOut(BaseModel):
    name: str
    rel_path: str
    type: Literal["file", "dir", "symlink"]
    size: int
    mtime: datetime | None
    is_text: bool
    is_json: bool


class DirListingOut(BaseModel):
    server_id: int
    path: str
    entries: list[FileEntryOut]


class FileContentOut(BaseModel):
    path: str
    size: int
    editable: bool
    is_json: bool
    content: str | None = None


class WriteContentIn(BaseModel):
    content: str


class MkdirIn(BaseModel):
    path: str


class RenameIn(BaseModel):
    path: str
    new_path: str