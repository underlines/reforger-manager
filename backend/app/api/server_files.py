"""Per-server profile files: browse, read small text files, download any file
and the whole profile dir as a zip (S13).

GET    /api/servers/{id}/files              list a directory
GET    /api/servers/{id}/files/content      read a small text file (editable marker)
GET    /api/servers/{id}/files/download     raw bytes of any file
GET    /api/servers/{id}/files/archive      zip of the whole profile dir

Mutating routes (PUT /content, DELETE /, POST /mkdir, /rename, /upload) are
refused with 409 while that server is running.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import get_session
from ..core.security import get_current_user
from ..schemas.server_files import (
    DirListingOut,
    FileContentOut,
    FileEntryOut,
    MkdirIn,
    RenameIn,
    WriteContentIn,
)
from ..servers.files import (
    UnsafePath,
    _sniff_is_json,
    _sniff_is_text,
    build_archive_bytes,
    list_dir,
    read_text_file,
    resolve_safe_path,
    write_text_file,
)
from ..servers.supervisor import supervisor
from .servers import _load

router = APIRouter(
    prefix="/servers/{server_id}/files",
    tags=["server-files"],
    dependencies=[Depends(get_current_user)],
)


def _guard(exc: UnsafePath) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _refuse_while_running(server_id: int) -> None:
    if supervisor.active_server_id == server_id and supervisor.is_running():
        raise HTTPException(
            status_code=409, detail="stop the server before editing its profile files"
        )


@router.get("", response_model=DirListingOut)
async def list_server_files(
    server_id: int,
    path: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> DirListingOut:
    await _load(session, server_id)
    try:
        entries = [FileEntryOut(**vars(e)) for e in list_dir(server_id, path)]
    except UnsafePath as exc:
        raise _guard(exc)
    return DirListingOut(server_id=server_id, path=path or "", entries=entries)


@router.get("/content", response_model=FileContentOut)
async def get_file_content(
    server_id: int,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> FileContentOut:
    await _load(session, server_id)
    try:
        target = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    size = target.stat().st_size
    parent_rel = target.parent.relative_to(settings.profile_dir(server_id))
    entry = None
    if str(parent_rel) == ".":
        try:
            listing = list_dir(server_id, None)
        except UnsafePath as exc:
            raise _guard(exc)
    else:
        try:
            listing = list_dir(server_id, str(parent_rel))
        except UnsafePath as exc:
            raise _guard(exc)
    for e in listing:
        if e.name == target.name:
            entry = e
            break
    if entry is not None and entry.is_text and size <= settings.files_max_edit_bytes:
        return FileContentOut(
            path=path,
            size=size,
            editable=True,
            is_json=entry.is_json,
            content=read_text_file(server_id, path),
        )
    return FileContentOut(path=path, size=size, editable=False, is_json=False, content=None)


@router.get("/download")
async def download_file(
    server_id: int,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _load(session, server_id)
    try:
        target = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    data = target.read_bytes()
    safe = target.name.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@router.get("/archive")
async def download_profile_archive(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    await _load(session, server_id)
    return Response(
        content=build_archive_bytes(server_id),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="server-{server_id}-profile.zip"'},
    )

def _entry_from_listing(server_id: int, parent: Path, target: Path) -> FileEntryOut:
    rel_parent = None if parent == resolve_safe_path(server_id, None) else str(
        parent.relative_to(resolve_safe_path(server_id, None))
    )
    for e in list_dir(server_id, rel_parent):
        if e.name == target.name:
            return FileEntryOut(**vars(e))
    raise HTTPException(status_code=404, detail="entry not found after write")


def _entry_out(path: Path, rel_path: str) -> FileEntryOut:
    if path.is_symlink():
        entry = FileEntryOut(name=path.name, rel_path=rel_path, type="symlink", size=0, mtime=None, is_text=False, is_json=False)
    else:
        try:
            st = path.stat()
        except OSError:
            return FileEntryOut(name=path.name, rel_path=rel_path, type="file", size=0, mtime=None, is_text=False, is_json=False)
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if path.is_dir():
            entry = FileEntryOut(name=path.name, rel_path=rel_path, type="dir", size=0, mtime=mtime, is_text=False, is_json=False)
        else:
            is_text = st.st_size <= settings.files_max_edit_bytes and _sniff_is_text(path)
            is_json = is_text and path.suffix.lower() == ".json" and _sniff_is_json(path)
            entry = FileEntryOut(name=path.name, rel_path=rel_path, type="file", size=st.st_size, mtime=mtime, is_text=is_text, is_json=is_json)
    return entry


@router.put("/content", response_model=FileEntryOut)
async def write_file_content(
    server_id: int,
    body: WriteContentIn,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> FileEntryOut:
    await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        resolve_safe_path(server_id, None).mkdir(parents=True, exist_ok=True)
        target = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if not target.parent.is_dir():
        raise HTTPException(status_code=400, detail="parent directory does not exist; create it first")
    if target.suffix.lower() == ".json":
        try:
            json.loads(body.content)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}")
    try:
        write_text_file(server_id, path, body.content)
    except UnsafePath as exc:
        raise _guard(exc)
    return _entry_from_listing(server_id, target.parent, target)


@router.delete("", status_code=204)
async def delete_server_file(
    server_id: int,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        target = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if target == resolve_safe_path(server_id, None):
        raise HTTPException(status_code=400, detail="cannot delete the profile root")
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        target = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()
    return Response(status_code=204)


@router.post("/mkdir", response_model=FileEntryOut)
async def make_directory(
    server_id: int,
    body: MkdirIn,
    session: AsyncSession = Depends(get_session),
) -> FileEntryOut:
    await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        created = resolve_safe_path(server_id, body.path)
        created.mkdir(parents=True, exist_ok=True)
    except UnsafePath as exc:
        raise _guard(exc)
    rel = created.relative_to(resolve_safe_path(server_id, None)).as_posix()
    return _entry_out(created, rel)


@router.post("/rename")
async def rename_server_file(
    server_id: int,
    body: RenameIn,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        src = resolve_safe_path(server_id, body.path)
        dst = resolve_safe_path(server_id, body.new_path)
    except UnsafePath as exc:
        raise _guard(exc)
    root = resolve_safe_path(server_id, None)
    if not src.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if src == root or dst == root:
        raise HTTPException(status_code=400, detail="cannot rename the profile root")
    if dst.exists():
        raise HTTPException(status_code=400, detail="destination already exists")
    if dst != src and dst.is_relative_to(src):
        raise HTTPException(status_code=400, detail="cannot move a directory into itself")
    os.replace(src, dst)
    return {"path": body.new_path}


@router.post("/upload", response_model=list[FileEntryOut])
async def upload_files(
    server_id: int,
    files: list[UploadFile] = File(...),
    path: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[FileEntryOut]:
    await _load(session, server_id)
    _refuse_while_running(server_id)
    try:
        resolve_safe_path(server_id, None).mkdir(parents=True, exist_ok=True)
        target_dir = resolve_safe_path(server_id, path)
    except UnsafePath as exc:
        raise _guard(exc)
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="target directory does not exist")
    root = resolve_safe_path(server_id, None)
    max_bytes = settings.files_max_upload_bytes
    written: list[FileEntryOut] = []
    for f in files:
        if not f.filename:
            raise HTTPException(status_code=400, detail="file has no name")
        if "/" in f.filename or "\\" in f.filename:
            raise HTTPException(status_code=400, detail="filename must not contain path separators")
        safe_rel = f"{path or ''}/{Path(f.filename).name}".lstrip("/")
        try:
            dest = resolve_safe_path(server_id, safe_rel)
        except UnsafePath as exc:
            raise _guard(exc)
        total = 0
        tmp = dest.with_name(f".{dest.name}.upload-{os.getpid()}")
        try:
            with tmp.open("wb") as fh:
                while chunk := await f.read(1 << 20):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=413, detail="file too large")
                    fh.write(chunk)
            os.replace(tmp, dest)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            await f.close()
        written.append(_entry_out(dest, dest.relative_to(root).as_posix()))
    return written
