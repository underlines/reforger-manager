"""Async steamcmd wrapper for Arma Reforger Server (app 1874900).

ANONYMOUS ONLY. The only login argument ever passed is ``+login anonymous`` —
there is no credential path anywhere in this project. Do not add one.

stdout is streamed line-by-line into the owning job's progress; download percent
is parsed from steamcmd's ``Update state (0x...) ..., progress: NN.NN`` lines.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..core.config import settings
from ..core.jobs import JobContext

APP_ID = str(settings.steam_app_id)  # "1874900"

# steamcmd progress lines, e.g.
#   Update state (0x61) downloading, progress: 42.66 (1234 / 5678)
#   Update state (0x5) verifying install, progress: 90.11 (...)
_PROGRESS_RE = re.compile(
    r"Update state \(0x[0-9a-fA-F]+\)\s*([^,]+),\s*progress:\s*([0-9]+(?:\.[0-9]+)?)"
)
# generic fallback ("... progress: NN.NN")
_PROGRESS_FALLBACK_RE = re.compile(r"progress:\s*([0-9]+(?:\.[0-9]+)?)")
_SUCCESS_RE = re.compile(r"Success! App '1874900' fully installed", re.IGNORECASE)
_ERROR_RE = re.compile(r"Error!|ERROR!|Failed to install", re.IGNORECASE)


class SteamCmdError(RuntimeError):
    pass


def _base_args(install_dir: Path) -> list[str]:
    return [
        settings.steamcmd_path,
        "+force_install_dir",
        str(install_dir),
        "+login",
        "anonymous",
    ]


async def _stream(args: list[str], ctx: JobContext, *, phase: str) -> int:
    """Run a steamcmd invocation, pump output into the job, return exit code."""
    await ctx.log(f"$ {' '.join(args)}")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    last_pct = -1.0
    async for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        await ctx.log(line)

        m = _PROGRESS_RE.search(line)
        if m:
            step, pct = m.group(1).strip(), float(m.group(2))
            if abs(pct - last_pct) >= 0.5:
                last_pct = pct
                await ctx.progress(pct, f"{phase}: {step}")
            continue
        m = _PROGRESS_FALLBACK_RE.search(line)
        if m:
            pct = float(m.group(1))
            if abs(pct - last_pct) >= 0.5:
                last_pct = pct
                await ctx.progress(pct, phase)

    rc = await proc.wait()
    await ctx.log(f"steamcmd exited {rc}")
    return rc


async def install_or_update(ctx: JobContext, *, validate: bool = True) -> dict:
    """`steamcmd +force_install_dir <SERVER_DIR> +login anonymous
    +app_info_update 1 +app_update 1874900 [validate] +quit`.

    ``+app_info_update 1`` is mandatory: on a cold steamcmd appinfo cache (a
    fresh container, a wiped ``~/Steam``) ``+app_update 1874900`` otherwise
    aborts with ``Failed to install app '1874900' (Missing configuration)``
    because the depot manifest was never fetched.
    """
    settings.server_dir.mkdir(parents=True, exist_ok=True)
    args = _base_args(settings.server_dir) + [
        "+app_info_update", "1", "+app_update", APP_ID,
    ]
    if validate:
        args.append("validate")
    args.append("+quit")

    await ctx.progress(0.0, "starting steamcmd")
    rc = await _stream(args, ctx, phase="app_update")
    if rc != 0:
        raise SteamCmdError(f"steamcmd +app_update exited with code {rc}")
    await ctx.progress(100.0, "steamcmd finished")
    return {"exit_code": rc, "install_dir": str(settings.server_dir), "validated": validate}


async def validate_only(ctx: JobContext) -> dict:
    """Re-validate the existing install in place (no forced full redownload)."""
    return await install_or_update(ctx, validate=True)


async def app_info_print() -> str:
    """`steamcmd +login anonymous +app_info_update 1 +app_info_print 1874900 +quit`.

    Used only as the engine-build fallback when api.steamcmd.net is unavailable.
    Writes only to the container's ephemeral Steam/appcache. Returns raw stdout.
    """
    args = [
        settings.steamcmd_path,
        "+login",
        "anonymous",
        "+app_info_update",
        "1",
        "+app_info_print",
        APP_ID,
        "+quit",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace")
