from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    game_id: str
    name: str | None = None
    game_mode: str | None = None
    player_count: int | None = None


class ModDependencyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    depends_on_guid: str
    depends_on_name: str | None = None
    required_version: str | None = None
    source: str | None = None


class ModRefOut(BaseModel):
    """A lightweight reference to another library mod (used by ``required_by``)."""

    guid: str
    name: str | None = None


class ModOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    guid: str
    name: str | None = None
    summary: str | None = None
    installed_version: str | None = None
    latest_version: str | None = None
    latest_game_version: str | None = None
    size: int | None = None
    thumbnail: str | None = None
    tags: list | None = None
    last_checked: datetime | None = None

    is_unlisted: bool = False
    is_private: bool = False
    is_obsolete: bool = False
    is_local: bool = False

    api_state: str = "unchecked"
    api_checked_at: datetime | None = None

    pinned_version: str | None = None
    pinned_at_build: str | None = None
    pinned_reason: str | None = None
    pinned_at: datetime | None = None

    # computed
    has_update: bool = False
    stale_pin: bool = False
    # Library mods that declare this mod as a dependency. Non-empty => deleting
    # this mod / its files is refused while those mods are assigned or packed.
    required_by: list[ModRefOut] = []

    created_at: datetime | None = None
    updated_at: datetime | None = None


class ResolvedNodeOut(BaseModel):
    guid: str
    name: str | None = None
    via: str
    state: str
    depth: int


class ResolvedEdgeOut(BaseModel):
    from_: str
    to: str


class ResolvedTreeOut(BaseModel):
    roots: list[str]
    nodes: list[ResolvedNodeOut]
    edges: list[dict]


class ModDetailOut(ModOut):
    scenarios: list[ModScenarioOut] = []
    dependencies: list[ModDependencyOut] = []
    dependency_tree: ResolvedTreeOut | None = None
    versions: list[dict] = []
    used_by: list[str] = []


class ModAddIn(BaseModel):
    url_or_id: str


class ModPinIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=4096)


class ModVerifyIn(BaseModel):
    guids: list[str] | None = None


class ModDownloadIn(BaseModel):
    version: str | None = Field(default=None, min_length=1, max_length=64)


class ModSearchResult(BaseModel):
    id: str
    name: str | None = None
    summary: str | None = None
    latest_version: str | None = None
    workshop_url: str | None = None
