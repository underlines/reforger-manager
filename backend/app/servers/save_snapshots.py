"""Manager-owned save snapshots: create/list/rename/delete/restore.

Service layer only — no HTTP routes here (a later story wires routes to what's
below; every public function is written so a router can call it directly and
map the exceptions defined here to HTTP status codes).

A snapshot is a point-in-time copy of one save-point directory (as discovered
by :mod:`app.servers.saves`), stored as a tar.gz plus a JSON manifest sidecar
under ``PROFILES_DIR/{server_id}/snapshots/`` — a top-level dir excluded from
the generic profile file browser (``app.servers.files.EXCLUDED_TOP_LEVEL``) and
from whole-profile archive downloads, so snapshot storage is never
double-counted or exposed through that surface.

Storage layout::

    PROFILES_DIR/{server_id}/snapshots/{snapshot_id}.tar.gz
    PROFILES_DIR/{server_id}/snapshots/{snapshot_id}.json   (the manifest)

The manifest also carries the *restore-location* bookkeeping
(``scenario_dir``/``playthrough_dir_name``/``save_point_dir_name``) captured at
create time via ``saves.discover()`` — that avoids re-deriving scenario
directory naming (which cannot be computed from ``Server.scenario_game_id``
alone, see ``saves.py``'s module docstring) at restore time. An externally
uploaded archive (``store_upload``) does not have this hierarchy context, so
those fields are best-effort/``None`` there; ``restore_snapshot`` falls back to
re-deriving what it can.

Every filesystem write goes through blocking stdlib calls (``tarfile``,
``shutil``, plain file IO) run off the event loop via ``anyio.to_thread``.
Every path written under the *save* tree (not the snapshot store, which is not
resolver-browsable) goes through
:func:`app.servers.files.resolve_safe_path` — including on restore, where the
manifest's recorded directory names are never trusted blindly.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO
from uuid import uuid4

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..mods.freespace import check_free_space
from ..models import Server, ServerMod
from ..schemas.saves import RestoreResult, SnapshotModOut, SnapshotOut
from . import saves
from .files import resolve_safe_path

_META_FILENAME = "meta-info.json"
_MAX_ARCHIVE_MEMBERS = 5000


# ------------------------------------------------------------------ exceptions
class SaveNotFoundError(Exception):
    """The requested save point (or enough of a snapshot's manifest to locate
    its restore target) could not be found. A router maps this to 404."""


class SnapshotNotFoundError(Exception):
    """No snapshot with this id exists for this server. A router maps this to
    404."""


class SnapshotCapExceededError(Exception):
    """The server is already at ``settings.save_snapshot_max_per_server``.
    Nothing is deleted automatically; a router maps this to 409."""


class InvalidArchiveError(Exception):
    """An uploaded archive failed :func:`validate_archive`. A router maps this
    to 400."""


# ---------------------------------------------------------------------- paths
def _snapshots_dir(server_id: int) -> Path:
    """``PROFILES_DIR/{server_id}/snapshots`` — not reachable via
    :func:`resolve_safe_path` (``"snapshots"`` is an excluded top-level dir), so
    this module builds the path directly instead."""
    return settings.profile_dir(server_id) / "snapshots"


def _manifest_path(server_id: int, snapshot_id: str) -> Path:
    return _snapshots_dir(server_id) / f"{snapshot_id}.json"


def _archive_path(server_id: int, snapshot_id: str) -> Path:
    return _snapshots_dir(server_id) / f"{snapshot_id}.tar.gz"


# -------------------------------------------------------------------- helpers
def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for fname in files:
            fpath = Path(root) / fname
            try:
                if not fpath.is_symlink():
                    total += fpath.stat().st_size
            except OSError:
                continue
    return total


def _write_manifest_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _manifest_to_out(manifest: dict[str, Any]) -> SnapshotOut:
    return SnapshotOut(**manifest)


def _skip_symlinks(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """``tarfile.add`` filter: never archive a symlink/hardlink member."""
    if tarinfo.issym() or tarinfo.islnk():
        return None
    return tarinfo


async def _mods_snapshot(session: AsyncSession, server: Server) -> list[dict[str, Any]]:
    result = await session.execute(select(ServerMod).where(ServerMod.server_id == server.id))
    mods = result.scalars().all()
    return [
        SnapshotModOut(mod_id=m.mod_guid, name=m.mod_name, version=m.pinned_version).model_dump()
        for m in mods
    ]


async def _locate_save_point(
    session: AsyncSession, server: Server, save_uuid: str
) -> tuple[Any, str, str, str]:
    """Find the save point with ``uuid == save_uuid`` via ``saves.discover()``.

    Returns ``(SavePointOut, scenario_dir, playthrough_dir_name, save_point_dir_name)``.
    """
    for scenario in await saves.discover(session, server):
        for playthrough in scenario.playthroughs:
            for save_point in playthrough.save_points:
                if save_point.uuid == save_uuid:
                    return (
                        save_point,
                        scenario.scenario_dir,
                        playthrough.dir_name,
                        save_point.dir_name,
                    )
    raise SaveNotFoundError(f"save point {save_uuid!r} not found")


async def _write_snapshot(
    server_id: int, save_dir: Path, label: str, manifest_fields: dict[str, Any]
) -> SnapshotOut:
    """Tar ``save_dir`` into a fresh snapshot + write its manifest sidecar."""
    snapshots_dir = _snapshots_dir(server_id)
    snapshot_id = uuid4().hex
    archive_path = snapshots_dir / f"{snapshot_id}.tar.gz"
    manifest_path = snapshots_dir / f"{snapshot_id}.json"

    def _do_tar() -> int:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        tmp = archive_path.with_name(f"{archive_path.name}.tmp-{os.getpid()}")
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(save_dir, arcname=save_dir.name, filter=_skip_symlinks)
        os.replace(tmp, archive_path)
        return archive_path.stat().st_size

    archive_size = await to_thread.run_sync(_do_tar)

    manifest: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **manifest_fields,
        "archive_size_bytes": archive_size,
    }
    await to_thread.run_sync(_write_manifest_file, manifest_path, manifest)
    return _manifest_to_out(manifest)


async def _auto_backup_existing(session: AsyncSession, server: Server, target_dir: Path) -> str | None:
    """Snapshot whatever currently sits at ``target_dir`` before it's overwritten.

    Prefers going through :func:`create_snapshot` (uuid-based locate, so the
    resulting snapshot carries full restore-location bookkeeping); falls back
    to a direct tar of ``target_dir`` when its ``meta-info.json`` is missing or
    unreadable (so ``create_snapshot``'s uuid-based locate can't find it).
    """
    meta_path = target_dir / _META_FILENAME
    existing_uuid: str | None = None
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            existing_uuid = data.get("m_Id")
        except (OSError, json.JSONDecodeError):
            existing_uuid = None

    label = f"auto-backup before restore {datetime.now(timezone.utc).isoformat()}"

    if existing_uuid:
        snap = await create_snapshot(session, server, existing_uuid, label)
        return snap.snapshot_id

    size = await to_thread.run_sync(_dir_size, target_dir)
    check_free_space(settings.profiles_dir, size)
    manifest_fields: dict[str, Any] = {
        "source_save_uuid": None,
        "playthrough_nr": None,
        "save_point_nr": None,
        "mission_resource": None,
        "game_version": None,
        "saved_at_unix": None,
        "playtime_seconds": None,
        "uncompressed_size_bytes": size,
        "scenario_game_id": server.scenario_game_id,
        "mods": await _mods_snapshot(session, server),
        "scenario_dir": None,
        "playthrough_dir_name": None,
        "save_point_dir_name": None,
    }
    snap = await _write_snapshot(server.id, target_dir, label, manifest_fields)
    return snap.snapshot_id


def _inspect_uploaded_archive(archive_path: Path) -> dict[str, Any]:
    """Best-effort read of an externally-created archive's manifest fields.

    Only trusts what a single-rooted archive containing a ``meta-info.json``
    directly under that root can tell us; anything else (multi-rooted, no
    meta-info.json, unreadable) leaves those fields absent -> ``None`` in the
    caller's manifest. Never raises; ``store_upload`` already validated the
    archive's structural safety before this runs.
    """
    result: dict[str, Any] = {}
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            names = [n for n in tf.getnames() if n not in ("", ".")]
            top_level = {n.split("/", 1)[0] for n in names}
            if len(top_level) == 1:
                root_name = next(iter(top_level))
                result["save_point_dir_name"] = root_name
                meta_name = f"{root_name}/{_META_FILENAME}"
                try:
                    member = tf.getmember(meta_name)
                except KeyError:
                    member = None
                if member is not None:
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        try:
                            data = json.loads(extracted.read().decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            data = {}
                        result["source_save_uuid"] = data.get("m_Id")
                        result["mission_resource"] = data.get("m_sMissionResource")
                        result["game_version"] = data.get("m_sGameVersion")
                        saved_at_unix = data.get("m_iSavedAtUnix")
                        if isinstance(saved_at_unix, (int, float)):
                            result["saved_at_unix"] = int(saved_at_unix)
                        result["playtime_seconds"] = data.get("m_iPlaytimeSeconds")
                        sp_nr = data.get("m_iSavePointNr")
                        if isinstance(sp_nr, int):
                            result["save_point_nr"] = sp_nr
                        pt_nr = data.get("m_iPlaythroughNr")
                        if isinstance(pt_nr, int):
                            result["playthrough_nr"] = pt_nr
            result["uncompressed_size_bytes"] = sum(m.size for m in tf.getmembers() if m.isfile())
    except (tarfile.TarError, OSError):
        return {}
    return result


# ---------------------------------------------------------------------- CRUD
async def create_snapshot(
    session: AsyncSession, server: Server, save_uuid: str, label: str
) -> SnapshotOut:
    """Snapshot the save point with uuid ``save_uuid`` for ``server``.

    Raises :class:`SaveNotFoundError` if no such save point is discoverable,
    :class:`SnapshotCapExceededError` if the server is already at
    ``settings.save_snapshot_max_per_server`` (nothing is deleted), and
    propagates the 409 ``HTTPException`` from ``mods.freespace.check_free_space``
    when the projected write does not fit.
    """
    save_point, scenario_dir_name, playthrough_dir_name, save_point_dir_name = (
        await _locate_save_point(session, server, save_uuid)
    )

    existing = await list_snapshots(server)
    cap = int(settings.save_snapshot_max_per_server)
    if len(existing) >= cap:
        raise SnapshotCapExceededError(
            f"server {server.id} already has {len(existing)} snapshot(s), at the "
            f"cap of {cap} (SAVE_SNAPSHOT_MAX_PER_SERVER)"
        )

    check_free_space(settings.profiles_dir, save_point.size_bytes)

    base = resolve_safe_path(server.id, None)
    save_dir = base / save_point.rel_path

    manifest_fields: dict[str, Any] = {
        "source_save_uuid": save_uuid,
        "playthrough_nr": save_point.playthrough_nr,
        "save_point_nr": save_point.save_point_nr,
        "mission_resource": save_point.mission_resource,
        "game_version": save_point.game_version,
        "saved_at_unix": int(save_point.saved_at.timestamp()) if save_point.saved_at else None,
        "playtime_seconds": save_point.playtime_seconds,
        "uncompressed_size_bytes": save_point.size_bytes,
        "scenario_game_id": server.scenario_game_id,
        "mods": await _mods_snapshot(session, server),
        "scenario_dir": scenario_dir_name,
        "playthrough_dir_name": playthrough_dir_name,
        "save_point_dir_name": save_point_dir_name,
    }
    return await _write_snapshot(server.id, save_dir, label, manifest_fields)


async def list_snapshots(server: Server) -> list[SnapshotOut]:
    """All snapshots for ``server``, newest first. ``[]`` when it has none."""
    snapshots_dir = _snapshots_dir(server.id)
    if not snapshots_dir.is_dir():
        return []

    def _read_all() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in snapshots_dir.glob("*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    manifests = await to_thread.run_sync(_read_all)
    items = [_manifest_to_out(m) for m in manifests]
    items.sort(key=lambda s: s.created_at, reverse=True)
    return items


async def rename_snapshot(server: Server, snapshot_id: str, label: str) -> SnapshotOut:
    """Update a snapshot's ``label`` in place. Raises :class:`SnapshotNotFoundError`
    when the manifest does not exist."""
    manifest_path = _manifest_path(server.id, snapshot_id)

    def _update() -> dict[str, Any]:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} not found") from exc
        data["label"] = label
        _write_manifest_file(manifest_path, data)
        return data

    data = await to_thread.run_sync(_update)
    return _manifest_to_out(data)


async def delete_snapshot(server: Server, snapshot_id: str) -> None:
    """Remove a snapshot's archive + manifest. Idempotent: missing files are
    not an error."""
    archive_path = _archive_path(server.id, snapshot_id)
    manifest_path = _manifest_path(server.id, snapshot_id)

    def _delete() -> None:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)

    await to_thread.run_sync(_delete)


async def restore_snapshot(
    session: AsyncSession, server: Server, snapshot_id: str, arm: bool
) -> RestoreResult:
    """Extract snapshot ``snapshot_id`` back to its original save-point location.

    If a save point already exists there, it is snapshotted first (never
    silently discarded). When ``arm`` is ``True``, pins ``server`` to the
    restored save (caller commits). Every path written under the save tree is
    resolved via :func:`resolve_safe_path` — the manifest's recorded directory
    names are never trusted blindly.
    """
    manifest_path = _manifest_path(server.id, snapshot_id)
    archive_path = _archive_path(server.id, snapshot_id)
    if not manifest_path.is_file() or not archive_path.is_file():
        raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    scenario_dir_name: str | None = manifest.get("scenario_dir")
    playthrough_dir_name: str | None = manifest.get("playthrough_dir_name")
    save_point_dir_name: str | None = manifest.get("save_point_dir_name")

    if not scenario_dir_name and manifest.get("mission_resource"):
        for scenario in await saves.discover(session, server):
            if scenario.mission_resource == manifest["mission_resource"]:
                scenario_dir_name = scenario.scenario_dir
                break
    if not playthrough_dir_name and manifest.get("playthrough_nr") is not None:
        playthrough_dir_name = f"playthrough{int(manifest['playthrough_nr']):03d}"
    if not save_point_dir_name and manifest.get("save_point_nr") is not None:
        save_point_dir_name = f"savepoint{int(manifest['save_point_nr']):03d}"

    if not scenario_dir_name or not playthrough_dir_name or not save_point_dir_name:
        raise SaveNotFoundError(
            f"snapshot {snapshot_id!r} does not carry enough information to "
            "determine where to restore it"
        )

    playthrough_rel = f"profile/.save/game/{scenario_dir_name}/{playthrough_dir_name}"
    target_rel = f"{playthrough_rel}/{save_point_dir_name}"
    target_dir = resolve_safe_path(server.id, target_rel)

    auto_backup_id: str | None = None
    if target_dir.exists():
        auto_backup_id = await _auto_backup_existing(session, server, target_dir)

    check_free_space(settings.profiles_dir, manifest.get("uncompressed_size_bytes"))

    def _extract() -> None:
        with open(archive_path, "rb") as fh:
            validate_archive(fh)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        resolve_safe_path(server.id, playthrough_rel).mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf:
                dest = resolve_safe_path(server.id, f"{playthrough_rel}/{member.name}")
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                # Any other member type is already rejected by validate_archive.

    await to_thread.run_sync(_extract)

    restored_uuid = manifest.get("source_save_uuid")
    if arm:
        server.save_mode = "pinned"
        server.save_pinned_uuid = restored_uuid

    return RestoreResult(
        snapshot_id=snapshot_id,
        restored_uuid=restored_uuid,
        target_rel_path=target_rel,
        armed=arm,
        auto_backup_snapshot_id=auto_backup_id,
    )


# ------------------------------------------------------------------- upload
def _check_member_path(name: str) -> None:
    if not name or name in (".",):
        return
    if name.startswith("/") or PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive:
        raise InvalidArchiveError(f"absolute member path is not allowed: {name!r}")
    unified = name.replace("\\", "/")
    segments = [s for s in unified.split("/") if s not in ("", ".")]
    if any(s == ".." for s in segments):
        raise InvalidArchiveError(f"'..' member path is not allowed: {name!r}")


def validate_archive(fileobj: BinaryIO) -> None:
    """Inspect ``fileobj`` (a tar.gz) member-by-member; raise
    :class:`InvalidArchiveError` on the FIRST bad member found.

    Checked in order, per member: absolute path, ``..`` path segment, symlink
    or hardlink, device/fifo/char-special; then whole-archive running totals:
    member count over :data:`_MAX_ARCHIVE_MEMBERS`, uncompressed size over
    ``settings.save_upload_max_uncompressed_bytes``. Sync and validate-only —
    never extracts; caller extracts only after this returns cleanly.
    """
    try:
        fileobj.seek(0)
    except (AttributeError, OSError):
        pass

    count = 0
    total_size = 0
    cap_bytes = int(settings.save_upload_max_uncompressed_bytes)
    try:
        with tarfile.open(fileobj=fileobj, mode="r:gz") as tf:
            for member in tf:
                _check_member_path(member.name)
                if member.issym() or member.islnk():
                    raise InvalidArchiveError(
                        f"symlink/hardlink members are not allowed: {member.name!r}"
                    )
                if member.isdev():
                    raise InvalidArchiveError(
                        f"device/fifo/char-special members are not allowed: {member.name!r}"
                    )
                count += 1
                if count > _MAX_ARCHIVE_MEMBERS:
                    raise InvalidArchiveError(
                        f"archive has more than {_MAX_ARCHIVE_MEMBERS} members"
                    )
                total_size += max(member.size, 0)
                if total_size > cap_bytes:
                    raise InvalidArchiveError(
                        f"archive uncompressed size exceeds the {cap_bytes} byte cap"
                    )
    except tarfile.TarError as exc:
        raise InvalidArchiveError(f"not a valid tar.gz archive: {exc}") from exc
    finally:
        try:
            fileobj.seek(0)
        except (AttributeError, OSError):
            pass


async def store_upload(
    session: AsyncSession,
    server: Server,
    fileobj: BinaryIO,
    label: str,
    restore_now: bool,
    arm: bool,
) -> SnapshotOut:
    """Validate, then store, an externally-supplied snapshot archive.

    ``validate_archive(fileobj)`` runs FIRST; on failure nothing is stored or
    restored — the exception propagates untouched. Manifest fields the archive
    doesn't reveal (no reliable original hierarchy) are ``None``. When
    ``restore_now`` is ``True``, immediately restores what was just stored.
    """
    await to_thread.run_sync(validate_archive, fileobj)
    try:
        fileobj.seek(0)
    except (AttributeError, OSError):
        pass

    snapshot_id = uuid4().hex
    snapshots_dir = _snapshots_dir(server.id)
    archive_path = snapshots_dir / f"{snapshot_id}.tar.gz"
    manifest_path = snapshots_dir / f"{snapshot_id}.json"

    def _store() -> dict[str, Any]:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        tmp = archive_path.with_name(f"{archive_path.name}.tmp-{os.getpid()}")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fileobj, out)
        os.replace(tmp, archive_path)
        return _inspect_uploaded_archive(archive_path)

    inspected = await to_thread.run_sync(_store)
    mods = await _mods_snapshot(session, server)

    manifest: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_save_uuid": inspected.get("source_save_uuid"),
        "playthrough_nr": inspected.get("playthrough_nr"),
        "save_point_nr": inspected.get("save_point_nr"),
        "mission_resource": inspected.get("mission_resource"),
        "game_version": inspected.get("game_version"),
        "saved_at_unix": inspected.get("saved_at_unix"),
        "playtime_seconds": inspected.get("playtime_seconds"),
        "uncompressed_size_bytes": inspected.get("uncompressed_size_bytes"),
        "scenario_game_id": server.scenario_game_id,
        "mods": mods,
        "scenario_dir": inspected.get("scenario_dir"),
        "playthrough_dir_name": inspected.get("playthrough_dir_name"),
        "save_point_dir_name": inspected.get("save_point_dir_name"),
        "archive_size_bytes": archive_path.stat().st_size,
    }
    await to_thread.run_sync(_write_manifest_file, manifest_path, manifest)
    out = _manifest_to_out(manifest)

    if restore_now:
        await restore_snapshot(session, server, snapshot_id, arm=arm)

    return out
