"""The steamcmd engine-install invocation must ask for app-info first.

Regression guard: on a cold steamcmd appinfo cache (fresh container, wiped
``~/Steam``) ``+app_update 1874900`` alone aborts with
``Failed to install app '1874900' (Missing configuration)``. The fix is to pass
``+app_info_update 1`` before ``+app_update`` so the depot manifest is fetched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.steam import steamcmd


class _FakeCtx:
    async def log(self, *_a, **_k) -> None: ...
    async def progress(self, *_a, **_k) -> None: ...


@pytest.mark.asyncio
@pytest.mark.parametrize("validate", [True, False])
async def test_install_or_update_passes_app_info_update_before_app_update(monkeypatch, validate):
    captured: dict[str, list[str]] = {}

    async def _fake_stream(args, ctx, *, phase):  # noqa: ANN001
        captured["args"] = list(args)
        return 0

    monkeypatch.setattr(steamcmd, "_stream", _fake_stream)

    result = await steamcmd.install_or_update(_FakeCtx(), validate=validate)
    assert result["exit_code"] == 0

    args = captured["args"]
    assert "+app_info_update" in args, args
    assert "+app_update" in args, args
    # order matters: app-info must be requested before the app update
    assert args.index("+app_info_update") < args.index("+app_update"), args
    assert args[args.index("+app_info_update") + 1] == "1", args
    assert args[args.index("+app_update") + 1] == steamcmd.APP_ID, args
    assert ("validate" in args) is validate, args
    assert args[-1] == "+quit", args
    # anonymous only, always
    assert args[args.index("+login") + 1] == "anonymous", args
