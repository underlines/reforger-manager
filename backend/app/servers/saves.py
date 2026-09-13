"""Save-game discovery + mod-drift analysis for one server.

Read-only: no writes, no HTTP routes (a later story adds those). Everything
here is either a filesystem read under the server's profile directory (via
:func:`app.servers.files.resolve_safe_path`, never hand-rolled path joining)
or a read-only DB query.

Layout, verified against the live system (see the sprint story brief, not
re-derived here):

* Saves live at ``PROFILES_DIR/{server_id}/profile/.save/game/{scenarioDir}/
  playthrough{NNN}/savepoint{NNN}/``, each holding a ``meta-info.json`` and an
  opaque ``WorldState/`` directory.
* ``.save/settings/`` is a sibling of ``.save/game/`` and is never listed,
  touched, or descended into.
* The scenario directory name cannot be derived from ``Server.scenario_game_id``
  (braces stripped, ``_``/``-`` mangled, extension dropped) — scenario
  directories are discovered by globbing, then each save's parsed
  ``m_sMissionResource`` is compared against ``server.scenario_game_id`` by
  exact string match.
* A server with no ``.save/game`` at all is a normal, common state, not an
  error (started/stopped, never saved).
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ENGINE_SINGLETON_ID, Engine, Server, ServerConfigRevision, ServerMod
from ..schemas.saves import ModDrift, PlaythroughOut, SavePointOut, ScenarioSaves
from .files import resolve_safe_path

logger = logging.getLogger("reforger.saves")

_META_FILENAME = "meta-info.json"
_FALLBACK_MAX_DEPTH = 6
_PLAYTHROUGH_NR_RE = re.compile(r"^playthrough0*(\d+)$", re.IGNORECASE)
_SAVEPOINT_NR_RE = re.compile(r"^savepoint0*(\d+)$", re.IGNORECASE)


# --------------------------------------------------------------------- helpers
def _as_utc(dt: datetime | None) -> datetime | None:
    """Normalise to a tz-aware UTC datetime; naive input is assumed UTC.

    sqlite (the test DB) round-trips ``DateTime(timezone=True)`` as naive, so
    revision/save timestamps must be normalised before comparison.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int_suffix(name: str, pattern: re.Pattern[str]) -> int | None:
    m = pattern.match(name)
    return int(m.group(1)) if m else None


def _read_meta(path: Path) -> tuple[bool, dict]:
    """Best-effort JSON read. ``(False, {})`` on any missing/unreadable/invalid file."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.info("unreadable meta-info.json at %s: %s", path, exc)
        return False, {}
    if not isinstance(data, dict):
        return False, {}
    return True, data


def _dir_size(path: Path) -> int:
    """Recursive size sum of every file under ``path`` (symlinks never followed)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
        for fname in files:
            fpath = root_path / fname
            try:
                if not fpath.is_symlink():
                    total += fpath.stat().st_size
            except OSError:
                continue
    return total


def _find_savepoint_dirs_primary(game_root: Path) -> list[Path]:
    """``.save/game/*/playthrough*/savepoint*`` — enumerated as *directories*,
    independent of whether ``meta-info.json`` exists inside (it may not)."""
    found: list[Path] = []
    if not game_root.is_dir():
        return found
    try:
        scenario_dirs = sorted(p for p in game_root.iterdir() if p.is_dir())
    except OSError:
        return found
    for scenario_dir in scenario_dirs:
        try:
            playthrough_dirs = sorted(scenario_dir.glob("playthrough*"))
        except OSError:
            continue
        for playthrough_dir in playthrough_dirs:
            if not playthrough_dir.is_dir():
                continue
            try:
                savepoint_dirs = sorted(playthrough_dir.glob("savepoint*"))
            except OSError:
                continue
            for savepoint_dir in savepoint_dirs:
                if savepoint_dir.is_dir():
                    found.append(savepoint_dir)
    return found


def _find_savepoint_dirs_fallback(save_root: Path, settings_root: Path) -> list[Path]:
    """Bounded (depth <= 6) recursive search for stray ``meta-info.json`` files
    under ``.save/`` — covers an unexpected layout the primary glob misses.
    Never descends into ``.save/settings``."""
    found: list[Path] = []
    if not save_root.is_dir():
        return found
    for root, dirs, files in os.walk(save_root, followlinks=False):
        root_path = Path(root)
        try:
            depth = len(root_path.relative_to(save_root).parts)
        except ValueError:
            depth = 0
        if root_path == settings_root or settings_root in root_path.parents:
            dirs[:] = []
            continue
        if depth >= _FALLBACK_MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
        if _META_FILENAME in files and root_path.name.lower().startswith("savepoint"):
            found.append(root_path)
    return found


# --------------------------------------------------------------------- discover
async def discover(
    session: AsyncSession,
    server: Server,
    installed_engine_version: str | None = None,
) -> list[ScenarioSaves]:
    """Discover every save point for ``server`` and annotate it with scenario /
    engine / mod drift.

    ``installed_engine_version`` overrides the auto-looked-up currently
    installed engine display version (``Engine.installed_version``); pass it
    explicitly to avoid the lookup (e.g. in tests) or when comparing against a
    hypothetical build.
    """
    base = resolve_safe_path(server.id, None)
    game_root = resolve_safe_path(server.id, "profile/.save/game")
    save_root = resolve_safe_path(server.id, "profile/.save")
    settings_root = resolve_safe_path(server.id, "profile/.save/settings")

    if installed_engine_version is None:
        engine_row = await session.get(Engine, ENGINE_SINGLETON_ID)
        installed_engine_version = engine_row.installed_version if engine_row else None

    # Primary discovery, then the bounded fallback, deduplicated by resolved path.
    ordered_dirs: list[Path] = []
    seen: set[Path] = set()
    for d in _find_savepoint_dirs_primary(game_root):
        resolved = d.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            ordered_dirs.append(d)
    for d in _find_savepoint_dirs_fallback(save_root, settings_root):
        resolved = d.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            ordered_dirs.append(d)

    # Mod-drift inputs: one query each, outside the per-save loop.
    revisions_result = await session.execute(
        select(ServerConfigRevision)
        .where(ServerConfigRevision.server_id == server.id)
        .order_by(ServerConfigRevision.created_at.asc())
    )
    revisions = list(revisions_result.scalars().all())
    revision_times = [_as_utc(r.created_at) for r in revisions]

    current_mods_result = await session.execute(
        select(ServerMod).where(ServerMod.server_id == server.id)
    )
    current_mods = list(current_mods_result.scalars().all())
    current_id_to_name = {m.mod_guid: (m.mod_name or m.mod_guid) for m in current_mods}
    current_ids = set(current_id_to_name)

    def _preceding_revision(saved_at: datetime | None) -> ServerConfigRevision | None:
        if saved_at is None or not revisions:
            return None
        idx = bisect.bisect_right(revision_times, saved_at) - 1
        if idx < 0:
            return None
        return revisions[idx]

    # scenario_name -> playthrough_nr -> accumulator
    scenarios: dict[str, dict[int, dict]] = {}

    for savepoint_dir in ordered_dirs:
        playthrough_dir = savepoint_dir.parent
        scenario_dir_path = playthrough_dir.parent
        scenario_name = scenario_dir_path.name

        readable, data = _read_meta(savepoint_dir / _META_FILENAME)

        uuid = data.get("m_Id") if readable else None
        saved_at_unix = data.get("m_iSavedAtUnix") if readable else None
        saved_at = (
            datetime.fromtimestamp(saved_at_unix, tz=timezone.utc)
            if isinstance(saved_at_unix, (int, float))
            else None
        )
        playtime_seconds = data.get("m_iPlaytimeSeconds") if readable else None
        game_version = data.get("m_sGameVersion") if readable else None
        mission_resource = data.get("m_sMissionResource") if readable else None
        display_name = data.get("m_sSavePointDisplayName") if readable else None
        playthrough_display_name = data.get("m_sPlaythroughDisplayName") if readable else None
        started_unix = data.get("m_iStartedUnix") if readable else None
        started_at = (
            datetime.fromtimestamp(started_unix, tz=timezone.utc)
            if isinstance(started_unix, (int, float))
            else None
        )

        save_point_nr = data.get("m_iSavePointNr") if readable else None
        if not isinstance(save_point_nr, int):
            save_point_nr = _int_suffix(savepoint_dir.name, _SAVEPOINT_NR_RE) or 0
        playthrough_nr = data.get("m_iPlaythroughNr") if readable else None
        if not isinstance(playthrough_nr, int):
            playthrough_nr = _int_suffix(playthrough_dir.name, _PLAYTHROUGH_NR_RE) or 0

        size_bytes = _dir_size(savepoint_dir)
        matches_current_scenario = bool(
            readable and server.scenario_game_id and mission_resource == server.scenario_game_id
        )
        engine_drift = bool(
            readable and installed_engine_version and game_version
            and game_version != installed_engine_version
        )

        saved_at_utc = _as_utc(saved_at)
        revision = _preceding_revision(saved_at_utc)
        mod_drift: ModDrift | None = None
        mod_drift_unknown = False
        if revision is None:
            mod_drift_unknown = True
        else:
            snapshot = revision.snapshot or {}
            mods_list = ((snapshot.get("game") or {}).get("mods") or [])
            snap_id_to_name: dict[str, str] = {}
            for m in mods_list:
                if isinstance(m, dict) and m.get("modId"):
                    snap_id_to_name[m["modId"]] = m.get("name") or m["modId"]
            snap_ids = set(snap_id_to_name)
            added_ids = current_ids - snap_ids
            removed_ids = snap_ids - current_ids
            if added_ids or removed_ids:
                mod_drift = ModDrift(
                    added=sorted(current_id_to_name[i] for i in added_ids),
                    removed=sorted(snap_id_to_name[i] for i in removed_ids),
                )

        rel_path = savepoint_dir.resolve(strict=False)
        try:
            rel_path = rel_path.relative_to(base).as_posix()
        except ValueError:
            rel_path = savepoint_dir.name

        sp_out = SavePointOut(
            dir_name=savepoint_dir.name,
            rel_path=rel_path,
            save_point_nr=save_point_nr,
            playthrough_nr=playthrough_nr,
            readable=readable,
            uuid=uuid,
            saved_at=saved_at,
            playtime_seconds=playtime_seconds,
            game_version=game_version,
            mission_resource=mission_resource,
            display_name=display_name,
            size_bytes=size_bytes,
            matches_current_scenario=matches_current_scenario,
            engine_drift=engine_drift,
            mod_drift=mod_drift,
            mod_drift_unknown=mod_drift_unknown,
        )

        scenario_entry = scenarios.setdefault(scenario_name, {})
        pt_entry = scenario_entry.setdefault(
            playthrough_nr,
            {
                "dir_name": playthrough_dir.name,
                "display_name": None,
                "started_at": None,
                "save_points": [],
            },
        )
        if pt_entry["display_name"] is None and playthrough_display_name:
            pt_entry["display_name"] = playthrough_display_name
        if pt_entry["started_at"] is None and started_at is not None:
            pt_entry["started_at"] = started_at
        pt_entry["save_points"].append(sp_out)

    # Assemble, sorting every level descending by its numeric field.
    scored_scenarios: list[tuple[datetime | None, ScenarioSaves]] = []
    for scenario_name, playthroughs in scenarios.items():
        playthroughs_out: list[PlaythroughOut] = []
        for pt_nr, pdata in playthroughs.items():
            save_points_sorted = sorted(
                pdata["save_points"], key=lambda sp: sp.save_point_nr, reverse=True
            )
            playthroughs_out.append(
                PlaythroughOut(
                    dir_name=pdata["dir_name"],
                    playthrough_nr=pt_nr,
                    display_name=pdata["display_name"],
                    started_at=pdata["started_at"],
                    save_points=save_points_sorted,
                )
            )
        playthroughs_out.sort(key=lambda p: p.playthrough_nr, reverse=True)

        all_saves = [sp for pt in playthroughs_out for sp in pt.save_points]
        mission_resource = next((sp.mission_resource for sp in all_saves if sp.mission_resource), None)
        matches_current_scenario = any(sp.matches_current_scenario for sp in all_saves)
        newest = max((sp.saved_at for sp in all_saves if sp.saved_at is not None), default=None)

        scored_scenarios.append(
            (
                newest,
                ScenarioSaves(
                    scenario_dir=scenario_name,
                    mission_resource=mission_resource,
                    matches_current_scenario=matches_current_scenario,
                    playthroughs=playthroughs_out,
                ),
            )
        )

    _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    scored_scenarios.sort(key=lambda pair: pair[0] or _epoch, reverse=True)
    return [scenario for _, scenario in scored_scenarios]
