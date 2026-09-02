"""initial schema - all Phase 2 core tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-02

Produced by ``alembic revision --autogenerate`` against SQLite, then hand-tidied:
the JSON columns render as ``JSON`` with a Postgres ``JSONB`` variant (so the
production Postgres 17 gets real JSONB), the ``astext_type`` render artefact is
removed, and index creation is plain ``op.create_index`` instead of SQLite batch
mode. Verified: ``upgrade head`` on SQLite, and ``upgrade --sql`` offline render
against a ``postgresql://`` URL.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


_NOW = sa.text("CURRENT_TIMESTAMP")
_JOB_STATE = sa.Enum(
    "queued", "running", "succeeded", "failed", "cancelled",
    name="jobstate", native_enum=False, length=16,
)
_API_STATE = sa.Enum(
    "ok", "not_found", "unchecked",
    name="apistate", native_enum=False, length=16,
)


def upgrade() -> None:
    op.create_table(
        "engine",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("installed_build", sa.String(32)),
        sa.Column("installed_version", sa.String(64)),
        sa.Column("target_build", sa.String(32)),
        sa.Column("beta_key", sa.String(64)),
        sa.Column("latest_build", sa.String(32)),
        sa.Column("latest_version", sa.String(64)),
        sa.Column("latest_time_updated", sa.DateTime(timezone=True)),
        sa.Column("update_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_checked", sa.DateTime(timezone=True)),
        sa.Column("last_updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("state", _JOB_STATE, nullable=False),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("current_step", sa.String(255)),
        sa.Column("params", _json()),
        sa.Column("result", _json()),
        sa.Column("error", sa.Text()),
        sa.Column("log_tail", _json()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_state", "jobs", ["state"])

    op.create_table(
        "modpacks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "mods",
        sa.Column("guid", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("summary", sa.Text()),
        sa.Column("installed_version", sa.String(64)),
        sa.Column("latest_version", sa.String(64)),
        sa.Column("latest_game_version", sa.String(64)),
        sa.Column("size", sa.BigInteger()),
        sa.Column("thumbnail", sa.String(512)),
        sa.Column("tags", _json()),
        sa.Column("last_checked", sa.DateTime(timezone=True)),
        sa.Column("is_unlisted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_obsolete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("api_state", _API_STATE, nullable=False, server_default="unchecked"),
        sa.Column("api_checked_at", sa.DateTime(timezone=True)),
        sa.Column("pinned_version", sa.String(64)),
        sa.Column("pinned_at_build", sa.String(32)),
        sa.Column("pinned_reason", sa.Text()),
        sa.Column("pinned_at", sa.DateTime(timezone=True)),
        sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("guid"),
    )

    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_favourite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("scenario_game_id", sa.String(255)),
        sa.Column("game_name", sa.String(255)),
        sa.Column("game_password", sa.String(128)),
        sa.Column("admin_password", sa.String(128)),
        sa.Column("max_players", sa.Integer(), nullable=False, server_default="32"),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("game_properties", _json()),
        sa.Column("extra_config", _json()),
        sa.Column("bind_address", sa.String(64), nullable=False, server_default="0.0.0.0"),
        sa.Column("bind_port", sa.Integer(), nullable=False, server_default="2001"),
        sa.Column("public_address", sa.String(128)),
        sa.Column("public_port", sa.Integer(), nullable=False, server_default="2001"),
        sa.Column("a2s_address", sa.String(64), nullable=False, server_default="0.0.0.0"),
        sa.Column("a2s_port", sa.Integer(), nullable=False, server_default="17777"),
        sa.Column("rcon_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("rcon_address", sa.String(64), nullable=False, server_default="0.0.0.0"),
        sa.Column("rcon_port", sa.Integer(), nullable=False, server_default="19999"),
        sa.Column("rcon_password", sa.String(128)),
        sa.Column("rcon_permission", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("rcon_max_clients", sa.Integer(), nullable=False, server_default="16"),
        sa.Column("config", _json()),
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_running", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pid", sa.Integer()),
        sa.Column("last_state", sa.String(32)),
        sa.Column("last_exit_code", sa.Integer()),
        sa.Column("last_diagnosis", _json()),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_stopped_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "mod_dependencies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mod_guid", sa.String(32), nullable=False),
        sa.Column("depends_on_guid", sa.String(32), nullable=False),
        sa.Column("depends_on_name", sa.String(255)),
        sa.Column("required_version", sa.String(64)),
        sa.Column("source", sa.String(16)),
        sa.ForeignKeyConstraint(["mod_guid"], ["mods.guid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mod_guid", "depends_on_guid", name="uq_mod_dependency"),
    )
    op.create_index("ix_mod_dependencies_mod_guid", "mod_dependencies", ["mod_guid"])
    op.create_index("ix_mod_dependencies_depends_on_guid", "mod_dependencies", ["depends_on_guid"])

    op.create_table(
        "mod_scenarios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mod_guid", sa.String(32), nullable=False),
        sa.Column("game_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("game_mode", sa.String(64)),
        sa.Column("player_count", sa.Integer()),
        sa.ForeignKeyConstraint(["mod_guid"], ["mods.guid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mod_guid", "game_id", name="uq_mod_scenario"),
    )
    op.create_index("ix_mod_scenarios_mod_guid", "mod_scenarios", ["mod_guid"])
    op.create_index("ix_mod_scenarios_game_id", "mod_scenarios", ["game_id"])

    op.create_table(
        "modpack_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("modpack_id", sa.Integer(), nullable=False),
        sa.Column("mod_guid", sa.String(32), nullable=False),
        sa.Column("load_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["modpack_id"], ["modpacks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("modpack_id", "mod_guid", name="uq_modpack_item"),
    )
    op.create_index("ix_modpack_items_modpack_id", "modpack_items", ["modpack_id"])
    op.create_index("ix_modpack_items_mod_guid", "modpack_items", ["mod_guid"])

    op.create_table(
        "server_config_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", _json(), nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("created_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "revision", name="uq_server_config_revision"),
    )
    op.create_index(
        "ix_server_config_revisions_server_id", "server_config_revisions", ["server_id"]
    )

    op.create_table(
        "server_mods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("mod_guid", sa.String(32), nullable=False),
        sa.Column("mod_name", sa.String(255)),
        sa.Column("load_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pinned_version", sa.String(64)),
        sa.Column("pinned_at_build", sa.String(32)),
        sa.Column("pinned_reason", sa.Text()),
        sa.Column("pinned_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "mod_guid", name="uq_server_mod"),
    )
    op.create_index("ix_server_mods_server_id", "server_mods", ["server_id"])
    op.create_index("ix_server_mods_mod_guid", "server_mods", ["mod_guid"])


def downgrade() -> None:
    op.drop_table("server_mods")
    op.drop_table("server_config_revisions")
    op.drop_table("modpack_items")
    op.drop_table("mod_scenarios")
    op.drop_table("mod_dependencies")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    op.drop_table("servers")
    op.drop_table("mods")
    op.drop_table("modpacks")
    op.drop_index("ix_jobs_state", table_name="jobs")
    op.drop_index("ix_jobs_kind", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("engine")
