"""S1 — save discovery + mod drift (``app.servers.saves``).

In-memory sqlite, self-contained (no ``conftest.py`` in this repo — see
``test_server_files_routes.py`` for the harness pattern this copies).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import Base
from app.models import Server, ServerConfigRevision, ServerMod
from app.servers.saves import discover


class SavesDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        self.tmp = Path(tempfile.mkdtemp(prefix="reforger-saves-test-"))
        self.profiles = self.tmp / "profiles"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.profiles_patch = patch.object(settings, "profiles_dir", self.profiles)
        self.profiles_patch.start()

    async def asyncTearDown(self) -> None:
        self.profiles_patch.stop()
        await self.engine.dispose()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _game_root(self, server_id: int = 1) -> Path:
        return self.profiles / str(server_id) / "profile" / ".save" / "game"

    def _settings_root(self, server_id: int = 1) -> Path:
        return self.profiles / str(server_id) / "profile" / ".save" / "settings"

    def _write_savepoint(
        self,
        server_id: int,
        scenario_dir: str,
        playthrough_nr: int,
        save_point_nr: int,
        *,
        meta: dict | None = None,
        skip_meta: bool = False,
        corrupt_meta: bool = False,
        dir_name_override: str | None = None,
        playthrough_dir_override: str | None = None,
    ) -> Path:
        pt_name = playthrough_dir_override or f"playthrough{playthrough_nr:03d}"
        sp_name = dir_name_override or f"savepoint{save_point_nr:03d}"
        path = self._game_root(server_id) / scenario_dir / pt_name / sp_name
        path.mkdir(parents=True, exist_ok=True)
        ws = path / "WorldState"
        ws.mkdir(exist_ok=True)
        (ws / "11111111-1111-1111-1111-111111111111.blob").write_bytes(b"x" * 16)

        if skip_meta:
            return path

        default = {
            "m_Id": f"uuid-{scenario_dir}-{playthrough_nr}-{save_point_nr}",
            "m_eType": 2,
            "m_iSavedAtUnix": 1_700_000_000 + playthrough_nr * 10_000 + save_point_nr * 100,
            "m_iSavePointNr": save_point_nr,
            "m_sSavePointDisplayName": "",
            "m_sMissionResource": "{5F5F49B8F5D935A2}Missions/Test.conf",
            "m_iPlaythroughNr": playthrough_nr,
            "m_sPlaythroughDisplayName": "",
            "m_iStartedUnix": 1_700_000_000,
            "m_iPlaytimeSeconds": 100,
            "m_sGameVersion": "1.8.0.13",
            "m_aUsedAddons": [],
        }
        if meta:
            default.update(meta)

        if corrupt_meta:
            (path / "meta-info.json").write_text("{not valid json", encoding="utf-8")
        else:
            (path / "meta-info.json").write_text(json.dumps(default), encoding="utf-8")
        return path

    async def _add_server(self, **kwargs) -> Server:
        server = Server(id=kwargs.pop("id", 1), name=kwargs.pop("name", "srv"), **kwargs)
        async with self.sessions() as session:
            session.add(server)
            await session.commit()
            await session.refresh(server)
        return server

    # ------------------------------------------------------------------ tests
    async def test_grouping_and_descending_order(self) -> None:
        server = await self._add_server()
        self._write_savepoint(1, "ScenA", 0, 1)
        self._write_savepoint(1, "ScenA", 0, 2)
        self._write_savepoint(1, "ScenA", 1, 1)

        async with self.sessions() as session:
            result = await discover(session, server)

        self.assertEqual(len(result), 1)
        scenario = result[0]
        self.assertEqual(scenario.scenario_dir, "ScenA")
        self.assertEqual([p.playthrough_nr for p in scenario.playthroughs], [1, 0])

        pt0 = next(p for p in scenario.playthroughs if p.playthrough_nr == 0)
        self.assertEqual([sp.save_point_nr for sp in pt0.save_points], [2, 1])
        pt1 = next(p for p in scenario.playthroughs if p.playthrough_nr == 1)
        self.assertEqual([sp.save_point_nr for sp in pt1.save_points], [1])

    async def test_corrupt_or_missing_meta_still_emits_record(self) -> None:
        server = await self._add_server()
        self._write_savepoint(1, "ScenA", 0, 1, skip_meta=True)
        self._write_savepoint(1, "ScenA", 0, 2, corrupt_meta=True)

        async with self.sessions() as session:
            result = await discover(session, server)

        save_points = [sp for pt in result[0].playthroughs for sp in pt.save_points]
        self.assertEqual(len(save_points), 2)
        self.assertTrue(all(sp.readable is False for sp in save_points))

    async def test_settings_and_non_savepoint_dirs_excluded(self) -> None:
        server = await self._add_server()
        self._write_savepoint(1, "ScenA", 0, 1)

        # A settings/ sibling of game/, never descended into.
        settings_meta_dir = self._settings_root(1) / "somejunk"
        settings_meta_dir.mkdir(parents=True, exist_ok=True)
        (settings_meta_dir / "meta-info.json").write_text("{}", encoding="utf-8")

        # A directory under the playthrough that isn't "savepoint*".
        self._write_savepoint(
            1, "ScenA", 0, 99, dir_name_override="backup001"
        )

        async with self.sessions() as session:
            result = await discover(session, server)

        save_points = [sp for pt in result[0].playthroughs for sp in pt.save_points]
        self.assertEqual(len(save_points), 1)
        self.assertEqual(save_points[0].save_point_nr, 1)
        names = {sp.dir_name for sp in save_points}
        self.assertNotIn("backup001", names)
        self.assertNotIn("somejunk", names)

    async def test_matches_current_scenario(self) -> None:
        server = await self._add_server(scenario_game_id="{5F5F49B8F5D935A2}Missions/Test.conf")
        self._write_savepoint(
            1, "ScenA", 0, 1, meta={"m_sMissionResource": "{5F5F49B8F5D935A2}Missions/Test.conf"}
        )
        self._write_savepoint(
            1, "ScenA", 0, 2, meta={"m_sMissionResource": "{OTHER}Missions/Other.conf"}
        )

        async with self.sessions() as session:
            result = await discover(session, server)

        save_points = {sp.save_point_nr: sp for pt in result[0].playthroughs for sp in pt.save_points}
        self.assertTrue(save_points[1].matches_current_scenario)
        self.assertFalse(save_points[2].matches_current_scenario)

    async def test_mod_drift_added_removed_and_unknown(self) -> None:
        server = await self._add_server()

        # Save between revision 1 (mods {A, B}) and revision 2 (mods {B, C});
        # the preceding revision is revision 1. Current mods are {B, C}.
        self._write_savepoint(
            1, "ScenA", 1, 1,
            meta={"m_iSavedAtUnix": 1_699_930_000},
        )
        # Identical-set save: revision + current both {B, C} only -> no drift.
        self._write_savepoint(
            1, "ScenA", 1, 2,
            meta={"m_iSavedAtUnix": 1_700_020_000},
        )
        # Save with NO preceding revision at all (predates every revision).
        self._write_savepoint(
            1, "ScenA", 0, 1,
            meta={"m_iSavedAtUnix": 1_600_000_000},
        )

        async with self.sessions() as session:
            session.add_all(
                [
                    ServerConfigRevision(
                        server_id=1,
                        revision=1,
                        snapshot={
                            "game": {
                                "mods": [
                                    {"modId": "AAA", "name": "Mod A"},
                                    {"modId": "BBB", "name": "Mod B"},
                                ]
                            }
                        },
                        created_at=datetime(2023, 11, 14, 0, 0, tzinfo=timezone.utc),
                    ),
                    ServerConfigRevision(
                        server_id=1,
                        revision=2,
                        snapshot={
                            "game": {
                                "mods": [
                                    {"modId": "BBB", "name": "Mod B"},
                                    {"modId": "CCC", "name": "Mod C"},
                                ]
                            }
                        },
                        created_at=datetime(2023, 11, 14, 6, 0, tzinfo=timezone.utc),
                    ),
                    ServerMod(server_id=1, mod_guid="BBB", mod_name="Mod B", load_order=0),
                    ServerMod(server_id=1, mod_guid="CCC", mod_name="Mod C", load_order=1),
                ]
            )
            await session.commit()

        # 1_699_930_000 is between the two revisions (revision 1 applies).
        # 1_700_020_000 is after both (revision 2 applies -> identical set).
        # 1_600_000_000 predates both.
        async with self.sessions() as session:
            result = await discover(session, server)

        save_points = {}
        for pt in result[0].playthroughs:
            for sp in pt.save_points:
                save_points[(pt.playthrough_nr, sp.save_point_nr)] = sp

        drifted = save_points[(1, 1)]
        self.assertFalse(drifted.mod_drift_unknown)
        self.assertIsNotNone(drifted.mod_drift)
        self.assertIn("Mod C", drifted.mod_drift.added)
        self.assertIn("Mod A", drifted.mod_drift.removed)

        no_drift = save_points[(1, 2)]
        self.assertFalse(no_drift.mod_drift_unknown)
        self.assertIsNone(no_drift.mod_drift)

        unknown = save_points[(0, 1)]
        self.assertTrue(unknown.mod_drift_unknown)
        self.assertIsNone(unknown.mod_drift)


if __name__ == "__main__":
    unittest.main()
