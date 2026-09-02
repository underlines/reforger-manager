"""app_settings singleton table (runtime-mutable settings)

Revision ID: 0002_app_settings
Revises: 0001_initial
Create Date: 2026-09-02

Adds the single-row settings table backing ``GET/PATCH /api/settings``. The row
itself is lazily seeded from env defaults by ``app.core.app_settings`` on first
read, so the migration only creates the (initially empty) table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_app_settings"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nightly_check_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("nightly_check_hour", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("log_spam_patterns", _json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
