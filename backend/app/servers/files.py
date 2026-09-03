"""Path-safe filesystem primitives scoped to one server's profile directory.

Everything the profile-file-manager API does goes through :func:`resolve_safe_path`
first, so no route ever builds a ``Path`` from caller input itself. The resolver
is a generalisation of ``mods/logview.py``'s hardcoded ``logs/console.log``
guard: reject dangerous input *textually* (absolute paths, ``..`` segments, NUL
bytes, excluded top-level dirs) before touching the filesystem, then
``resolve(strict=False)`` and re-check ancestry ``profiles_root ⊃ profile_dir ⊃
candidate``, then walk every path component and refuse if any of them is a
symlink (inside-the-profile targets included — "never follow" is unconditional).

No DB, no ORM: this is pure I/O against the ``PROFILES_DIR/{server_id}/`` bind
mount.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from ..core.config import settings
# Reuse logview's ancestry check + server-id validation verbatim — do not fork.
from ..mods.logview import _is_relative_to, _normalise_server_id

EntryType = Literal["file", "dir", "symlink"]

# Filtered out of listings here, and rejected as a path prefix by the resolver so
# no route can reach into them by spelling the path out. ``logs/`` has its own
# Console/Log tab; ``addons_tmp/`` is engine scratch recreated every start.
EXCLUDED_TOP_LEVEL = frozenset({"logs", "addons_tmp"})

_TEXT_SNIFF_BYTES = 8192


class UnsafePath(Exception):
    """Caller-supplied path failed a safety check. The API layer maps it to 400."""


@dataclass(frozen=True)
class FileEntry:
    name: str
    rel_path: str
    type: EntryType
    size: int
    mtime: datetime | None
    is_text: bool
    is_json: bool


# --------------------------------------------------------------------------- path

def _looks_absolute(raw: str, unified: str) -> bool:
    """True for POSIX-absolute, drive-qualified, UNC, or root-anchored input.

    ``Path(base) / "/etc/passwd"`` silently discards ``base`` in pathlib, so this
    check is load-bearing. ``PureWindowsPath`` catches ``C:\\...`` and ``\\\\host``
    on every platform; its ``.root`` catches a leading slash even where the
    native flavour would not.
    """
    if PurePosixPath(unified).is_absolute():
        return True
    win = PureWindowsPath(raw)
    if win.is_absolute() or win.drive or win.root:
        return True
    if Path(raw).is_absolute():
        return True
    return False


def _split_segments(rel_path: str | None) -> list[str]:
    """Reject dangerous input textually and return clean path segments.

    Empty (root) is allowed. ``..`` anywhere, NUL bytes, absolute/drive/UNC
    paths, and an excluded top-level first segment all raise :class:`UnsafePath`.
    """
    raw = "" if rel_path is None else str(rel_path)
    if "\x00" in raw:
        raise UnsafePath("path contains a NUL byte")

    raw = raw.strip()
    unified = raw.replace("\\", "/")
    if raw and _looks_absolute(raw, unified):
        raise UnsafePath(f"absolute paths are not allowed: {raw!r}")

    segments = [s for s in unified.split("/") if s not in ("", ".")]
    if any(s == ".." for s in segments):
        raise UnsafePath(f"'..' segments are not allowed: {raw!r}")
    if segments and segments[0] in EXCLUDED_TOP_LEVEL:
        raise UnsafePath(f"{segments[0]!r} is not browsable through this API")
    return segments


def _reject_symlink_components(base: Path, segments: list[str]) -> None:
    """Walk the *unresolved* path; refuse if any component is a symlink."""
    cur = base
    for seg in segments:
        cur = cur / seg
        try:
            is_link = cur.is_symlink()
        except OSError as exc:  # pragma: no cover - defensive
            raise UnsafePath(f"cannot inspect path component: {seg!r}") from exc
        if is_link:
            raise UnsafePath(f"symlinked path component is never followed: {seg!r}")


def resolve_safe_path(server_id: int, rel_path: str | None) -> Path:
    """Resolve ``rel_path`` under this server's profile dir or raise ``UnsafePath``.

    ``settings.profiles_dir`` is resolved *per call* (never cached at import) so
    monkeypatched tests do not leak into each other.
    """
    server_id = _normalise_server_id(server_id)
    profiles_root = Path(settings.profiles_dir).resolve(strict=False)
    resolved_profile_dir = settings.profile_dir(server_id).resolve(strict=False)

    segments = _split_segments(rel_path)
    candidate = resolved_profile_dir.joinpath(*segments) if segments else resolved_profile_dir
    resolved_candidate = candidate.resolve(strict=False)

    if not _is_relative_to(resolved_profile_dir, profiles_root):
        raise UnsafePath("profile directory escapes the profiles root")
    if not _is_relative_to(resolved_candidate, resolved_profile_dir):
        raise UnsafePath(f"path escapes the profile directory: {rel_path!r}")

    _reject_symlink_components(resolved_profile_dir, segments)
    return resolved_candidate


# ------------------------------------------------------------------------ sniffs

def _sniff_is_text(path: Path) -> bool:
    """Best-effort: read ~8 KiB, false on a NUL byte or a UTF-8 decode error."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _sniff_is_json(path: Path) -> bool:
    """``json.loads`` the whole file. Callers gate this behind the size cap."""
    try:
        with path.open("rb") as fh:
            raw = fh.read()
        json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _make_entry(path: Path, rel_path: str, *, max_edit_bytes: int) -> FileEntry:
    name = path.name
    if path.is_symlink():
        return FileEntry(name, rel_path, "symlink", 0, None, False, False)
    try:
        st = path.stat()
    except OSError:
        return FileEntry(name, rel_path, "file", 0, None, False, False)
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if path.is_dir():
        return FileEntry(name, rel_path, "dir", 0, mtime, False, False)
    size = st.st_size
    is_text = size <= max_edit_bytes and _sniff_is_text(path)
    is_json = is_text and path.suffix.lower() == ".json" and _sniff_is_json(path)
    return FileEntry(name, rel_path, "file", size, mtime, is_text, is_json)


# ------------------------------------------------------------------------ listing

def list_dir(server_id: int, rel_path: str | None = None) -> list[FileEntry]:
    """Non-recursive listing. ``[]`` (not an error) when the path does not exist.

    Dirs first, then files/symlinks, each sorted case-insensitively by name.
    ``logs/`` and ``addons_tmp/`` are dropped from the profile root.
    """
    target = resolve_safe_path(server_id, rel_path)
    if not target.is_dir():
        return []
    base = resolve_safe_path(server_id, None)
    is_root = target == base
    max_edit = int(settings.files_max_edit_bytes)

    try:
        children = list(target.iterdir())
    except OSError:
        return []

    entries: list[FileEntry] = []
    for child in children:
        if is_root and child.name in EXCLUDED_TOP_LEVEL:
            continue
        rel = child.relative_to(base).as_posix()
        entries.append(_make_entry(child, rel, max_edit_bytes=max_edit))

    dirs = sorted((e for e in entries if e.type == "dir"), key=lambda e: e.name.lower())
    rest = sorted((e for e in entries if e.type != "dir"), key=lambda e: e.name.lower())
    return dirs + rest


# ------------------------------------------------------ resolver-first mutators

def read_text_file(server_id: int, rel_path: str) -> str:
    """Resolve then read as UTF-8 text."""
    return resolve_safe_path(server_id, rel_path).read_text(encoding="utf-8")


def write_text_file(server_id: int, rel_path: str, content: str) -> Path:
    """Resolve then write atomically (temp file in the same dir + ``os.replace``)."""
    target = resolve_safe_path(server_id, rel_path)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return target


def delete_path(server_id: int, rel_path: str) -> None:
    """Resolve then delete a file, symlink, or directory (recursive)."""
    target = resolve_safe_path(server_id, rel_path)
    if target == resolve_safe_path(server_id, None):
        raise UnsafePath("refusing to delete the profile root")
    if target.is_dir() and not target.is_symlink():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()


def make_dir(server_id: int, rel_path: str) -> Path:
    """Resolve then ``mkdir(parents=True, exist_ok=True)``."""
    target = resolve_safe_path(server_id, rel_path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def rename_path(server_id: int, rel_path: str, new_rel_path: str) -> Path:
    """Resolve both ends then move. Refuses if the destination already exists."""
    src = resolve_safe_path(server_id, rel_path)
    dst = resolve_safe_path(server_id, new_rel_path)
    root = resolve_safe_path(server_id, None)
    if src == root or dst == root:
        raise UnsafePath("refusing to rename the profile root")
    if dst.exists():
        raise UnsafePath(f"destination already exists: {new_rel_path!r}")
    if src.parent == dst.parent:
        os.replace(src, dst)
    else:
        import shutil

        shutil.move(str(src), str(dst))
    return dst


def build_archive_bytes(server_id: int) -> bytes:
    """A ``ZIP_DEFLATED`` archive of the whole profile dir, built in memory.

    Symlinks are skipped (never followed); the excluded top-level dirs are
    pruned. Returns a valid empty zip when the profile dir does not exist.
    """
    base = resolve_safe_path(server_id, None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if base.is_dir():
            for root, dirs, files in os.walk(base, followlinks=False):
                root_path = Path(root)
                rel_root = root_path.relative_to(base)
                if rel_root == Path("."):
                    dirs[:] = [d for d in dirs if d not in EXCLUDED_TOP_LEVEL]
                dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                for fname in files:
                    fpath = root_path / fname
                    if fpath.is_symlink():
                        continue
                    zf.write(fpath, (rel_root / fname).as_posix())
    return buf.getvalue()
