"""servers.game_admins column (persistent per-server admin list)

Revision ID: 0004_server_game_admins
Revises: 0003_mcp_tokens
Create Date: 2026-09-07

Adds the nullable JSON ``game_admins`` column to ``servers``. It holds a list of
identity ids (UUID strings from ``#players``) that the generated ``config.json``
emits as ``game.admins``. Fresh databases get the column from ``create_all``;
this migration covers existing ones.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_server_game_admins"
down_revision: Union[str, Sequence[str], None] = "0003_mcp_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("servers", sa.Column("game_admins", _json(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "game_admins")
