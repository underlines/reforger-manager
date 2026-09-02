"""Full-backup transfer schemas (S18, PLAN F29-F31).

One portable JSON document carrying every server *definition* (config fields +
its mod set, with per-server pins in **cleartext**) and every modpack. Runtime
state, the generated ``config`` blob, ``config_revision``, the ``engine`` row and
the disk-derived ``mods`` library are all excluded -- they are reconstructable
from a scan / a fresh engine install.

Servers and packs are keyed by ``name`` on import, so ``name`` is mandatory in
every entry. ``servers`` and ``modpacks`` are required arrays -- a document
missing either, or with the wrong types, fails schema validation (422).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# Every ``Server`` column that is part of the *definition* (folded into the
# generated config or otherwise operator-set). Excludes: id, all runtime columns
# (is_running, pid, last_state, last_exit_code, last_diagnosis, last_started_at,
# last_stopped_at), config, config_revision, created_at/updated_at.
SERVER_CONFIG_FIELDS: tuple[str, ...] = (
    "name",
    "is_favourite",
    "scenario_game_id",
    "game_name",
    "game_password",
    "admin_password",
    "max_players",
    "visible",
    "game_properties",
    "extra_config",
    "bind_address",
    "bind_port",
    "public_address",
    "public_port",
    "a2s_address",
    "a2s_port",
    "rcon_enabled",
    "rcon_address",
    "rcon_port",
    "rcon_password",
    "rcon_permission",
    "rcon_max_clients",
)


class BackupMod(BaseModel):
    """A single ``server_mods`` row, pins included."""

    model_config = ConfigDict(from_attributes=True)

    mod_guid: str
    mod_name: str | None = None
    load_order: int = 0
    enabled: bool = True
    pinned_version: str | None = None
    pinned_at: datetime | None = None
    pinned_at_build: str | None = None
    pinned_reason: str | None = None


class BackupServer(BaseModel):
    """A server definition: every config field + its mod set.

    The three ``*_password`` fields are carried in CLEARTEXT -- a restore has to
    reproduce a server the operator can actually administer.
    """

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
    rcon_max_clients: int = 16

    mods: list[BackupMod] = []


class BackupModpackItem(BaseModel):
    mod_guid: str
    load_order: int = 0


class BackupModpack(BaseModel):
    name: str
    description: str | None = None
    items: list[BackupModpackItem] = []


class BackupDocument(BaseModel):
    """The whole export / the accepted import body."""

    version: int = 1
    exported_at: datetime | None = None
    servers: list[BackupServer]
    modpacks: list[BackupModpack]


class BackupItemPlan(BaseModel):
    name: str
    action: str  # "create" | "replace" | "skip"
    note: str | None = None


class BackupPlan(BaseModel):
    """What an import would do / did, per name."""

    dry_run: bool
    on_conflict: str
    servers: list[BackupItemPlan] = []
    modpacks: list[BackupItemPlan] = []
