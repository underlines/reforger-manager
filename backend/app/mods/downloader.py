"""Headless Arma Reforger addon downloader.

The engine downloads addons named in its server config before it creates a game.
This module supplies a deliberately minimal config and stops that temporary
process at the verified ``Required addons are ready to use.`` marker.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..core.config import settings
from ..models import Mod
from ..servers.supervisor import supervisor

if TYPE_CHECKING:
    from ..core.jobs import JobContext
    from sqlalchemy.ext.asyncio import AsyncSession


MOD_DOWNLOAD_JOB_KIND = "mod_download"
_GUID_RE = re.compile(r"^[0-9A-F]{16}$")

# The engine now schema-validates the server config and rejects an empty
# ``game.scenarioId`` (must match ``^\{[0-9A-F]{16}\}[a-zA-Z0-9_./ -]+$``). The
# headless downloader stops at "Required addons are ready to use." long before a
# scenario world is loaded, so any well-formed id works — use a base-game one
# that is always installed.
_PLACEHOLDER_SCENARIO_ID = "{ECC61978EDCC2B5A}Missions/23_Campaign.conf"
_START_RE = re.compile(r"Addon Download started ([0-9A-F]{16}) - (.+)$")
_VERSION_RE = re.compile(r"Downloading ([0-9A-F]{16}) version (.+)$")
_PROGRESS_RE = re.compile(
    r"(.+?):\s*\[[^]]*\]\s*(\d{1,3})%\s*([0-9.]+)\s*/\s*([0-9.]+)\s*MB$"
)
_SPEED_RE = re.compile(r"Download speed\s+(.+)$")
_FATAL_RE = re.compile(
    r"Addon [0-9A-F]{16} .* - Addon is blocked\."
    r"|Addon [0-9A-F]{16} - Addon was not found on workshop\."
    r"|Addon [0-9A-F]{16} .* - Addon has dependencies deleted from workshop\."
    r"|\d+ addons are not downloadable! Cannot start until they are removed from server config\."
    r"|Failed to fetch addon details from workshop API! Repeat later or try different mods\."
    r"|ENGINE\s+\(E\): Addon loading failed \{[0-9A-F,]+\}"
    r"|SCRIPT\s+\(E\): Can't compile \"Game\" script module!"
    r"|SCRIPT\s+\(E\): .*(?:Can't find variable|Can't find class|Too many parameters|Syntax error)"
    r"|ENGINE\s+\(E\): Cannot create game!"
    r"|ENGINE\s+\(E\): Unable to initialize the game",
    re.IGNORECASE,
)
_READY_MARKER = "Required addons are ready to use."


class ModDownloadError(RuntimeError):
    """The engine could not prepare the requested addons."""

    def __init__(self, message: str, *, diagnosis: Any = None) -> None:
        super().__init__(message)
        self.diagnosis = diagnosis


@dataclass
class DownloadProgress:
    """State extracted from the engine's verified downloader output."""

    names: dict[str, str] = field(default_factory=dict)
    name_to_guid: dict[str, str] = field(default_factory=dict)
    percentages: dict[str, float] = field(default_factory=dict)
    totals_mb: dict[str, float] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    speed: str | None = None
    ready: bool = False
    current_guid: str | None = None

    def feed(self, line: str) -> dict[str, Any] | None:
        """Consume one output line and return its recognized event, if any."""
        text = line.strip()
        if text.startswith("BACKEND : "):
            text = text[len("BACKEND : ") :]

        match = _START_RE.search(text)
        if match:
            guid, name = match.groups()
            self.names[guid] = name
            self.name_to_guid[name] = guid
            self.current_guid = guid
            return {"kind": "start", "guid": guid, "name": name}

        match = _VERSION_RE.search(text)
        if match:
            guid, version = match.groups()
            self.versions[guid] = version
            self.current_guid = guid
            return {"kind": "version", "guid": guid, "version": version}

        match = _PROGRESS_RE.search(text)
        if match:
            name, pct, _done_mb, total_mb = match.groups()
            guid = self.name_to_guid.get(name, self.current_guid)
            if guid:
                self.percentages[guid] = min(100.0, float(pct))
                total = float(total_mb)
                if total > 0:
                    self.totals_mb[guid] = total
                return {
                    "kind": "progress",
                    "guid": guid,
                    "name": name,
                    "percent": float(pct),
                    "total_mb": total,
                }

        match = _SPEED_RE.search(text)
        if match:
            self.speed = match.group(1)
            return {"kind": "speed", "speed": self.speed}

        if _READY_MARKER in text:
            self.ready = True
            return {"kind": "ready"}
        return None

    def percent(self, guids: list[str]) -> float:
        """Return byte-weighted progress when reported addon sizes are known."""
        values = [(guid, self.percentages.get(guid, 0.0)) for guid in guids]
        known = [(guid, pct) for guid, pct in values if self.totals_mb.get(guid, 0) > 0]
        if known:
            total = sum(self.totals_mb[guid] for guid, _pct in known)
            return sum(self.totals_mb[guid] * pct for guid, pct in known) / total
        return sum(pct for _guid, pct in values) / len(values) if values else 100.0


def _normalise_inputs(
    guids: list[str], versions: dict[str, str] | None
) -> tuple[list[str], dict[str, str]]:
    unique: list[str] = []
    for guid in guids:
        normalised = str(guid).upper()
        if not _GUID_RE.fullmatch(normalised):
            raise ValueError(f"invalid Reforger addon GUID: {guid!r}")
        if normalised not in unique:
            unique.append(normalised)

    normalised_versions: dict[str, str] = {}
    for guid, version in (versions or {}).items():
        normalised = str(guid).upper()
        if normalised not in unique:
            raise ValueError(f"version pin supplied for unrequested addon {guid!r}")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"invalid version pin for addon {guid!r}")
        normalised_versions[normalised] = version.strip()
    return unique, normalised_versions


def _build_config(guids: list[str], versions: dict[str, str]) -> dict:
    mods = []
    for guid in guids:
        mod = {"modId": guid}
        if guid in versions:
            mod["version"] = versions[guid]
        mods.append(mod)
    return {
        "game": {
            "name": "Reforger Manager addon downloader",
            "password": "",
            "passwordAdmin": "",
            "scenarioId": _PLACEHOLDER_SCENARIO_ID,
            "maxPlayers": 1,
            "visible": False,
            "supportedPlatforms": ["PLATFORM_PC"],
            "mods": mods,
        }
    }


def _build_command(config_path: Path, profile_dir: Path, addon_tmp: Path) -> list[str]:
    return [
        str(settings.reforger_binary),
        "-config", str(config_path),
        "-profile", str(profile_dir),
        # The engine writes addons under ``<addonDownloadDir>/addons/``. The
        # scanner (``scanner.addons_root()``) and the running server both use
        # ``<mods_dir>/reforger/addons/``, so this MUST include the ``reforger``
        # segment or downloaded mods land where nothing looks for them.
        "-addonDownloadDir", str(Path(settings.mods_dir) / "reforger"),
        "-addonTempDir", str(addon_tmp),
        "-nothrow",
        "-maxFPS", "10",
    ]


def _diagnose(lines: list[str]) -> Any:
    """Use Phase 3b diagnosis when available without coupling this module to it."""
    try:
        from ..servers.diagnosis import diagnose
    except ImportError:
        return None
    try:
        return diagnose("\n".join(lines))
    except Exception:
        return None


def _terminate(process) -> None:
    try:
        process.send_signal(signal.SIGTERM)
    except (ProcessLookupError, ValueError, NotImplementedError, AttributeError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


# The engine block-buffers its stdout when it is a pipe, so the
# "Required addons are ready to use." line often never reaches us before the
# process goes quiet — the job would then hang on a blocked read forever. The
# engine's own ``<profile>/logs/logs_*/console.log`` is written promptly and
# line-buffered, so that is what we follow instead.
_LOG_APPEAR_TIMEOUT = 120.0   # seconds to wait for the engine to create its log
_OVERALL_TIMEOUT = 60 * 60    # hard cap on a single download run
_POST_EXIT_GRACE = 5.0        # keep draining the log this long after the engine exits
_POLL_INTERVAL = 0.5


async def _newest_console_log(profile_dir: Path) -> Path | None:
    logs_root = profile_dir / "logs"
    if not logs_root.is_dir():
        return None
    candidates = sorted(logs_root.glob("logs_*/console.log"), key=lambda p: p.name)
    return candidates[-1] if candidates else None


async def _run_engine(ctx: "JobContext | None", guids: list[str], versions: dict[str, str]) -> dict:
    if supervisor.is_running():
        raise ModDownloadError("cannot download addons while a server is running")
    if not settings.reforger_binary.is_file():
        raise ModDownloadError(f"Reforger binary not found at {settings.reforger_binary}")

    parser = DownloadProgress()
    lines: list[str] = []
    loop = asyncio.get_running_loop()

    with tempfile.TemporaryDirectory(prefix="reforger-mod-download-") as tmp:
        root = Path(tmp)
        config_path = root / "config.json"
        profile_dir = root / "profile"
        addon_tmp = root / "addons_tmp"
        profile_dir.mkdir()
        addon_tmp.mkdir()
        config_path.write_text(json.dumps(_build_config(guids, versions)), encoding="utf-8")
        command = _build_command(config_path, profile_dir, addon_tmp)
        if ctx:
            await ctx.progress(0.0, "starting addon downloader")
            await ctx.log("launching headless addon downloader")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(settings.server_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def _finish_ok() -> dict:
            _terminate(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=15)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            if ctx:
                await ctx.progress(100.0, "addons ready")
            return {
                "guids": guids,
                "versions": versions,
                "progress": 100.0,
                "speed": parser.speed,
                "downloaded": parser.names,
            }

        async def _consume(line: str) -> dict | None:
            lines.append(line)
            if ctx:
                await ctx.log(line)
            event = parser.feed(line)
            if event and event["kind"] == "progress" and ctx:
                await ctx.progress(parser.percent(guids), event.get("name"))
            if event and event["kind"] == "ready":
                return await _finish_ok()
            if _FATAL_RE.search(line):
                diagnosis = _diagnose(lines)
                _terminate(process)
                await process.wait()
                detail = str(diagnosis) if diagnosis is not None else line
                raise ModDownloadError(f"addon download failed: {detail}", diagnosis=diagnosis)
            return None

        try:
            deadline = loop.time() + _OVERALL_TIMEOUT
            log_appear_deadline = loop.time() + _LOG_APPEAR_TIMEOUT

            # 1. wait for the engine to create its console log
            log_path: Path | None = None
            while log_path is None:
                log_path = await _newest_console_log(profile_dir)
                if log_path is not None:
                    break
                if process.returncode is not None:
                    raise ModDownloadError(
                        f"addon downloader engine exited early (code {process.returncode}) "
                        "before writing a log"
                    )
                if loop.time() > log_appear_deadline:
                    _terminate(process)
                    await process.wait()
                    raise ModDownloadError("addon download timed out before the engine started logging")
                await asyncio.sleep(_POLL_INTERVAL)

            # 2. follow it line by line until the ready/fatal marker or the engine exits
            handle = log_path.open("r", encoding="utf-8", errors="replace")
            exited_at: float | None = None
            try:
                buf = ""
                while True:
                    chunk = handle.read()
                    if chunk:
                        buf += chunk
                        while "\n" in buf:
                            raw_line, buf = buf.split("\n", 1)
                            result = await _consume(raw_line.rstrip("\r"))
                            if result is not None:
                                return result
                        continue

                    if process.returncode is not None:
                        if exited_at is None:
                            exited_at = loop.time()
                        elif loop.time() - exited_at > _POST_EXIT_GRACE:
                            break
                    if loop.time() > deadline:
                        _terminate(process)
                        await process.wait()
                        raise ModDownloadError("addon download timed out")
                    await asyncio.sleep(_POLL_INTERVAL)
            finally:
                handle.close()

            # engine exited without ever emitting the ready marker
            rc = await process.wait()
            diagnosis = _diagnose(lines)
            detail = str(diagnosis) if diagnosis is not None else "engine exited before addons were ready"
            raise ModDownloadError(f"addon download failed (exit {rc}): {detail}", diagnosis=diagnosis)
        except BaseException:
            if process.returncode is None:
                _terminate(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise


async def run_mod_download(
    ctx: "JobContext", guids: list[str], versions: dict[str, str] | None = None
) -> dict:
    """Run the headless downloader job for the requested addons."""
    normalised_guids, normalised_versions = _normalise_inputs(guids, versions)
    if not normalised_guids:
        await ctx.progress(100.0, "no addons requested")
        return {"guids": [], "versions": {}, "progress": 100.0, "downloaded": {}}
    return await _run_engine(ctx, normalised_guids, normalised_versions)


async def ensure_mods_ready(
    session: "AsyncSession", guids: list[str], versions: dict[str, str] | None = None
) -> None:
    """Ensure missing or specifically pinned addons are downloaded before return.

    Callers own the supplied session and any subsequent scan/upsert. With no
    version pins, locally recorded addons are left untouched; a pin additionally
    requires its recorded installed version to match.
    """
    normalised_guids, normalised_versions = _normalise_inputs(guids, versions)
    if not normalised_guids:
        return
    rows = (
        await session.execute(select(Mod).where(Mod.guid.in_(normalised_guids)))
    ).scalars().all()
    by_guid = {row.guid.upper(): row for row in rows}
    needed = [
        guid
        for guid in normalised_guids
        if (row := by_guid.get(guid)) is None
        or not row.is_local
        or (guid in normalised_versions and row.installed_version != normalised_versions[guid])
    ]
    if needed:
        await _run_engine(
            None,
            needed,
            {guid: normalised_versions[guid] for guid in needed if guid in normalised_versions},
        )
