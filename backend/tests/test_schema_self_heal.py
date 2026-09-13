"""Additive missing-column self-heal for ``DB_MIGRATE_ON_STARTUP=create_all``.

Regression coverage for the Sprint 10 prod outage: ``Base.metadata.create_all``
only creates missing tables, never adds a column to a table that already
exists. ``app.main._sync_missing_columns`` closes that gap for additive
(server_default-carrying) columns, and must not touch anything it can't do
safely.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).parents[1]))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import Base
from app.main import _sync_missing_columns

_SPRINT_10_COLUMNS = {
    "persistence_enabled",
    "auto_save_interval",
    "save_retention",
    "load_session_save",
    "keep_session_save",
    "hive_id",
    "save_mode",
    "save_pinned_uuid",
    "save_selection_sticky",
}


def _old_servers_table(metadata: sa.MetaData) -> sa.Table:
    """The real `servers` table shape, minus exactly the columns Sprint 10
    added — i.e. what a pre-existing `create_all`-built prod DB actually has."""
    real = Base.metadata.tables["servers"]
    columns = [c.copy() for c in real.columns if c.name not in _SPRINT_10_COLUMNS]
    return sa.Table("servers", metadata, *columns)


class SchemaSelfHealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_missing_columns_are_added_with_their_defaults(self) -> None:
        old_schema = sa.MetaData()
        table = _old_servers_table(old_schema)
        async with self.engine.begin() as conn:
            await conn.run_sync(old_schema.create_all)
            await conn.execute(
                sa.insert(table).values(id=1, name="srv", scenario_game_id="x")
            )

        async with self.engine.begin() as conn:
            await conn.run_sync(_sync_missing_columns)

        async with self.engine.begin() as conn:

            def _columns(sync_conn) -> set[str]:
                return {c["name"] for c in sa.inspect(sync_conn).get_columns("servers")}

            columns = await conn.run_sync(_columns)

        for expected in _SPRINT_10_COLUMNS:
            self.assertIn(expected, columns)

        # The pre-existing row survives, and the new NOT NULL columns
        # backfilled from their server_default rather than erroring.
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "SELECT name, persistence_enabled, save_retention, save_mode "
                        "FROM servers WHERE id = 1"
                    )
                )
            ).one()
        self.assertEqual(row.name, "srv")
        self.assertEqual(bool(row.persistence_enabled), True)
        self.assertEqual(row.save_retention, 10)
        self.assertEqual(row.save_mode, "latest")

    async def test_no_missing_columns_is_a_no_op(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Second pass over an already-current schema must not raise.
            await conn.run_sync(_sync_missing_columns)

    async def test_not_null_column_without_server_default_is_skipped_not_crashed(
        self,
    ) -> None:
        # `is_favourite` is NOT NULL with only a client-side `default=`, no
        # `server_default` — the class of column this self-heal cannot safely
        # add to a table with existing rows. It must be skipped, not raised.
        old_schema = sa.MetaData()
        real = Base.metadata.tables["servers"]
        columns = [
            c.copy() for c in real.columns if c.name not in ({"is_favourite"} | _SPRINT_10_COLUMNS)
        ]
        table = sa.Table("servers", old_schema, *columns)
        async with self.engine.begin() as conn:
            await conn.run_sync(old_schema.create_all)
            await conn.execute(
                sa.insert(table).values(id=1, name="srv", scenario_game_id="x")
            )

        async with self.engine.begin() as conn:
            await conn.run_sync(_sync_missing_columns)  # must not raise

        async with self.engine.begin() as conn:

            def _columns(sync_conn) -> set[str]:
                return {c["name"] for c in sa.inspect(sync_conn).get_columns("servers")}

            columns_after = await conn.run_sync(_columns)

        self.assertNotIn("is_favourite", columns_after)
        for expected in _SPRINT_10_COLUMNS:
            self.assertIn(expected, columns_after)


if __name__ == "__main__":
    unittest.main()
