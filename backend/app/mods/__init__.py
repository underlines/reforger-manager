"""Mod subsystem: Workshop API client, local disk scanner, offline scenario
scan, and dependency resolution (API first, local ``addon.gproj`` fallback).

Phase 3a implements data acquisition:

* ``workshop`` — async ``api.reforgermods.net/v2`` client behind one
  container-wide rate limiter + a 10-minute TTL cache.
* ``scanner`` — pure/offline scan of ``<MODS_DIR>/reforger/addons`` (skips any
  directory without a ``meta`` file, e.g. ``saves/``).
* ``sync``  — the ``mod_sync`` job: scan -> local upsert -> API enrich -> prune.
* ``resolve`` — BFS dependency resolution reused by pre-flight (Phase 3b).

Pinning is library-only here; routes and pre-flight integration live elsewhere.
"""

from .resolve import ResolvedNode, ResolvedTree, resolve_dependencies
from .scanner import (
    ScannedMod,
    ScanResult,
    addons_root,
    has_thumbnail,
    parse_gproj,
    parse_meta,
    parse_scenarios,
    parse_serverdata,
    scan_all,
    scan_one,
)
from .sync import enrich_one, run_mod_sync
from .downloader import ModDownloadError, ensure_mods_ready, run_mod_download
from .pinning import (
    CurrentEngineBuildMissing,
    PinRecordNotFound,
    PinningError,
    is_stale,
    pin_mod,
    pin_server_mod,
    unpin_mod,
    unpin_server_mod,
)
from .updates import (
    MOD_UPDATE_APPLY_JOB_KIND,
    MOD_UPDATE_CHECK_JOB_KIND,
    UpdateScopeError,
    apply_updates,
    check_updates,
    make_apply_updates_job,
    make_check_updates_job,
    normalize_scope,
)
from .logview import (
    LogDownload,
    LogFile,
    LogLine,
    LogSearchResult,
    current_log,
    iter_log_lines,
    read_log_download,
    search_log,
)
from .workshop import (
    ModNotFound,
    RateLimitExceeded,
    WorkshopClient,
    WorkshopError,
    clear_cache,
    workshop,
)

MOD_SYNC_JOB_KIND = "mod_sync"

__all__ = [
    # workshop
    "WorkshopClient",
    "workshop",
    "WorkshopError",
    "ModNotFound",
    "RateLimitExceeded",
    "clear_cache",
    # scanner
    "scan_all",
    "scan_one",
    "ScannedMod",
    "ScanResult",
    "addons_root",
    "parse_meta",
    "parse_gproj",
    "parse_scenarios",
    "parse_serverdata",
    "has_thumbnail",
    # sync
    "run_mod_sync",
    "enrich_one",
    "MOD_SYNC_JOB_KIND",
    "run_mod_download",
    "ensure_mods_ready",
    "ModDownloadError",
    # log view
    "current_log",
    "iter_log_lines",
    "search_log",
    "read_log_download",
    "LogFile",
    "LogLine",
    "LogSearchResult",
    "LogDownload",
    # resolve
    "resolve_dependencies",
    "ResolvedTree",
    "ResolvedNode",
    # pinning
    "pin_mod",
    "unpin_mod",
    "pin_server_mod",
    "unpin_server_mod",
    "is_stale",
    "PinningError",
    "PinRecordNotFound",
    "CurrentEngineBuildMissing",
    # updates
    "check_updates",
    "apply_updates",
    "make_check_updates_job",
    "make_apply_updates_job",
    "normalize_scope",
    "UpdateScopeError",
    "MOD_UPDATE_CHECK_JOB_KIND",
    "MOD_UPDATE_APPLY_JOB_KIND",
]
