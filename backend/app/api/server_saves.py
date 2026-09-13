"""Per-server save-game discovery + manager-owned snapshots (S14).

GET    /servers/{id}/saves                                   discover + selection + snapshots
POST   /servers/{id}/saves/{save_uuid}/arm                    pin server to a save point
POST   /servers/{id}/saves/arm-fresh                          pin to a fresh (no-save) start
DELETE /servers/{id}/saves/selection                          clear selection back to "latest"
DELETE /servers/{id}/saves/{save_uuid}                        delete one save point
DELETE /servers/{id}/saves/playthroughs/{scenario_dir}/{nr}   delete a whole playthrough
GET    /servers/{id}/saves/{save_uuid}/download               tar.gz of one save point
POST   /servers/{id}/saves/{save_uuid}/snapshot               create a manager snapshot
PATCH  /servers/{id}/saves/snapshots/{snapshot_id}            rename a snapshot
DELETE /servers/{id}/saves/snapshots/{snapshot_id}            delete a snapshot
GET    /servers/{id}/saves/snapshots/{snapshot_id}/download   stream the snapshot archive
POST   /servers/{id}/saves/snapshots/{snapshot_id}/restore    restore a snapshot
POST   /servers/{id}/saves/snapshots/upload                   upload an externally-made snapshot

Arming routes (arm / arm-fresh / clear-selection) are explicitly NOT
running-guarded — the whole point is to be settable while the server is up.
Every other mutating route is refused with 409 while that server is running,
same as ``server_files.py``.

Route-ordering note: ``DELETE /selection`` must be registered before
``DELETE /{save_uuid}`` — both are one path segment, and Starlette matches
registration order, so the literal route would otherwise be shadowed by the
parameterized one.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_session
from ..core.security import get_current_user
from ..schemas.saves import (
    ArmIn,
    LabelIn,
    RestoreIn,
    RestoreResult,
    SavesListOut,
    SelectionOut,
    SnapshotOut,
)
from ..servers import save_snapshots
from ..servers import saves
from ..servers.files import UnsafePath, resolve_safe_path
from ..servers.save_snapshots import (
    InvalidArchiveError,
    SaveNotFoundError,
    SnapshotCapExceededError,
    SnapshotNotFoundError,
)
from ..servers.supervisor import supervisor
from .servers import _load

router = APIRouter(
    prefix="/servers/{server_id}/saves",
    tags=["server-saves"],
    dependencies=[Depends(get_current_user)],
)


def _refuse_while_running(server_id: int) -> None:
    if supervisor.active_server_id == server_id and supervisor.is_running():
        raise HTTPException(
            status_code=409, detail="stop the server before changing its saves"
        )


def _guard(exc: UnsafePath) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _clear_selection(server) -> None:
    server.save_mode = "latest"
    server.save_pinned_uuid = None
    server.save_selection_sticky = False


async def _find_save_point(session: AsyncSession, server, save_uuid: str):
    for scenario in await saves.discover(session, server):
        for playthrough in scenario.playthroughs:
            for save_point in playthrough.save_points:
                if save_point.uuid == save_uuid:
                    return save_point
    return None


def _tar_dir_bytes(path: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(path, arcname=arcname, filter=_skip_symlinks)
    return buf.getvalue()


def _skip_symlinks(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if tarinfo.issym() or tarinfo.islnk():
        return None
    return tarinfo


# ------------------------------------------------------------------------ list
@router.get("", response_model=SavesListOut)
async def list_saves(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> SavesListOut:
    server = await _load(session, server_id)
    scenarios = await saves.discover(session, server)
    snapshots = await save_snapshots.list_snapshots(server)
    return SavesListOut(
        scenarios=scenarios,
        snapshots=snapshots,
        selection=SelectionOut(
            mode=server.save_mode,
            pinned_uuid=server.save_pinned_uuid,
            sticky=server.save_selection_sticky,
        ),
    )


# --------------------------------------------------------------------- arming
@router.post("/{save_uuid}/arm", response_model=SelectionOut)
async def arm_save(
    server_id: int,
    save_uuid: str,
    body: ArmIn,
    session: AsyncSession = Depends(get_session),
) -> SelectionOut:
    server = await _load(session, server_id)
    save_point = await _find_save_point(session, server, save_uuid)
    if save_point is None:
        raise HTTPException(status_code=400, detail=f"save point {save_uuid!r} not found")
    server.save_mode = "pinned"
    server.save_pinned_uuid = save_uuid
    server.save_selection_sticky = body.sticky
    await session.commit()
    return SelectionOut(mode=server.save_mode, pinned_uuid=server.save_pinned_uuid, sticky=server.save_selection_sticky)


@router.post("/arm-fresh", response_model=SelectionOut)
async def arm_fresh(
    server_id: int,
    body: ArmIn,
    session: AsyncSession = Depends(get_session),
) -> SelectionOut:
    server = await _load(session, server_id)
    server.save_mode = "fresh"
    server.save_pinned_uuid = None
    server.save_selection_sticky = body.sticky
    await session.commit()
    return SelectionOut(mode=server.save_mode, pinned_uuid=server.save_pinned_uuid, sticky=server.save_selection_sticky)


@router.delete("/selection", response_model=SelectionOut)
async def clear_selection(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> SelectionOut:
    server = await _load(session, server_id)
    _clear_selection(server)
    await session.commit()
    return SelectionOut(mode=server.save_mode, pinned_uuid=server.save_pinned_uuid, sticky=server.save_selection_sticky)


# ------------------------------------------------------------------- deletion
@router.delete("/{save_uuid}", status_code=204)
async def delete_save_point(
    server_id: int, save_uuid: str, session: AsyncSession = Depends(get_session)
) -> Response:
    server = await _load(session, server_id)
    _refuse_while_running(server_id)
    save_point = await _find_save_point(session, server, save_uuid)
    if save_point is None:
        raise HTTPException(status_code=404, detail=f"save point {save_uuid!r} not found")
    try:
        target = resolve_safe_path(server_id, save_point.rel_path)
    except UnsafePath as exc:
        raise _guard(exc)
    await to_thread.run_sync(_rmtree, target)
    if server.save_pinned_uuid == save_uuid:
        _clear_selection(server)
    await session.commit()
    return Response(status_code=204)


@router.delete("/playthroughs/{scenario_dir}/{playthrough_nr}", status_code=204)
async def delete_playthrough(
    server_id: int,
    scenario_dir: str,
    playthrough_nr: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    server = await _load(session, server_id)
    _refuse_while_running(server_id)
    scenarios = await saves.discover(session, server)
    target_playthrough = None
    for scenario in scenarios:
        if scenario.scenario_dir != scenario_dir:
            continue
        for playthrough in scenario.playthroughs:
            if playthrough.playthrough_nr == playthrough_nr:
                target_playthrough = playthrough
                break
    if target_playthrough is None:
        raise HTTPException(
            status_code=404,
            detail=f"playthrough {playthrough_nr} not found for scenario {scenario_dir!r}",
        )
    rel_path = f"profile/.save/game/{scenario_dir}/{target_playthrough.dir_name}"
    try:
        target = resolve_safe_path(server_id, rel_path)
    except UnsafePath as exc:
        raise _guard(exc)
    await to_thread.run_sync(_rmtree, target)
    if server.save_pinned_uuid is not None and any(
        sp.uuid == server.save_pinned_uuid for sp in target_playthrough.save_points
    ):
        _clear_selection(server)
    await session.commit()
    return Response(status_code=204)


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


# ------------------------------------------------------------------- download
@router.get("/{save_uuid}/download")
async def download_save_point(
    server_id: int, save_uuid: str, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    server = await _load(session, server_id)
    save_point = await _find_save_point(session, server, save_uuid)
    if save_point is None:
        raise HTTPException(status_code=404, detail=f"save point {save_uuid!r} not found")
    try:
        target = resolve_safe_path(server_id, save_point.rel_path)
    except UnsafePath as exc:
        raise _guard(exc)
    data = await to_thread.run_sync(_tar_dir_bytes, target, save_point.dir_name)
    filename = f"{save_point.dir_name}.tar.gz"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------- snapshots
@router.post("/{save_uuid}/snapshot", response_model=SnapshotOut)
async def create_snapshot(
    server_id: int,
    save_uuid: str,
    body: LabelIn,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    server = await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        snapshot = await save_snapshots.create_snapshot(session, server, save_uuid, body.label)
    except SaveNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SnapshotCapExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await session.commit()
    return snapshot


@router.patch("/snapshots/{snapshot_id}", response_model=SnapshotOut)
async def rename_snapshot(
    server_id: int,
    snapshot_id: str,
    body: LabelIn,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    server = await _load(session, server_id)
    try:
        return await save_snapshots.rename_snapshot(server, snapshot_id, body.label)
    except SnapshotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/snapshots/{snapshot_id}", status_code=204)
async def delete_snapshot(
    server_id: int, snapshot_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    server = await _load(session, server_id)
    await save_snapshots.delete_snapshot(server, snapshot_id)
    return Response(status_code=204)


@router.get("/snapshots/{snapshot_id}/download")
async def download_snapshot(
    server_id: int, snapshot_id: str, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    await _load(session, server_id)
    archive_path = settings.profile_dir(server_id) / "snapshots" / f"{snapshot_id}.tar.gz"
    if not archive_path.is_file():
        raise HTTPException(status_code=404, detail=f"snapshot {snapshot_id!r} not found")
    return FileResponse(
        path=archive_path,
        media_type="application/gzip",
        filename=f"{snapshot_id}.tar.gz",
    )


@router.post("/snapshots/{snapshot_id}/restore", response_model=RestoreResult)
async def restore_snapshot(
    server_id: int,
    snapshot_id: str,
    body: RestoreIn,
    session: AsyncSession = Depends(get_session),
) -> RestoreResult:
    server = await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        result = await save_snapshots.restore_snapshot(session, server, snapshot_id, body.arm)
    except SnapshotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SaveNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return result


@router.post("/snapshots/upload", response_model=SnapshotOut)
async def upload_snapshot(
    server_id: int,
    file: UploadFile = File(...),
    label: str = Form(...),
    restore_now: bool = Form(False),
    arm: bool = Form(True),
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    server = await _load(session, server_id)
    _refuse_while_running(server_id)
    raw = await file.read()
    try:
        fileobj = io.BytesIO(raw)
        snapshot = await save_snapshots.store_upload(
            session, server, fileobj, label, restore_now, arm
        )
    except InvalidArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await file.close()
    await session.commit()
    return snapshot
