"""Addon-cache verification and repair job.

The Reforger binary does not have a confirmed per-GUID verification option, so
``guids`` is recorded in the job log only.  The engine always verifies the
whole configured addon cache.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from ..core.config import settings
from ..core.jobs import JobContext
from ..servers.supervisor import supervisor

VERIFY_REPAIR_JOB_KIND = "verify_repair"

_GUID = r"[0-9A-F]{16}"
_VERIFIED_RE = re.compile(
    rf"(?:\b(?:addon\s+)?(?P<guid>{_GUID})\b.*\b(?:verified|verification succeeded)\b|"
    rf"\b(?:verified|verification succeeded)\b.*\b(?:addon\s+)?(?P<guid_after>{_GUID})\b)",
    re.IGNORECASE,
)
_REPAIRED_RE = re.compile(
    rf"(?:\b(?:addon\s+)?(?P<guid>{_GUID})\b.*\b(?:repaired|repair succeeded)\b|"
    rf"\b(?:repaired|repair succeeded)\b.*\b(?:addon\s+)?(?P<guid_after>{_GUID})\b)",
    re.IGNORECASE,
)
_FAILED_RE = re.compile(
    rf"(?:\b(?:addon\s+)?(?P<guid>{_GUID})\b.*\b(?:verify|verification|repair)\w*\s+failed\b|"
    rf"\b(?:verify|verification|repair)\w*\s+failed\b.*\b(?:addon\s+)?(?P<guid_after>{_GUID})\b)",
    re.IGNORECASE,
)
_FATAL_RE = re.compile(
    r"\b(?:fatal error|unable to initialize the game|cannot create game|"
    r"addon loading failed|addons? are not downloadable)\b",
    re.IGNORECASE,
)


class VerifyRepairError(RuntimeError):
    """The engine could not complete addon verification and repair."""


@dataclass
class _VerifyProgress:
    checked: set[str] = field(default_factory=set)
    repaired: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    fatal_line: str | None = None

    def consume(self, line: str) -> None:
        """Record only explicit per-addon outcomes from engine output."""
        if _FATAL_RE.search(line):
            self.fatal_line = line
        for match in _VERIFIED_RE.finditer(line):
            self.checked.add(_matched_guid(match))
        for match in _REPAIRED_RE.finditer(line):
            guid = _matched_guid(match)
            self.checked.add(guid)
            self.repaired.add(guid)
        for match in _FAILED_RE.finditer(line):
            self.failed.add(_matched_guid(match))

    def result(self) -> dict:
        return {
            "checked": len(self.checked),
            "repaired": len(self.repaired),
            "failed": len(self.failed),
        }


def _matched_guid(match: re.Match[str]) -> str:
    return (match.group("guid") or match.group("guid_after")).upper()


def _build_args() -> list[str]:
    """Build the documented, whole-cache verify/repair invocation."""
    return [
        str(settings.reforger_binary),
        "-addonsDir",
        str(settings.mods_dir),
        "-addonsVerify",
        "-addonsRepair",
        "-nothrow",
    ]


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    await process.wait()


async def run_verify_repair(
    ctx: JobContext, guids: list[str] | None = None
) -> dict:
    """Verify and repair the complete addon cache through the engine.

    ``guids`` deliberately does not change the command: no supported selective
    verification flag has been confirmed.  It is logged so callers can retain
    their requested scope without claiming that the binary honored it.
    """
    if supervisor.is_running():
        active_id = supervisor.active_server_id
        raise VerifyRepairError(
            f"cannot verify addon cache while server {active_id} is running"
        )

    binary = settings.reforger_binary
    if not binary.is_file():
        raise VerifyRepairError(
            f"Reforger binary not found at {binary} (engine not installed?)"
        )

    if guids:
        requested = ", ".join(str(guid).upper() for guid in guids)
        await ctx.log(
            f"requested GUID scope: {requested}; verifying the entire addon cache "
            "because the engine has no confirmed selective verify flag"
        )

    args = _build_args()
    await ctx.progress(0.0, "starting addon verification and repair")
    await ctx.log(f"$ {' '.join(args)}")
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(settings.server_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None

    progress = _VerifyProgress()
    try:
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            await ctx.log(line)
            progress.consume(line)
            result = progress.result()
            await ctx.progress(
                min(99.0, float(result["checked"])),
                f"checked {result['checked']}; repaired {result['repaired']}; "
                f"failed {result['failed']}",
            )
            if progress.fatal_line:
                await _stop_process(process)
                raise VerifyRepairError(
                    f"fatal addon verification diagnosis: {progress.fatal_line}"
                )
            if ctx.cancelled:
                await _stop_process(process)
                raise asyncio.CancelledError
        exit_code = await process.wait()
    except asyncio.CancelledError:
        await _stop_process(process)
        raise

    result = progress.result()
    await ctx.log(f"addon verification exited {exit_code}: {result}")
    if exit_code != 0:
        raise VerifyRepairError(
            f"addon verification and repair exited with code {exit_code}: {result}"
        )
    if result["failed"]:
        raise VerifyRepairError(
            f"addon verification reported {result['failed']} failed addon(s): {result}"
        )

    await ctx.progress(100.0, "addon verification and repair complete")
    return result
