"""mcp_tokens table (MCP bearer tokens)

Revision ID: 0003_mcp_tokens
Revises: 0002_app_settings
Create Date: 2026-09-03

Adds the ``mcp_tokens`` table backing the MCP administration interface
(``Authorization: Bearer rfm_<token>``). Only the sha256 hex digest of each
token is stored; ``GET/POST/DELETE /api/mcp/tokens`` manage the rows and the
raw token is shown exactly once in the 201 create response.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_mcp_tokens"
down_revision: Union[str, Sequence[str], None] = "0002_app_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "mcp_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_tokens_token_hash", "mcp_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tokens_token_hash", table_name="mcp_tokens")
    op.drop_table("mcp_tokens")
