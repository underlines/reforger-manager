"""servers persistence + save-selection columns

Revision ID: 0005_server_persistence_and_save_selection
Revises: 0004_server_game_admins
Create Date: 2026-09-13

Adds the Reforger persistence / save-system columns to ``servers``:
``persistence_enabled``, ``auto_save_interval``, ``save_retention``,
``load_session_save``, ``keep_session_save``, ``hive_id`` (operator-set config)
plus the runtime save-selection trio ``save_mode``, ``save_pinned_uuid`` and
``save_selection_sticky``. Every non-nullable column gets a ``server_default`` so
existing rows migrate cleanly. Fresh databases get the columns from
``create_all``; this migration covers existing ones.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_server_persistence_and_save_selection"
down_revision: Union[str, Sequence[str], None] = "0004_server_game_admins"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "persistence_enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.add_column(
        "servers",
        sa.Column("auto_save_interval", sa.Integer(), server_default=sa.text("10"), nullable=False),
    )
    op.add_column(
        "servers",
        sa.Column("save_retention", sa.Integer(), server_default=sa.text("10"), nullable=False),
    )
    op.add_column(
        "servers",
        sa.Column(
            "load_session_save", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.add_column(
        "servers",
        sa.Column(
            "keep_session_save", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "servers",
        sa.Column("hive_id", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "servers",
        sa.Column(
            "save_mode",
            sa.String(16),
            server_default=sa.text("'latest'"),
            nullable=False,
        ),
    )
    op.add_column("servers", sa.Column("save_pinned_uuid", sa.String(64), nullable=True))
    op.add_column(
        "servers",
        sa.Column(
            "save_selection_sticky", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "save_selection_sticky")
    op.drop_column("servers", "save_pinned_uuid")
    op.drop_column("servers", "save_mode")
    op.drop_column("servers", "hive_id")
    op.drop_column("servers", "keep_session_save")
    op.drop_column("servers", "load_session_save")
    op.drop_column("servers", "save_retention")
    op.drop_column("servers", "auto_save_interval")
    op.drop_column("servers", "persistence_enabled")