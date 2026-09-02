"""Focused orchestration tests for post-engine-update intelligence."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models.engine import Engine
from app.models.mod import Mod
from app.models.server import ServerMod
from app.mods.post_engine_update import on_engine_updated
from app.servers.preflight import PreflightCheck, PreflightReport


class PostEngineUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_combines_preflight_and_stale_library_and_server_pins(self) -> None:
        updated_engine = Engine(id=1, installed_build="24501483", installed_version="1.8.1.0")
        library_stale = Mod(
            guid="AAAAAAAAAAAAAAAA", pinned_version="1.0.0", pinned_at_build="24501482"
        )
        library_current = Mod(
            guid="BBBBBBBBBBBBBBBB", pinned_version="1.0.0", pinned_at_build="24501483"
        )
        server_stale = ServerMod(
            server_id=4,
            mod_guid="CCCCCCCCCCCCCCCC",
            pinned_version="2.0.0",
            pinned_at_build="24501482",
        )
        reports = {
            4: PreflightReport("green", [PreflightCheck("preflight", "green", "OK")], []),
            9: PreflightReport("blocked", [PreflightCheck("Workshop", "blocked", "Missing")], []),
        }
        session = AsyncMock()
        session.execute.side_effect = [
            _result([library_stale, library_current]),
            _result([server_stale]),
        ]

        with patch(
            "app.mods.post_engine_update.mark_engine_updated",
            new=AsyncMock(return_value=updated_engine),
        ) as mark_updated, patch(
            "app.mods.post_engine_update.preflight_all",
            new=AsyncMock(return_value=reports),
        ) as run_preflight:
            report = await on_engine_updated(session)

        mark_updated.assert_awaited_once_with(session)
        run_preflight.assert_awaited_once_with(session)
        self.assertEqual(report["engine"], {
            "installed_build": "24501483", "installed_version": "1.8.1.0"
        })
        self.assertEqual(report["server_preflight"]["4"]["verdict"], "green")
        self.assertEqual(report["summary"], {
            "servers_checked": 2,
            "green_servers": 1,
            "warning_servers": 0,
            "blocked_servers": 1,
            "library_pins_checked": 2,
            "server_pins_checked": 1,
            "stale_pins": 2,
        })
        self.assertEqual(report["stale_pins"], [
            {
                "scope": "library", "guid": "AAAAAAAAAAAAAAAA", "pinned_version": "1.0.0",
                "pinned_at_build": "24501482", "installed_build": "24501483",
                "guidance": "Unpin and take latest.",
            },
            {
                "scope": "server", "guid": "CCCCCCCCCCCCCCCC", "server_id": 4,
                "pinned_version": "2.0.0", "pinned_at_build": "24501482",
                "installed_build": "24501483", "guidance": "Unpin and take latest.",
            },
        ])


class _result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


if __name__ == "__main__":
    unittest.main()
