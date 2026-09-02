"""Local disk scanner for the Reforger addon cache.

Root: ``<MODS_DIR>/reforger/addons`` — the layout the engine itself expects
(``<mods>/reforger/addons/<Name_GUID>/``). Each immediate subdirectory is one
mod; a directory **without a ``meta`` file is skipped** (that is how the sibling
``saves/`` directory is excluded).

Everything here is pure/offline. Per-directory failures never propagate: a bad
directory is recorded in the returned ``problems`` list and the scan continues.

What each file gives us (verified against the real cache 2026-09-02):

* ``meta``               UTF-8 **BOM** JSON, wrapped in a top-level ``"meta"``
                         object. ``id`` == the directory GUID; ``version`` at
                         ``versions[0].version``; ``size`` at
                         ``versions[0].package.totalSize``. Its ``scenarios`` /
                         ``dependencies`` / ``gameVersion`` are always empty
                         locally — ignored.
* ``addon.gproj``        ENFUSION ``GameProject { ... }`` (NOT JSON). The
                         ``Dependencies { ... }`` block (may span several lines)
                         is the authoritative local dependency list — bare
                         quoted 16-hex GUIDs, no names. File may be absent or
                         empty.
* ``resourceDatabase.rdb`` binary. A ``strings``-equivalent scan (printable
                         ASCII runs) + regex for ``Missions/....conf`` yields the
                         scenario *paths*. The full ``game_id`` is built as
                         ``{<mod GUID>}<path>``. No RDBC record parser — that
                         needs undocumented-format RE for no benefit. (Note: the
                         API is authoritative for scenarios and returns the true
                         per-scenario resource GUID, which is often *not* the mod
                         GUID; this offline id is a best-effort fallback.)
* ``ServerData.json``    BOM JSON: ``id``, ``name``, ``revision.version`` — a
                         simpler version cross-check.
* ``thumbnail.png``      presence -> library tile.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import settings

logger = logging.getLogger("reforger.mods.scanner")

_GUID_RE = re.compile(r"[0-9A-Fa-f]{16}")
_GUID_QUOTED_RE = re.compile(r'"([0-9A-Fa-f]{16})"')
_DEP_BLOCK_RE = re.compile(r"Dependencies\s*\{(.*?)\}", re.DOTALL)
_GPROJ_GUID_RE = re.compile(r'\bGUID\s+"([0-9A-Fa-f]{16})"')
_GPROJ_ID_RE = re.compile(r'\bID\s+"([^"]*)"')
_GPROJ_TITLE_RE = re.compile(r'\bTITLE\s+"([^"]*)"')
# A scenario path token: "Missions/..../something.conf" appearing anywhere in a
# printable run. Kept deliberately permissive (matches the brief).
_MISSION_PATH_RE = re.compile(r"Missions/[^\s\"'\x00]*?\.conf")
_PRINTABLE_RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")


@dataclass
class ScannedMod:
    guid: str
    name: str | None
    summary: str | None
    tags: list[str]
    version: str | None
    size: int | None
    is_unlisted: bool
    is_deleted_local: bool
    dep_guids: list[str]
    scenarios: list[tuple[str, str]]  # (game_id, path)
    has_thumbnail: bool
    dir_name: str
    server_data_version: str | None = None


@dataclass
class ScanResult:
    mods: list[ScannedMod] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------- paths
def addons_root() -> Path:
    """``<MODS_DIR>/reforger/addons``."""
    return Path(settings.mods_dir) / "reforger" / "addons"


def resolve_addon_dir(guid: str) -> Path | None:
    """Locate the on-disk addon directory for ``guid`` inside :func:`addons_root`.

    Directory names are ``<Name>_<GUID>``; the GUID is the last 16-hex token.
    Returns ``None`` when the root is missing or no immediate subdirectory maps
    to ``guid``. Used by the storage view and ``DELETE /api/mods/{guid}/local``
    so file deletion is always anchored to a real directory under the addons
    root — never a computed path.
    """
    guid = guid.upper()
    root = addons_root()
    if not root.is_dir():
        return None
    for entry in root.iterdir():
        if entry.is_dir() and _guid_from_dir_name(entry.name) == guid:
            return entry
    return None


# ------------------------------------------------------------------- parsers
def parse_meta(mod_dir: Path) -> dict | None:
    """Read ``meta`` (utf-8-sig JSON, everything under the top-level ``meta``
    object). Returns a flat dict or ``None`` when the file is absent. Raises
    ``ValueError`` on malformed JSON / structure.
    """
    path = mod_dir / "meta"
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8-sig")
    doc = json.loads(raw)
    meta = doc.get("meta") if isinstance(doc, dict) else None
    if not isinstance(meta, dict):
        raise ValueError("meta file has no top-level 'meta' object")

    versions = meta.get("versions")
    v0 = versions[0] if isinstance(versions, list) and versions else {}
    if not isinstance(v0, dict):
        v0 = {}
    package = v0.get("package") if isinstance(v0.get("package"), dict) else {}

    tags = meta.get("tags")
    if not isinstance(tags, list):
        tags = []

    size = package.get("totalSize")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None

    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "summary": meta.get("summary"),
        "tags": [str(t) for t in tags],
        "unlisted": bool(meta.get("unlisted")) if "unlisted" in meta else None,
        "deleted": bool(meta.get("deleted")) if "deleted" in meta else None,
        "version": v0.get("version") or None,
        "size": size,
    }


def parse_gproj(mod_dir: Path) -> dict:
    """Parse ``addon.gproj`` (ENFUSION key/value, not JSON).

    Returns ``{"guid", "id", "title", "dep_guids"}``. Missing / empty / unreadable
    file -> ``dep_guids == []``.
    """
    result: dict = {"guid": None, "id": None, "title": None, "dep_guids": []}
    path = mod_dir / "addon.gproj"
    if not path.is_file():
        return result
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return result
    if not text.strip():
        return result

    m = _GPROJ_GUID_RE.search(text)
    if m:
        result["guid"] = m.group(1).upper()
    m = _GPROJ_ID_RE.search(text)
    if m:
        result["id"] = m.group(1)
    m = _GPROJ_TITLE_RE.search(text)
    if m:
        result["title"] = m.group(1)

    seen: list[str] = []
    for block in _DEP_BLOCK_RE.findall(text):
        for guid in _GUID_QUOTED_RE.findall(block):
            g = guid.upper()
            if g not in seen:
                seen.append(g)
    result["dep_guids"] = seen
    return result


def parse_scenarios(mod_dir: Path, mod_guid: str) -> list[tuple[str, str]]:
    """``strings``-equivalent scan of ``resourceDatabase.rdb`` + regex for
    ``Missions/....conf``. Returns ``[(game_id, path)]`` deduped, where
    ``game_id == "{" + mod_guid + "}" + path``. Missing file -> ``[]``.
    """
    path = mod_dir / "resourceDatabase.rdb"
    if not path.is_file():
        return []
    try:
        blob = path.read_bytes()
    except OSError:
        return []

    guid = mod_guid.upper()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for run in _PRINTABLE_RUN_RE.findall(blob):
        token = run.decode("ascii", "replace")
        for match in _MISSION_PATH_RE.findall(token):
            rel = match.lstrip("/")
            if rel in seen:
                continue
            seen.add(rel)
            out.append(("{" + guid + "}" + rel, rel))
    return out


def parse_serverdata(mod_dir: Path) -> dict | None:
    """Read ``ServerData.json`` (BOM JSON). Returns ``{"id", "name", "version"}``
    or ``None`` when absent / unparseable."""
    path = mod_dir / "ServerData.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    revision = doc.get("revision") if isinstance(doc.get("revision"), dict) else {}
    return {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "version": revision.get("version") or None,
    }


def has_thumbnail(mod_dir: Path) -> bool:
    return (mod_dir / "thumbnail.png").is_file()


# --------------------------------------------------------------------- scan
def _guid_from_dir_name(name: str) -> str | None:
    """``Name_GUID`` -> ``GUID`` (last 16-hex token)."""
    matches = _GUID_RE.findall(name)
    return matches[-1].upper() if matches else None


def scan_one(mod_dir: Path) -> ScannedMod:
    """Scan a single addon directory. May raise — callers in :func:`scan_all`
    catch and record."""
    meta = parse_meta(mod_dir)
    if meta is None:
        raise ValueError("no 'meta' file")

    guid = (meta.get("id") or _guid_from_dir_name(mod_dir.name) or "").upper()
    if not _GUID_RE.fullmatch(guid or ""):
        raise ValueError(f"could not determine a 16-hex GUID (meta.id={meta.get('id')!r})")

    gproj = parse_gproj(mod_dir)
    scenarios = parse_scenarios(mod_dir, guid)
    server_data = parse_serverdata(mod_dir)

    # Drop a self-referential dependency if the file lists one.
    dep_guids = [g for g in gproj["dep_guids"] if g != guid]

    return ScannedMod(
        guid=guid,
        name=meta.get("name"),
        summary=meta.get("summary"),
        tags=meta.get("tags") or [],
        version=meta.get("version"),
        size=meta.get("size"),
        is_unlisted=bool(meta.get("unlisted")),
        is_deleted_local=bool(meta.get("deleted")),
        dep_guids=dep_guids,
        scenarios=scenarios,
        has_thumbnail=has_thumbnail(mod_dir),
        dir_name=mod_dir.name,
        server_data_version=(server_data or {}).get("version"),
    )


def scan_all(root: Path | None = None) -> ScanResult:
    """Scan every addon directory under ``root`` (default: :func:`addons_root`).

    Immediate subdirectories only; any without a ``meta`` file are skipped
    silently. Returns a :class:`ScanResult` with ``mods`` and ``problems`` — no
    exception escapes a single bad directory.
    """
    root = root or addons_root()
    result = ScanResult()
    if not root.is_dir():
        result.problems.append({"dir": str(root), "error": "addons root does not exist"})
        return result

    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        if not (entry / "meta").is_file():
            logger.debug("skipping %s (no meta file)", entry.name)
            continue
        try:
            result.mods.append(scan_one(entry))
        except Exception as exc:  # noqa: BLE001 - robustness is the point
            logger.warning("scan failed for %s: %s", entry.name, exc)
            result.problems.append({"dir": entry.name, "error": f"{type(exc).__name__}: {exc}"})
    return result
