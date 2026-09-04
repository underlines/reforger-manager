"""Declarative tool table (sprint 4, S4 reads + S5 mutations): one ``ToolSpec``
per mirrored REST endpoint, registered on the ``MCPServer`` by
:func:`register_tools`.

The tool layer holds no business logic: each spec becomes a typed async function
whose arguments are (a) the path params found in ``spec.path`` and (b) the fields
of ``spec.arg_model`` — flattened into plain keyword arguments so the JSON input
schema stays flat and predictable for agents. On a GET those fields travel as
query params, on a POST as the JSON body. ``spec.query_model`` fields (for the
routes that take a query string beside a body) always travel as query params;
``spec.multipart_field`` names the arg-model field sent as multipart form data
(the upload route). The call itself is a plain ``api.call`` against the real
app; non-2xx responses are surfaced as a deterministic ``ToolError`` carrying
the HTTP status and the API's detail, so an agent can reason about 404/409/502
semantics instead of seeing opaque crashes.

Confirm-gated specs (``confirm=True``) get a trailing ``confirm: bool = False``
argument; unless it is true the tool errors with the same 400-style shape and
the API call is never made. Job-returning tools (202) return the API's
``JobEnqueuedOut`` verbatim and point at the ``wait_for_job(job_id)`` idiom in
their description.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from pydantic_core import to_json
from pydantic.fields import FieldInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..schemas.backup import BackupDocument
from ..schemas.job import JobPruneIn
from ..schemas.mod import ModAddIn, ModDownloadIn, ModVerifyIn
from ..schemas.modpack import (
    ModpackApplyIn,
    ModpackCreate,
    ModpackFromServerIn,
    ModpackUpdate,
)
from ..schemas.server import (
    RconCommandIn,
    ScheduleRestartIn,
    ServerCloneIn,
    ServerCreate,
    ServerModPinIn,
    ServerUpdate,
)
from ..schemas.server_files import MkdirIn, RenameIn, WriteContentIn
from ..schemas.settings import SettingsUpdate
from . import api


@dataclass(frozen=True)
class ToolSpec:
    """One mirrored REST endpoint.

    ``path`` uses ``{name}`` placeholders; every placeholder becomes a required
    typed tool argument. ``arg_model`` fields (if any) become additional
    arguments: query params on a GET, a JSON body on any other method.
    ``query_model`` fields always become query params — for the endpoints that
    take a query string beside a JSON body. ``multipart_field`` names the
    ``arg_model`` field whose ``{filename, content}`` items are sent as
    multipart form data instead of JSON (the profile-file upload). Specs with
    ``confirm=True`` get a trailing ``confirm: bool = False`` argument and
    refuse (400-style tool error) to reach the API without it.
    """

    name: str
    method: str
    path: str
    description: str
    arg_model: type[BaseModel] | None = None
    query_model: type[BaseModel] | None = None
    multipart_field: str | None = None
    confirm: bool = False
    timeout_s: float = 60.0


# --------------------------------------------------------------------------
# Query/body arg models (small, read-only; the PATCH-style draft body reuses
# app/schemas/server.py directly)
# --------------------------------------------------------------------------


class ServerConfigQuery(BaseModel):
    """Arguments of get_server_config."""

    write: bool = False


class ModListFilters(BaseModel):
    """Optional filters for list_mods; every field may be omitted."""

    local: bool | None = None
    q: str | None = None
    update: bool | None = None
    state: str | None = None


class WorkshopSearchQuery(BaseModel):
    """Arguments of search_workshop_mods."""

    q: str
    limit: int = 20


class JobListFilters(BaseModel):
    """Optional filters for list_jobs; every field may be omitted."""

    limit: int = 50
    state: str | None = None
    kind: str | None = None


class FileListQuery(BaseModel):
    """Arguments of list_server_files; omit path for the profile root."""

    path: str | None = None


class FileContentQuery(BaseModel):
    """Arguments of read_server_file."""

    path: str


class ScenarioGuids(BaseModel):
    """Arguments of resolve_scenarios."""

    guids: list[str] = []


# Mutation-side models: query params that travel beside a JSON/multipart body,
# plus the upload route's multipart payload. The bodies themselves reuse the
# API's own schemas (imported above) — 1:1 mirroring, no re-declaration.


class FilePathQuery(BaseModel):
    """The required relative-path query param of the file write/delete routes."""

    path: str


class UploadDirQuery(BaseModel):
    """Optional target subdirectory of the upload route (profile root if omitted)."""

    path: str | None = None


class UploadFileContent(BaseModel):
    """One uploaded file: its plain name (no path separators) and UTF-8 text."""

    filename: str
    content: str


class ServerFileUploads(BaseModel):
    """The multipart payload of upload_server_files."""

    files: list[UploadFileContent]


class BackupImportQuery(BaseModel):
    """Query params of the backup import; dry_run defaults to true."""

    dry_run: bool = True
    on_conflict: Literal["skip", "replace"] = "skip"


_PATH_PARAM_TYPES: dict[str, type] = {
    "server_id": int,
    "pack_id": int,
    "job_id": int,
}


# --------------------------------------------------------------------------
# The tool table. Names/paths/descriptions derive from the router modules'
# docstrings (PLAN fact #5): servers, jobs, engine, mods, modpacks,
# server_files, backup, settings, storage, scenarios. Read rows first, then
# the confirm-gated mutation rows (S5).
# --------------------------------------------------------------------------

TOOL_SPECS: tuple[ToolSpec, ...] = (
    # ------------------------------------------------------------- servers
    ToolSpec(
        name="list_servers",
        method="GET",
        path="/api/servers",
        description=(
            "List every stored server definition: id, name, favourite flag, "
            "network/binding settings, RCON settings and the full assigned mod "
            "set with load order and pins. Pure configuration — does not tell "
            "you which server is currently running; use the running server's "
            "live endpoints (stats/players) for runtime state."
        ),
    ),
    ToolSpec(
        name="get_server",
        method="GET",
        path="/api/servers/{server_id}",
        description=(
            "Read one server definition in full by id, including its complete "
            "mod set (load order, enabled flag, pinned versions and the reason "
            "each pin was taken). 404 if the id is unknown."
        ),
    ),
    ToolSpec(
        name="get_server_config",
        method="GET",
        path="/api/servers/{server_id}/config",
        description=(
            "Generate the exact config.json this server definition produces "
            "(mods resolved, passwords folded in) and return it together with "
            "the path the file would be written to. write=true additionally "
            "persists the file to disk; the default (false) is a pure preview. "
            "The real file is regenerated from this same code on every server "
            "start."
        ),
        arg_model=ServerConfigQuery,
    ),
    ToolSpec(
        name="preview_server_config",
        method="POST",
        path="/api/servers/{server_id}/config/preview",
        description=(
            "Dry-run config generation for an unsaved draft: apply a "
            "ServerUpdate-shaped patch (any subset of the server's settings, "
            "optionally a working-copy mods set) to a detached copy of the "
            "definition and return the config.json it would produce. Nothing "
            "is persisted. Use get_server_config to preview the saved state "
            "instead."
        ),
        arg_model=ServerUpdate,
    ),
    ToolSpec(
        name="get_server_preflight",
        method="GET",
        path="/api/servers/{server_id}/preflight",
        description=(
            "Run the start pre-flight checks for a server definition and "
            "return the structured report: engine installed?, port conflicts, "
            "config problems, missing mods. Call this before starting a "
            "server; the start tool re-runs it anyway."
        ),
    ),
    ToolSpec(
        name="get_server_stats",
        method="GET",
        path="/api/servers/{server_id}/stats",
        description=(
            "Live session stats for a server (player count, FPS, uptime) "
            "queried over A2S. Only meaningful while that server is running: "
            "502/504 means the server is down or unreachable — treat that as "
            "'not running', not as a failure to retry."
        ),
        timeout_s=30.0,
    ),
    ToolSpec(
        name="get_server_players",
        method="GET",
        path="/api/servers/{server_id}/players",
        description=(
            "Current player list for a server, queried over RCON. 502 when "
            "the server is not running or RCON is disabled/unconfigured on "
            "its definition."
        ),
        timeout_s=30.0,
    ),
    ToolSpec(
        name="get_server_restart_schedule",
        method="GET",
        path="/api/servers/{server_id}/schedule-restart",
        description=(
            "Read the armed scheduled restart for this server: when it "
            "restarts, the in-game warnings queued so far and the seconds "
            "remaining. Returns armed=false when nothing is scheduled (or the "
            "armed schedule belongs to a different definition — only one "
            "server can run at a time)."
        ),
    ),
    # ---------------------------------------------------------------- mods
    ToolSpec(
        name="list_mods",
        method="GET",
        path="/api/mods",
        description=(
            "List the mod library: names, installed vs latest Workshop "
            "versions, update availability, pin staleness against the current "
            "engine build, and which servers/modpacks reference each mod. "
            "Optional filters: local=true only downloaded mods; q= name "
            "substring; update=true only mods with an update available; "
            "state= Workshop api_state (e.g. 'ok', 'deleted')."
        ),
        arg_model=ModListFilters,
    ),
    ToolSpec(
        name="get_mod",
        method="GET",
        path="/api/mods/{guid}",
        description=(
            "Full detail for one library mod by its 16-hex Workshop GUID: "
            "cached versions, resolved dependency tree, the parent mods that "
            "require it, and every server/modpack using it. 404 if the GUID "
            "is not in the library."
        ),
    ),
    ToolSpec(
        name="get_mod_dependencies",
        method="GET",
        path="/api/mods/{guid}/dependencies",
        description=(
            "Resolve just the dependency tree of a mod GUID (which mods it "
            "needs, which are already downloaded). Lighter than get_mod when "
            "only the tree is needed."
        ),
    ),
    ToolSpec(
        name="search_workshop_mods",
        method="GET",
        path="/api/mods/search",
        description=(
            "Search the Steam Workshop for Arma Reforger mods by free-text "
            "query (q, required; limit 1-80, default 20). Returns workshop "
            "ids, names and URLs — a search result is not installed yet; "
            "adding it is a separate (mutating) action. 502 when the Workshop "
            "is unreachable."
        ),
        arg_model=WorkshopSearchQuery,
        timeout_s=30.0,
    ),
    # ------------------------------------------------------------ modpacks
    ToolSpec(
        name="list_modpacks",
        method="GET",
        path="/api/modpacks",
        description=(
            "List every saved modpack: name, description and its load-ordered "
            "mod items (with each mod's current name). Reusable, named mod "
            "lists that can be applied to a server definition."
        ),
    ),
    ToolSpec(
        name="get_modpack",
        method="GET",
        path="/api/modpacks/{pack_id}",
        description=(
            "Read one modpack in full: its load-ordered item list with mod "
            "names. 404 if the id is unknown."
        ),
    ),
    # -------------------------------------------------------------- engine
    ToolSpec(
        name="get_engine_status",
        method="GET",
        path="/api/engine",
        description=(
            "Engine (Reforger dedicated server binary) status: installed "
            "build, latest known build and when it was last checked. Check "
            "this before starting a server or scheduling updates."
        ),
    ),
    ToolSpec(
        name="check_engine_update",
        method="POST",
        path="/api/engine/check",
        description=(
            "Refresh installed/latest engine build numbers right now by "
            "querying Steam — cheap and synchronous, downloads nothing. "
            "Compare the returned builds to decide whether an engine update "
            "is worth running."
        ),
        timeout_s=30.0,
    ),
    # ---------------------------------------------------------------- jobs
    ToolSpec(
        name="list_jobs",
        method="GET",
        path="/api/jobs",
        description=(
            "List recent background jobs (engine updates, mod scans/updates, "
            "downloads), newest first. Optional filters: limit (1-500, "
            "default 50), state (queued/running/succeeded/failed/...), kind "
            "(job type such as engine_update or mod_sync). Use this to "
            "discover job ids."
        ),
        arg_model=JobListFilters,
    ),
    ToolSpec(
        name="get_job",
        method="GET",
        path="/api/jobs/{job_id}",
        description=(
            "Read one background job's full record: state, progress, current "
            "step, params, final result or error, and a log tail. Poll this "
            "with the job_id that any mutating tool returning a job gives "
            "you. 404 if the id is unknown or the row was pruned."
        ),
    ),
    # --------------------------------------------------------------- files
    ToolSpec(
        name="list_server_files",
        method="GET",
        path="/api/servers/{server_id}/files",
        description=(
            "List a directory in a server's profile dir: entries with "
            "name/rel_path, file-or-dir type, size, mtime and whether each "
            "file is editable text/JSON. Omit path for the profile root; 404 "
            "for an unknown server."
        ),
        arg_model=FileListQuery,
    ),
    ToolSpec(
        name="read_server_file",
        method="GET",
        path="/api/servers/{server_id}/files/content",
        description=(
            "Read one file from a server's profile dir by relative path. "
            "Small text files (config.json, .json/.txt/...) return their "
            "content with editable=true; anything too large or binary returns "
            "metadata only (content=null). 404 if the file does not exist."
        ),
        arg_model=FileContentQuery,
    ),
    # -------------------------------------------------------------- backup
    ToolSpec(
        name="export_backup",
        method="GET",
        path="/api/backup/export",
        description=(
            "Export the whole configuration as one JSON backup document: "
            "every server definition (settings + mod set + pins) and every "
            "modpack. Includes server/admin/game passwords in cleartext — "
            "treat the output as a secret. Excludes runtime state and the mod "
            "disk cache; the import tool (mutating) can restore it."
        ),
    ),
    # ------------------------------------------------------------ settings
    ToolSpec(
        name="get_settings",
        method="GET",
        path="/api/settings",
        description=(
            "Read the manager's runtime settings: nightly engine check "
            "toggle/schedule and the log spam patterns. Changing them is a "
            "separate (mutating) action."
        ),
    ),
    # ------------------------------------------------------------- storage
    ToolSpec(
        name="get_storage_usage",
        method="GET",
        path="/api/storage",
        description=(
            "On-disk mod storage view: mods_path, free/total bytes, per-mod "
            "file sizes, orphaned mods that are safe to delete, mods kept "
            "only as dependencies (with the parents that need them), and "
            "unreferenced library entries. The dependency closure is resolved "
            "from the DB, so no Workshop calls are made."
        ),
    ),
    # ----------------------------------------------------------- scenarios
    ToolSpec(
        name="resolve_scenarios",
        method="POST",
        path="/api/scenarios/resolve",
        description=(
            "Resolve a list of mod GUIDs into the scenarios those mods add "
            "(game_id, name, game_mode, player_count), e.g. to pick a "
            "scenario_game_id for a server definition. Served from the local "
            "cache first; only uncached GUIDs query the Workshop API, and a "
            "GUID whose lookup fails is reported in failed[] without failing "
            "the others."
        ),
        arg_model=ScenarioGuids,
    ),
    # ------------------------------------------- mutations (confirm = required)
    # Servers: lifecycle. start is gated purely at this layer — the start
    # endpoint itself takes no confirm flag; the gate must never be bypassed.
    ToolSpec(
        name="create_server",
        method="POST",
        path="/api/servers",
        description=(
            "Create a new server definition: name (required) plus any subset "
            "of the network/binding, RCON and game settings, optionally with "
            "an initial load-ordered mods set. Pure configuration — nothing "
            "is started and no mod is downloaded; use start_server to boot "
            "it, and the modpack/mod tools to manage its mod set later."
        ),
        arg_model=ServerCreate,
        confirm=True,
    ),
    ToolSpec(
        name="update_server",
        method="PATCH",
        path="/api/servers/{server_id}",
        description=(
            "Patch a server definition (PATCH semantics — omitted fields "
            "stay): any subset of the network/binding, RCON and game "
            "settings, optionally a wholesale replacement of the load-ordered "
            "mods set (a provided mods[] replaces the set; mods that survive "
            "keep their pins). Pure configuration — the running process is "
            "not touched; the new settings take effect on the next start. "
            "404 for an unknown id."
        ),
        arg_model=ServerUpdate,
        confirm=True,
    ),
    ToolSpec(
        name="clone_server",
        method="POST",
        path="/api/servers/{server_id}/clone",
        description=(
            "Deep-copy a server definition into a new one: every "
            "config/network/RCON/game setting plus its whole load-ordered mod "
            "set with pins is copied verbatim; only the clone's name comes "
            "from the body. Side effect: a new independent definition row "
            "(editing the copy never touches the source) that collides with "
            "its source on bind/a2s/rcon ports. 404 for an unknown id."
        ),
        arg_model=ServerCloneIn,
        confirm=True,
    ),
    ToolSpec(
        name="start_server",
        method="POST",
        path="/api/servers/{server_id}/start",
        description=(
            "Start this server definition: runs the pre-flight checks, then "
            "boots the Reforger dedicated-server process, which binds its "
            "configured ports and becomes LAN-joinable. The single-server rule "
            "applies: 409 if another definition is already running, and a "
            "missing engine binary fails the start. Side effect: a real OS "
            "process is spawned."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="stop_server",
        method="POST",
        path="/api/servers/{server_id}/stop",
        description=(
            "Gracefully stop this server's running process (SIGTERM, then "
            "SIGKILL after the grace period). Players are disconnected; the "
            "definition itself is not modified. 409 if this definition is not "
            "the one currently running."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="delete_server",
        method="DELETE",
        path="/api/servers/{server_id}",
        description=(
            "Permanently delete a server definition: its settings and its "
            "whole mod-assignment set with pins. The profile dir (logs, "
            "generated config) is left on disk. 409 while that server is "
            "running — stop it first. There is no undo short of restoring a "
            "backup."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="schedule_server_restart",
        method="POST",
        path="/api/servers/{server_id}/schedule-restart",
        description=(
            "Arm a warned restart for this running server: #say warnings at "
            "each warn_at seconds-before-restart offset (default 300/60/10), "
            "then #restart at T-0 — players are disconnected when it fires. "
            "In-memory only: the schedule is gone if the manager restarts. "
            "409 unless this definition is the one currently running or RCON "
            "is not configured on it; arming replaces any prior schedule."
        ),
        arg_model=ScheduleRestartIn,
        confirm=True,
    ),
    ToolSpec(
        name="cancel_server_restart",
        method="DELETE",
        path="/api/servers/{server_id}/schedule-restart",
        description=(
            "Cancel this server's armed scheduled restart. Side effect: the "
            "pending restart no longer fires. A harmless no-op returning "
            "armed=false when nothing is armed or the armed schedule belongs "
            "to another definition (only one server runs at a time)."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="send_rcon_command",
        method="POST",
        path="/api/servers/{server_id}/rcon",
        description=(
            "Run one RCON command (e.g. #lock, #mission <name>, #say <text>) "
            "against this server's running process and return its raw "
            "response text. 409 unless this definition is the one currently "
            "running; 400 when RCON is not configured on it; 502/504 on RCON "
            "failure or timeout. For the player list use get_server_players "
            "instead."
        ),
        arg_model=RconCommandIn,
        confirm=True,
        timeout_s=30.0,
    ),
    # Mods (server-scoped): check/apply pair + per-assignment pin/unpin.
    ToolSpec(
        name="check_server_mod_updates",
        method="POST",
        path="/api/servers/{server_id}/mods/update/check",
        description=(
            "Enqueue the mod-update-check job scoped to this server's "
            "assigned mod set: refreshes latest Workshop versions and pin "
            "staleness for exactly those mods — downloads nothing. Returns "
            "the enqueued job ({job_id, kind}) immediately — poll it with "
            "wait_for_job(job_id), then use list_mods (update=true) to see "
            "what is newer and apply_server_mod_updates to act on it."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="apply_server_mod_updates",
        method="POST",
        path="/api/servers/{server_id}/mods/update/apply",
        description=(
            "Enqueue the mod-update-apply job for this server's assigned mod "
            "set: downloads newer Workshop versions where available and "
            "reconciles pins against the current engine build. Side effect: "
            "writes to the mod disk cache. Returns the enqueued job "
            "({job_id, kind}) immediately — poll it with "
            "wait_for_job(job_id). 409 while the server is running or the "
            "free-space guard trips."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="pin_server_mod",
        method="POST",
        path="/api/servers/{server_id}/mods/{guid}/pin",
        description=(
            "Pin one of this server's assigned mods to an exact Workshop "
            "version (with a reason), recorded against the current engine "
            "build — that assignment stops following mod updates. Side "
            "effect: rewrites the assignment's pin columns. 404 if the mod "
            "is not assigned to this server; 409 while the installed engine "
            "build is unknown; 400 for an empty version."
        ),
        arg_model=ServerModPinIn,
        confirm=True,
    ),
    ToolSpec(
        name="unpin_server_mod",
        method="DELETE",
        path="/api/servers/{server_id}/mods/{guid}/pin",
        description=(
            "Remove the version pin from one of this server's assigned mods "
            "so it follows updates again. Side effect: clears the "
            "assignment's pin columns. 404 if the mod is not assigned to "
            "this server."
        ),
        confirm=True,
    ),
    # Mods
    ToolSpec(
        name="scan_mods",
        method="POST",
        path="/api/mods/scan",
        description=(
            "Enqueue the mod_sync job: re-scans the on-disk addon cache, "
            "reconciles the mod library with what is actually on disk and "
            "refreshes Workshop metadata. Side effect: rewrites library rows. "
            "Returns the enqueued job ({job_id, kind}) immediately — poll it "
            "with wait_for_job(job_id)."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="add_mod",
        method="POST",
        path="/api/mods/add",
        description=(
            "Add a Workshop mod to the library by URL or bare 16-hex id: "
            "resolves it on the Workshop and stores the catalogue entry. Side "
            "effect: creates the library row; no files are downloaded. 404 if "
            "the Workshop cannot resolve the id (deleted/private), 502 if the "
            "Workshop is unreachable."
        ),
        arg_model=ModAddIn,
        confirm=True,
    ),
    ToolSpec(
        name="delete_mod",
        method="DELETE",
        path="/api/mods/{guid}",
        description=(
            "Delete a mod from the library entirely: the catalogue row and, "
            "if present, its on-disk addon files. 409 while any server runs, "
            "or while the mod is still assigned to a server/modpack or held "
            "by another mod's dependency closure."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="download_mod",
        method="POST",
        path="/api/mods/{guid}/download",
        description=(
            "Force a (re)download of one library mod's addon files by 16-hex GUID, "
            "optionally pinned to an exact Workshop version. Side effect: writes to "
            "the mod disk cache (steamcmd). 404 if the GUID is not a library row, "
            "400/409 if the free-space guard trips. Returns the enqueued job "
            "({job_id, kind}) immediately — poll it with wait_for_job(job_id)."
        ),
        arg_model=ModDownloadIn,
        confirm=True,
    ),
    ToolSpec(
        name="verify_mods",
        method="POST",
        path="/api/mods/verify",
        description=(
            "Enqueue the verify_repair job: re-check downloaded addon files against "
            "their Workshop manifests and re-fetch anything missing or corrupt. "
            "guids omitted = the whole local library; pass a guids list to scope "
            "it. Side effect: may rewrite the mod disk cache. Returns the enqueued "
            "job ({job_id, kind}) immediately — poll it with wait_for_job(job_id)."
        ),
        arg_model=ModVerifyIn,
        confirm=True,
    ),
    ToolSpec(
        name="check_all_mod_updates",
        method="POST",
        path="/api/mods/updates/check",
        description=(
            "Enqueue a library-WIDE mod-update-check job: refresh latest Workshop "
            "versions and pin staleness for every mod in the library — downloads "
            "nothing. Use check_server_mod_updates instead to scope it to one "
            "server's assigned set. Returns the enqueued job ({job_id, kind}) "
            "immediately — poll it with wait_for_job(job_id), then list_mods "
            "(update=true) to see what is newer."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="apply_all_mod_updates",
        method="POST",
        path="/api/mods/updates/apply",
        description=(
            "Enqueue a library-WIDE mod-update-apply job: download the newer "
            "Workshop version of every library mod that has one and reconcile pins "
            "against the current engine build. Side effect: writes to the mod disk "
            "cache. 409 while any server is running or the free-space guard trips. "
            "Returns the enqueued job ({job_id, kind}) immediately — poll it with "
            "wait_for_job(job_id)."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="delete_mod_local",
        method="DELETE",
        path="/api/mods/{guid}/local",
        description=(
            "Delete one mod's on-disk addon files but KEEP its catalogue row "
            "(is_local flips to false) — the disk-space counterpart of delete_mod, "
            "which removes the row too. Side effect: removes the addon directory. "
            "409 while any server runs, if the mod has no on-disk files, or while "
            "it is still referenced by a server/modpack or another mod's resolved "
            "dependency closure; 404 for an unknown GUID. Returns the updated mod."
        ),
        confirm=True,
    ),
    # Modpacks
    ToolSpec(
        name="create_modpack",
        method="POST",
        path="/api/modpacks",
        description=(
            "Create a reusable, named, load-ordered mod list: items is a list "
            "of {mod_guid, load_order}. Side effect: a new modpack row "
            "(duplicate GUIDs inside items are a 422, a name collision a "
            "409). Creating a pack never touches any server definition."
        ),
        arg_model=ModpackCreate,
        confirm=True,
    ),
    ToolSpec(
        name="update_modpack",
        method="PATCH",
        path="/api/modpacks/{pack_id}",
        description=(
            "Patch a modpack: name and/or description, and/or a wholesale "
            "replacement of its item set (PATCH semantics — omitted fields "
            "stay; a provided items[] replaces the set). Duplicate GUIDs "
            "inside items are a 422. 404 for an unknown id."
        ),
        arg_model=ModpackUpdate,
        confirm=True,
    ),
    ToolSpec(
        name="delete_modpack",
        method="DELETE",
        path="/api/modpacks/{pack_id}",
        description=(
            "Permanently delete a modpack. Server definitions keep their "
            "current mod assignments — packs are copied on apply, never "
            "live-linked. 404 for an unknown id."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="apply_modpack",
        method="POST",
        path="/api/modpacks/{pack_id}/apply/{server_id}",
        description=(
            "Write a modpack's mod list into a server definition's mod set. "
            "mode=replace (default) swaps the whole set for the pack's "
            "load-ordered list, keeping pins of mods that survive; mode=append "
            "only adds GUIDs not already assigned, after the existing load "
            "order, and never drops a pin. Side effect: rewrites the server's "
            "assignment rows (dropped pins are reported). 409 while the target "
            "server runs; 404 for an unknown pack or server."
        ),
        arg_model=ModpackApplyIn,
        confirm=True,
    ),
    ToolSpec(
        name="create_modpack_from_server",
        method="POST",
        path="/api/modpacks/from-server/{server_id}",
        description=(
            "Snapshot a server definition's current mod set and load order into a "
            "NEW modpack (name required, optional description). Side effect: a new "
            "modpack row. Pins are NOT carried into the pack (a pack is a mod list, "
            "not a version lock) — pins_note in the response names any that were "
            "dropped. The source server is never modified. 404 for an unknown "
            "server; 409 on a modpack name collision."
        ),
        arg_model=ModpackFromServerIn,
        confirm=True,
    ),
    # Engine
    ToolSpec(
        name="update_engine",
        method="POST",
        path="/api/engine/update",
        description=(
            "Enqueue the engine-update job: downloads and installs the latest "
            "Reforger dedicated-server build via steamcmd — a multi-GB, "
            "minutes-long download into the server dir. 409 while any server "
            "is running. Returns the enqueued job ({job_id, kind}) "
            "immediately — poll it with wait_for_job(job_id)."
        ),
        confirm=True,
    ),
    # Jobs
    ToolSpec(
        name="cancel_job",
        method="POST",
        path="/api/jobs/{job_id}/cancel",
        description=(
            "Request cancellation of a queued or running background job "
            "(cooperative: it stops at its next checkpoint). 404 for an "
            "unknown id; 409 if the job is already terminal. Returns "
            "{job_id, cancelled}."
        ),
        confirm=True,
    ),
    ToolSpec(
        name="prune_jobs",
        method="POST",
        path="/api/jobs/prune",
        description=(
            "Bulk-delete finished (terminal) job rows. Optional states list is "
            "intersected with the terminal set server-side (a non-terminal "
            "state is a 400); the default prunes every terminal state. Side "
            "effect: job history is gone — get_job on a pruned id is a 404. "
            "Returns {deleted: n}."
        ),
        arg_model=JobPruneIn,
        confirm=True,
    ),
    ToolSpec(
        name="delete_job",
        method="DELETE",
        path="/api/jobs/{job_id}",
        description=(
            "Delete one finished job row. 404 for an unknown or already-pruned "
            "id; 409 while the job is still queued or running (cancel it "
            "first). Side effect: that job's history is gone."
        ),
        confirm=True,
    ),
    # Profile files (all refused with 409 while that server is running)
    ToolSpec(
        name="write_server_file",
        method="PUT",
        path="/api/servers/{server_id}/files/content",
        description=(
            "Create or overwrite one text file in a server's profile dir: "
            "path (relative, required) and content. Side effect: writes to "
            "disk; .json targets are validated server-side (400 on invalid "
            "JSON) and a missing parent directory is a 400 — create it first. "
            "409 while that server is running."
        ),
        arg_model=WriteContentIn,
        query_model=FilePathQuery,
        confirm=True,
    ),
    ToolSpec(
        name="delete_server_file",
        method="DELETE",
        path="/api/servers/{server_id}/files",
        description=(
            "Delete one file or directory (recursively) from a server's "
            "profile dir by relative path. Side effect: removes it from disk. "
            "404 if missing; 400 for the profile root; 409 while that server "
            "is running."
        ),
        query_model=FilePathQuery,
        confirm=True,
    ),
    ToolSpec(
        name="mkdir_server_dir",
        method="POST",
        path="/api/servers/{server_id}/files/mkdir",
        description=(
            "Create a directory (parents included) in a server's profile dir. "
            "Side effect: creates it on disk (an existing path is left as is). "
            "409 while that server is running."
        ),
        arg_model=MkdirIn,
        confirm=True,
    ),
    ToolSpec(
        name="rename_server_file",
        method="POST",
        path="/api/servers/{server_id}/files/rename",
        description=(
            "Move/rename a file or directory within a server's profile dir "
            "(path -> new_path). Side effect: the old path is gone. 404 if "
            "the source is missing; 400 if the destination exists, the "
            "profile root is involved, or a directory is moved into itself; "
            "409 while that server is running."
        ),
        arg_model=RenameIn,
        confirm=True,
    ),
    ToolSpec(
        name="upload_server_files",
        method="POST",
        path="/api/servers/{server_id}/files/upload",
        description=(
            "Upload one or more text files into a server's profile dir: files "
            "= [{filename, content}] (UTF-8 text, no path separators in the "
            "names), optional path = target subdirectory (must already "
            "exist). Side effect: writes the files to disk; 413 over the size "
            "limit; 409 while that server is running."
        ),
        arg_model=ServerFileUploads,
        query_model=UploadDirQuery,
        multipart_field="files",
        confirm=True,
    ),
    # Backup / settings
    ToolSpec(
        name="import_backup",
        method="POST",
        path="/api/backup/import",
        description=(
            "Import a backup document (the shape export_backup returns): "
            "server definitions and modpacks are keyed by name. dry_run "
            "defaults to true — you get the per-item plan and nothing "
            "changes; pass dry_run=false to apply it (on_conflict: skip "
            "leaves existing names alone, replace overwrites them; a replace "
            "of the running server's definition is forced to skip). Side "
            "effect when applied: creates/overwrites definitions and packs "
            "(passwords arrive in cleartext inside the document)."
        ),
        arg_model=BackupDocument,
        query_model=BackupImportQuery,
        confirm=True,
    ),
    ToolSpec(
        name="patch_settings",
        method="PATCH",
        path="/api/settings",
        description=(
            "Patch the manager's runtime settings (PATCH semantics): "
            "nightly_check_enabled, nightly_check_hour (0-23) and "
            "log_spam_patterns; omitted keys stay unchanged. Side effect: "
            "takes effect immediately — the nightly scheduler restarts and "
            "the console spam filter re-warms as needed. Returns the updated "
            "settings."
        ),
        arg_model=SettingsUpdate,
        confirm=True,
    ),
)


# --------------------------------------------------------------------------
# Registration factory
# --------------------------------------------------------------------------


def _path_params(path: str) -> list[str]:
    return [part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")]


def _field_default(field_info: FieldInfo) -> Any:
    """The Python default for an optional pydantic field (required -> none)."""
    if field_info.is_required():
        return inspect.Parameter.empty
    if field_info.default is not PydanticUndefined:
        return field_info.default
    if field_info.default_factory is not None:
        return field_info.default_factory()
    return None


def _build_tool_fn(spec: ToolSpec) -> Any:
    """Build the typed async function one spec describes.

    The signature (path params + arg_model fields + query_model fields, then a
    trailing ``confirm: bool = False`` for confirm-gated specs) is what the SDK
    turns into the tool's JSON input schema; the body only reassembles what the
    SDK validated and delegates to ``api.call``.
    """
    path_params = _path_params(spec.path)
    arg_fields = spec.arg_model.model_fields if spec.arg_model is not None else {}
    query_fields = spec.query_model.model_fields if spec.query_model is not None else {}

    def _param(name: str, annotation: Any, default: Any) -> inspect.Parameter:
        return inspect.Parameter(
            name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation, default=default
        )

    # Required parameters first: a flattened arg model may declare required
    # fields after defaulted ones (e.g. BackupDocument), which a real def
    # could not; the SDK validates by keyword, order is presentation only.
    flattened = [
        _param(name, _PATH_PARAM_TYPES.get(name, str), inspect.Parameter.empty)
        for name in path_params
    ] + [
        _param(name, field_info.annotation, _field_default(field_info))
        for name, field_info in arg_fields.items()
    ] + [
        _param(name, field_info.annotation, _field_default(field_info))
        for name, field_info in query_fields.items()
    ]
    parameters = [p for p in flattened if p.default is inspect.Parameter.empty] + [
        p for p in flattened if p.default is not inspect.Parameter.empty
    ]
    if spec.confirm:
        parameters.append(_param("confirm", bool, False))

    async def tool_fn(**kwargs: Any) -> Any:
        # Confirm gate first: a confirm-gated tool must never reach the API
        # (and never forward the flag itself — no endpoint takes it).
        if spec.confirm and not kwargs.get("confirm", False):
            raise ToolError(
                "API 400: this tool changes real state — re-run it with "
                "confirm=true to proceed (no API call was made)"
            )
        request_kwargs: dict[str, Any] = {}
        if query_fields:
            given = {
                name: value
                for name, value in kwargs.items()
                if name in query_fields and value is not None
            }
            payload = spec.query_model(**given).model_dump(exclude_unset=True)  # type: ignore[misc]
            request_kwargs["params"] = {k: v for k, v in payload.items() if v is not None}
        if arg_fields:
            given = {
                name: value
                for name, value in kwargs.items()
                if name in arg_fields
                # PATCH semantics: an omitted optional field must not become an
                # explicit null on the wire.
                and not (value is None and not arg_fields[name].is_required())
            }
            model = spec.arg_model(**given)  # type: ignore[misc]
            payload = model.model_dump(exclude_unset=True)
            if spec.multipart_field is not None:
                # Multipart form data instead of a JSON body; any leftover
                # arg-model field is a query param (the upload's target dir).
                items = payload.pop(spec.multipart_field, [])
                request_kwargs["files"] = [
                    ("files", (item["filename"], item["content"])) for item in items
                ]
                leftover = {k: v for k, v in payload.items() if v is not None}
                if leftover:
                    request_kwargs.setdefault("params", {}).update(leftover)
            elif spec.method == "GET":
                # Query params: drop None so an omitted filter stays omitted.
                request_kwargs.setdefault("params", {}).update(
                    {k: v for k, v in payload.items() if v is not None}
                )
            else:
                request_kwargs["json"] = payload
        try:
            body = await api.call(
                spec.method,
                spec.path.format(**{name: kwargs[name] for name in path_params}),
                timeout_s=spec.timeout_s,
                **request_kwargs,
            )
        except api.ApiError as exc:
            # Deterministic, self-describing tool error: status + API detail.
            raise ToolError(f"API {exc.status_code}: {exc.detail}") from exc
        # Encode here, once: the SDK's legacy result converter would flatten a
        # list body into one text block per item, not one JSON document.
        return to_json(body, fallback=str, indent=2).decode()

    tool_fn.__signature__ = inspect.Signature(parameters)
    tool_fn.__name__ = spec.name
    tool_fn.__qualname__ = spec.name
    tool_fn.__doc__ = spec.description
    return tool_fn


def register_tools(mcp: MCPServer) -> None:
    """Register every spec in :data:`TOOL_SPECS` on the given MCP server."""
    for spec in TOOL_SPECS:
        mcp.add_tool(
            _build_tool_fn(spec),
            name=spec.name,
            description=spec.description,
            structured_output=False,
        )
