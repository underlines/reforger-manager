"""Pydantic types for save-game discovery (``app.servers.saves``).

Read-only: these describe what :func:`app.servers.saves.discover` returns, no
request bodies live here (a later story adds the HTTP layer and its own
request/response wrappers).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ModDrift(BaseModel):
    """Mod-id-set delta between the revision active at save time and now.

    ``added``/``removed`` carry mod *names* (for display); the underlying
    comparison is by mod id, the stable key.
    """

    added: list[str] = []
    removed: list[str] = []


class SavePointOut(BaseModel):
    """One ``savepointNNN/`` directory."""

    dir_name: str
    rel_path: str  # relative to the server's profile root
    save_point_nr: int
    playthrough_nr: int
    readable: bool

    uuid: str | None = None
    saved_at: datetime | None = None
    playtime_seconds: int | None = None
    game_version: str | None = None
    mission_resource: str | None = None
    display_name: str | None = None
    size_bytes: int = 0

    matches_current_scenario: bool = False
    engine_drift: bool = False

    mod_drift: ModDrift | None = None
    mod_drift_unknown: bool = False


class PlaythroughOut(BaseModel):
    """One ``playthroughNNN/`` directory, grouping its save points."""

    dir_name: str
    playthrough_nr: int
    display_name: str | None = None
    started_at: datetime | None = None
    save_points: list[SavePointOut] = []


class ScenarioSaves(BaseModel):
    """One scenario directory under ``.save/game/``, grouping its playthroughs."""

    scenario_dir: str
    mission_resource: str | None = None
    matches_current_scenario: bool = False
    playthroughs: list[PlaythroughOut] = []


# --------------------------------------------------------------------- snapshots
# Manager-owned save snapshots (``app.servers.save_snapshots``). A snapshot is a
# tar.gz copy of one save-point directory plus a JSON manifest sidecar; these
# types mirror that manifest.


class SnapshotModOut(BaseModel):
    """One mod as recorded on the server at snapshot-creation time."""

    mod_id: str
    name: str | None = None
    version: str | None = None


class SnapshotOut(BaseModel):
    """A stored save snapshot (manifest fields + a couple of housekeeping ones)."""

    snapshot_id: str
    label: str
    created_at: datetime

    source_save_uuid: str | None = None
    playthrough_nr: int | None = None
    save_point_nr: int | None = None
    mission_resource: str | None = None
    game_version: str | None = None
    saved_at_unix: int | None = None
    playtime_seconds: int | None = None
    uncompressed_size_bytes: int | None = None
    scenario_game_id: str | None = None
    mods: list[SnapshotModOut] = []

    # Restore-location bookkeeping, captured at snapshot-creation time via
    # ``saves.discover()`` so ``restore_snapshot`` need not re-derive scenario
    # directory naming. Not guaranteed for a snapshot stored via
    # ``store_upload`` (an externally-created archive) — best-effort there,
    # ``None`` for what could not be inspected.
    scenario_dir: str | None = None
    playthrough_dir_name: str | None = None
    save_point_dir_name: str | None = None

    archive_size_bytes: int | None = None


class RestoreResult(BaseModel):
    """Outcome of ``restore_snapshot``."""

    snapshot_id: str
    restored_uuid: str | None = None
    target_rel_path: str
    armed: bool
    auto_backup_snapshot_id: str | None = None


# --------------------------------------------------------------------- HTTP layer
# Request/response wrappers for ``app.api.server_saves`` (S14).


class SelectionOut(BaseModel):
    """A server's current save-selection state (mirrors the ``Server`` columns)."""

    mode: str
    pinned_uuid: str | None = None
    sticky: bool


class SavesListOut(BaseModel):
    """``GET /servers/{id}/saves`` response: discovery + snapshots + selection."""

    scenarios: list[ScenarioSaves] = []
    snapshots: list[SnapshotOut] = []
    selection: SelectionOut


class ArmIn(BaseModel):
    """Body for ``POST /{save_uuid}/arm`` and ``POST /arm-fresh``."""

    sticky: bool = False


class LabelIn(BaseModel):
    """Body carrying a snapshot label — used for both create and rename."""

    label: str


class RestoreIn(BaseModel):
    """Body for ``POST /snapshots/{snapshot_id}/restore``."""

    arm: bool = True
