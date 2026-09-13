"""Persistence block in the generated config.json.

``game.gameProperties.persistence`` carries the five persistence columns; when
``Server.persistence_enabled`` is False the block is withheld and
``gameProperties.missionHeader.m_eSaveTypes`` is set to 0 instead. ``extra_config``
still wins the deep-merge. Deterministic unittest style (``test_game_admins``),
no session, no app lifespan.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.models.server import Server
from app.servers import config_gen


class BuildConfigPersistenceTests(unittest.TestCase):
    def test_enabled_emits_persistence_block(self) -> None:
        server = Server(
            name="srv",
            persistence_enabled=True,
            auto_save_interval=15,
            save_retention=32,
            load_session_save=True,
            keep_session_save=True,
            hive_id=4096,
        )
        config = config_gen.build_config(server, [])
        persistence = config["game"]["gameProperties"]["persistence"]
        self.assertEqual(persistence["autoSaveInterval"], 15)
        self.assertEqual(persistence["saveRetention"], 32)
        self.assertEqual(persistence["loadSessionSave"], True)
        self.assertEqual(persistence["keepSessionSave"], True)
        self.assertEqual(persistence["hiveId"], 4096)

    def test_disabled_emits_mission_header_save_types_and_no_persistence(self) -> None:
        server = Server(name="srv", persistence_enabled=False)
        config = config_gen.build_config(server, [])
        game_properties = config["game"]["gameProperties"]
        self.assertEqual(game_properties["missionHeader"]["m_eSaveTypes"], 0)
        self.assertNotIn("persistence", game_properties)

    def test_extra_config_override_wins_merge(self) -> None:
        server = Server(
            name="srv",
            persistence_enabled=True,
            auto_save_interval=15,
            extra_config={
                "game": {"gameProperties": {"persistence": {"autoSaveInterval": 42}}}
            },
        )
        config = config_gen.build_config(server, [])
        self.assertEqual(
            config["game"]["gameProperties"]["persistence"]["autoSaveInterval"], 42
        )


if __name__ == "__main__":
    unittest.main()