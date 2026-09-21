"""app_settings.default_scenarios column

Revision ID: 0006_default_scenarios
Revises: 0005_server_persistence_and_save_selection
Create Date: 2026-09-21

Adds the operator-editable list of official scenarios offered by the
ScenarioField picker regardless of a definition's mods. Nullable, no
``server_default`` — ``app.core.app_settings.get_app_settings`` backfills the
built-in 31-scenario list into any row that reaches this column as ``NULL``
(a fresh row, or an existing one that picked the column up via ``create_all``
self-heal), so no data migration is needed here.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_default_scenarios"
down_revision: Union[str, Sequence[str], None] = "0005_server_persistence_and_save_selection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("default_scenarios", _json(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "default_scenarios")
