"""Engine-build tracking for Arma Reforger Server (app 1874900).

Detection keys on the numeric Steam ``buildid`` (string compare), never the
display version:

* installed build  -> ``<SERVER_DIR>/steamapps/appmanifest_1874900.acf`` "buildid"
* latest build     -> ``GET https://api.steamcmd.net/v1/info/1874900``
                      ``data["1874900"].depots.branches.public.buildid``
* fallback         -> ``steamcmd +login anonymous +app_info_update 1
                      +app_info_print 1874900`` (retry once on empty)

The human-readable version (``1.8.0.10``) never appears in any manifest. It is
normally scraped from a server ``console.log``; a small verified build map
allows compatibility preflight before the first server run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models import ENGINE_SINGLETON_ID, Engine

logger = logging.getLogger("reforger.engine")

APP_ID = str(settings.steam_app_id)

_DISPLAY_VERSION_RE = re.compile(
    r"Creating game instance\(.*?\), version (\S+)"
)

# Verified from the real console log documented in docs/REF.md. Keep this
# deliberately small: an unknown build must not inherit an old display version.
KNOWN_BUILD_DISPLAY_VERSIONS = {
    "24501482": "1.8.0.10",
}


# --------------------------------------------------------------------- VDF/KV
def _parse_kv(text: str) -> dict:
    """Minimal Valve KeyValues parser -> nested dict.

    Handles quoted keys/values, nested ``{ }`` blocks and ``//`` comments. Good
    enough for appmanifest .acf and the ``app_info_print`` payload; not a full
    VDF implementation.
    """
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stack: list[dict] = [{}]
    pending_key: str | None = None
    for quoted, brace in tokens:
        if brace == "{":
            if pending_key is None:
                continue
            new: dict = {}
            stack[-1][pending_key] = new
            stack.append(new)
            pending_key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            pending_key = None
        else:
            value = quoted.replace('\\"', '"').replace("\\\\", "\\")
            if pending_key is None:
                pending_key = value
            else:
                stack[-1][pending_key] = value
                pending_key = None
    return stack[0]


def _ci_get(d: dict, *keys: str) -> dict | str | None:
    """Case-insensitive nested lookup."""
    cur: dict | str | None = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        lower = {k.lower(): v for k, v in cur.items()}
        cur = lower.get(key.lower())
    return cur


# ------------------------------------------------------------ installed build
def read_installed_build(manifest_path: Path | None = None) -> dict:
    path = manifest_path or settings.appmanifest_path
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.info("appmanifest not found at %s (engine not installed yet)", path)
        return {}
    parsed = _parse_kv(text)
    state = parsed.get("AppState") or parsed.get("appstate") or {}
    if not isinstance(state, dict):
        state = {}
    build = _ci_get(state, "buildid")
    result: dict = {}
    if build:
        result["buildid"] = str(build)
    target = _ci_get(state, "TargetBuildID")
    if target and str(target) != "0":
        result["target_build"] = str(target)
    beta = _ci_get(state, "BetaKey") or _ci_get(state, "UserConfig", "BetaKey")
    if beta:
        result["beta_key"] = str(beta)
    return result


# -------------------------------------------------------------- latest build
async def fetch_latest_build() -> dict:
    """Return ``{"buildid": str, "time_updated": datetime|None}`` or ``{}``."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(settings.steamcmd_net_info_url)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("api.steamcmd.net lookup failed: %s", exc)
        payload = {}

    app = (payload.get("data") or {}).get(APP_ID) or {}
    public = _ci_get(app, "depots", "branches", "public")
    if isinstance(public, dict) and public.get("buildid"):
        return {
            "buildid": str(public["buildid"]),
            "time_updated": _epoch(public.get("timeupdated")),
        }

    # Fallback: steamcmd app_info_print, retry once on empty.
    for attempt in (1, 2):
        try:
            from .steamcmd import app_info_print

            raw = await app_info_print()
        except Exception as exc:  # pragma: no cover - subprocess/env dependent
            logger.warning("app_info_print failed (attempt %d): %s", attempt, exc)
            continue
        build = _parse_app_info_build(raw)
        if build:
            return {"buildid": build["buildid"], "time_updated": build.get("time_updated")}
    return {}


def _parse_app_info_build(raw: str) -> dict:
    idx = raw.find(f'"{APP_ID}"')
    if idx == -1:
        return {}
    parsed = _parse_kv(raw[idx:])
    app = parsed.get(APP_ID) or {}
    public = _ci_get(app, "depots", "branches", "public") if isinstance(app, dict) else None
    if isinstance(public, dict) and public.get("buildid"):
        return {
            "buildid": str(public["buildid"]),
            "time_updated": _epoch(public.get("timeupdated")),
        }
    return {}


def _epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ persistence
async def get_or_create_engine(session: AsyncSession) -> Engine:
    row = await session.get(Engine, ENGINE_SINGLETON_ID)
    if row is None:
        row = Engine(id=ENGINE_SINGLETON_ID)
        session.add(row)
        await session.flush()
    return row


def _set_installed_build(row: Engine, installed: dict) -> bool:
    """Persist an installed build and its safe display-version fallback.

    A stored display version is valid only for the build that produced it. On a
    build change, discard it before applying a fallback so an unknown build is
    never reported as compatible with the previous one.
    """
    build = installed["buildid"]
    build_changed = row.installed_build != build
    row.installed_build = build
    row.target_build = installed.get("target_build")
    row.beta_key = installed.get("beta_key")
    if build_changed or not row.installed_version:
        row.installed_version = KNOWN_BUILD_DISPLAY_VERSIONS.get(build)
    return build_changed


async def seed_engine(session: AsyncSession) -> Engine:
    """Ensure the singleton exists and its installed_* reflect what is on disk."""
    row = await get_or_create_engine(session)
    installed = read_installed_build()
    if installed.get("buildid"):
        _set_installed_build(row, installed)
    await session.commit()
    return row


async def refresh_engine(session: AsyncSession) -> Engine:
    """Re-read the installed build and query the latest public build; persist
    both onto the singleton keyed on buildid."""
    row = await get_or_create_engine(session)
    now = datetime.now(timezone.utc)

    installed = read_installed_build()
    if installed.get("buildid"):
        if row.installed_build and row.installed_build != installed["buildid"]:
            row.last_updated_at = now
        _set_installed_build(row, installed)

    latest = await fetch_latest_build()
    if latest.get("buildid"):
        row.latest_build = latest["buildid"]
        if latest.get("time_updated"):
            row.latest_time_updated = latest["time_updated"]

    row.last_checked = now
    row.update_available = bool(
        row.installed_build
        and row.latest_build
        and row.installed_build != row.latest_build
    )
    await session.commit()
    return row


async def mark_engine_updated(session: AsyncSession, display_version: str | None = None) -> Engine:
    """Called after a completed engine-update job: re-read the on-disk build,
    stamp last_updated_at, clear the update badge."""
    row = await get_or_create_engine(session)
    installed = read_installed_build()
    if installed.get("buildid"):
        _set_installed_build(row, installed)
    if display_version:
        row.installed_version = display_version
    row.last_updated_at = datetime.now(timezone.utc)
    row.update_available = bool(
        row.installed_build
        and row.latest_build
        and row.installed_build != row.latest_build
    )
    await session.commit()
    return row


# -------------------------------------------------------------- display version
def scrape_display_version(console_log_path: str | Path) -> str | None:
    try:
        text = Path(console_log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _DISPLAY_VERSION_RE.search(text)
    return m.group(1) if m else None
