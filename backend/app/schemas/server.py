from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServerModIn(BaseModel):
    mod_guid: str
    mod_name: str | None = None
    load_order: int = 0
    enabled: bool = True
    pinned_version: str | None = None
    pinned_at_build: str | None = None
    pinned_reason: str | None = None


class ServerModOut(ServerModIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pinned_at: datetime | None = None


class ServerBase(BaseModel):
    name: str
    is_favourite: bool = False
    scenario_game_id: str | None = None
    game_name: str | None = None
    game_password: str | None = None
    admin_password: str | None = None
    max_players: int = 32
    visible: bool = True
    game_properties: dict | None = None
    extra_config: dict | None = None

    bind_address: str = "0.0.0.0"
    bind_port: int = 2001
    public_address: str | None = None
    public_port: int = 2001
    a2s_address: str = "0.0.0.0"
    a2s_port: int = 17777

    rcon_enabled: bool = True
    rcon_address: str = "0.0.0.0"
    rcon_port: int = 19999
    rcon_password: str | None = None
    rcon_permission: str = "admin"
    rcon_max_clients: int = Field(default=16, ge=1, le=16)


class ServerCreate(ServerBase):
    mods: list[ServerModIn] = []


class ServerUpdate(BaseModel):
    """All fields optional (PATCH semantics)."""

    name: str | None = None
    is_favourite: bool | None = None
    scenario_game_id: str | None = None
    game_name: str | None = None
    game_password: str | None = None
    admin_password: str | None = None
    max_players: int | None = None
    visible: bool | None = None
    game_properties: dict | None = None
    extra_config: dict | None = None
    bind_address: str | None = None
    bind_port: int | None = None
    public_address: str | None = None
    public_port: int | None = None
    a2s_address: str | None = None
    a2s_port: int | None = None
    rcon_enabled: bool | None = None
    rcon_address: str | None = None
    rcon_port: int | None = None
    rcon_password: str | None = None
    rcon_permission: str | None = None
    rcon_max_clients: int | None = Field(default=None, ge=1, le=16)
    mods: list[ServerModIn] | None = None


class ServerOut(ServerBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    config_revision: int
    is_running: bool
    pid: int | None = None
    last_state: str | None = None
    last_exit_code: int | None = None
    last_diagnosis: dict | None = None
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    mods: list[ServerModOut] = []


class ServerCloneIn(BaseModel):
    """Body of `POST /api/servers/{id}/clone`: only the new definition's name.

    Everything else is deep-copied from the source row server-side.
    """

    name: str = Field(min_length=1, max_length=255)


class ServerConfigOut(BaseModel):
    server_id: int
    config: dict
    path: str


class ServerModPinIn(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=4096)


class RconCommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=4096)


class ScheduleRestartIn(BaseModel):
    """Arm a warned restart: `#say` at each seconds-before-restart offset,
    then `#restart` at T-0."""

    in_seconds: int = Field(gt=0, le=7 * 24 * 3600)
    warn_at: list[int] = Field(default_factory=lambda: [300, 60, 10])
