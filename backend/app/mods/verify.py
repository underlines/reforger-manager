"""Addon-cache verification and repair job.

The Reforger binary does not have a confirmed per-GUID verification option, so
``guids`` is recorded in the job log only.  The engine always verifies the
whole configured addon cache.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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


# The engine block-buffers its stdout when it is a pipe, so per-addon verify
# lines never arrive through one — the job would sit on a blocked read while
# the 200-line job log fills with main-menu spam. The engine's own
# ``<profile>/logs/logs_*/console.log`` is written promptly and line-buffered,
# so that is what we follow instead (the mod downloader does the same).
_LOG_APPEAR_TIMEOUT = 120.0  # seconds to wait for the engine to create its log
_POST_EXIT_GRACE = 2.0  # keep draining the log this long after the engine exits
_POLL_INTERVAL = 0.25


def _build_args(profile_dir: Path | None = None) -> list[str]:
    """Build the documented, whole-cache verify/repair invocation.

    ``profile_dir`` points the engine's profile (and with it its console log)
    into a scratch dir so the job can tail it instead of the unusable piped
    stdout.
    """
    args = [
        str(settings.reforger_binary),
        "-addonsDir",
        str(settings.mods_dir),
        "-addonsVerify",
        "-addonsRepair",
        "-nothrow",
    ]
    if profile_dir is not None:
        args += ["-profile", str(profile_dir)]
    return args


async def _stop_process(
    process: asyncio.subprocess.Process, grace: float = 10.0
) -> None:
    """Stop the engine, escalating SIGTERM -> SIGKILL.

    SIGKILL cannot be caught, blocked or ignored, so a wedged engine that sits
    on SIGTERM still dies here (the only hold-outs are uninterruptible-sleep /
    zombie states, which no signal can clear).
    """
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - already gone
        return
    await process.wait()


def _newest_console_log(profile_dir: Path) -> Path | None:
    """Newest engine console log under ``profile_dir`` (sort by name)."""
    logs_root = profile_dir / "logs"
    if not logs_root.is_dir():
        return None
    candidates = sorted(logs_root.glob("logs_*/console.log"), key=lambda p: p.name)
    return candidates[-1] if candidates else None


async def _consume_verify_line(
    process: asyncio.subprocess.Process,
    ctx: JobContext,
    progress: _VerifyProgress,
    line: str,
) -> None:
    """Log one engine line, fold it into the progress and react to verdicts."""
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


async def _stream_stdout(
    process: asyncio.subprocess.Process,
    ctx: JobContext,
    progress: _VerifyProgress,
) -> int:
    """Drain an explicitly attached stdout pipe.

    The engine block-buffers piped stdout, so the run itself spawns with
    DEVNULL and follows the console log (``_follow_console_log``); this path
    only serves callers that deliberately attach a pipe and feed it directly
    (the test suite does).
    """
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        await _consume_verify_line(process, ctx, progress, line)
    return await process.wait()


async def _follow_console_log(
    process: asyncio.subprocess.Process,
    profile_dir: Path,
    ctx: JobContext,
    progress: _VerifyProgress,
) -> int:
    """Wait for the engine's console log, then tail it line by line."""
    loop = asyncio.get_running_loop()
    appear_deadline = loop.time() + _LOG_APPEAR_TIMEOUT

    # 1. wait for the engine to create its console log
    log_path: Path | None = None
    while log_path is None:
        log_path = _newest_console_log(profile_dir)
        if log_path is not None:
            break
        if ctx.cancelled:
            await _stop_process(process)
            raise asyncio.CancelledError
        if process.returncode is not None:
            raise VerifyRepairError(
                f"addon verification engine exited early (code {process.returncode}) "
                "before writing a console log"
            )
        if loop.time() > appear_deadline:
            await _stop_process(process)
            raise VerifyRepairError(
                "addon verification timed out waiting for the engine to start "
                "writing its console log"
            )
        await asyncio.sleep(_POLL_INTERVAL)

    # 2. follow it line by line until the engine has exited and stayed quiet
    handle = log_path.open("r", encoding="utf-8", errors="replace")
    try:
        buf = ""
        exited_at: float | None = None
        while True:
            chunk = handle.read()
            if chunk:
                buf += chunk
                exited_at = None  # fresh output: restart the post-exit quiet wait
                while "\n" in buf:
                    raw_line, buf = buf.split("\n", 1)
                    line = raw_line.rstrip()
                    if not line:
                        continue
                    await _consume_verify_line(process, ctx, progress, line)
                continue
            if process.returncode is not None:
                if exited_at is None:
                    exited_at = loop.time()
                elif loop.time() - exited_at > _POST_EXIT_GRACE:
                    if buf.strip():
                        line = buf.rstrip()
                        if line:
                            await _consume_verify_line(process, ctx, progress, line)
                        buf = ""
                    return await process.wait()
            if ctx.cancelled:
                await _stop_process(process)
                raise asyncio.CancelledError
            await asyncio.sleep(_POLL_INTERVAL)
    finally:
        handle.close()


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

    progress = _VerifyProgress()
    with tempfile.TemporaryDirectory(prefix="reforger-verify-") as tmp:
        profile_dir = Path(tmp) / "profile"
        profile_dir.mkdir()
        args = _build_args(profile_dir)
        await ctx.progress(0.0, "starting addon verification and repair")
        await ctx.log(f"$ {' '.join(args)}")
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(settings.server_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if process.stdout is not None:
                exit_code = await _stream_stdout(process, ctx, progress)
            else:
                exit_code = await _follow_console_log(
                    process, profile_dir, ctx, progress
                )
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
