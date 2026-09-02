"""Application settings.

Every key here is read from the process environment (see ``.env.example``).
pydantic-settings is case-insensitive, so ``DATABASE_URL`` in the env maps to
``database_url`` below. Unknown env keys are ignored so the shared ``.env`` can
carry ``POSTGRES_*`` values the db container needs but the app does not.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- general ---
    tz: str = "UTC"
    app_name: str = "Reforger Manager"
    app_version: str = "0.2.0"

    # --- database ---
    # Async SQLAlchemy DSN. Postgres in production; sqlite+aiosqlite works for
    # local import-checks / tests.
    database_url: str = (
        "postgresql+asyncpg://reforger:reforger@localhost:5432/reforger_manager"
    )
    postgres_user: str = "reforger"
    postgres_password: str = ""
    postgres_db: str = "reforger_manager"
    # "create_all" (default, no external tooling needed) or "alembic".
    db_migrate_on_startup: str = "create_all"

    # --- auth ---
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 12
    admin_username: str = "admin"
    admin_password: str = "admin"

    # --- steam / engine ---
    steam_app_id: int = 1874900
    steamcmd_path: str = "/home/steam/steamcmd/steamcmd.sh"
    steamcmd_net_info_url: str = "https://api.steamcmd.net/v1/info/1874900"

    # --- ports ---
    web_port: int = 18090
    rcon_default_port: int = 19999

    # --- in-container paths (bind mounts) ---
    server_dir: Path = Path("/home/steam/data/server")
    mods_dir: Path = Path("/home/steam/data/mods")
    profiles_dir: Path = Path("/home/steam/data/profiles")
    configs_dir: Path = Path("/home/steam/data/configs")

    # --- behaviour ---
    engine_check_on_startup: bool = True
    # Opt-in only: this check makes Workshop API requests and scans the addon cache.
    nightly_check_enabled: bool = False
    nightly_check_hour: int = Field(default=3, ge=0, le=23)
    nightly_check_interval_seconds: int = 24 * 60 * 60
    reforger_binary_name: str = "ArmaReforgerServer"
    stop_grace_seconds: float = 20.0
    job_log_ring: int = 200
    # Explicit allow-list (S19 / PLAN G33). The SPA is served same-origin by
    # FastAPI, so this only affects the Vite dev server and any stray
    # cross-origin caller. Keep localhost:5173 or `npm run dev` breaks; add any
    # externally served origin via the CORS_ORIGINS env var (JSON array).
    cors_origins: list[str] = ["http://localhost:5173"]

    # ------------------------------------------------------------------ derived
    @property
    def appmanifest_path(self) -> Path:
        return self.server_dir / "steamapps" / f"appmanifest_{self.steam_app_id}.acf"

    @property
    def reforger_binary(self) -> Path:
        return self.server_dir / self.reforger_binary_name

    @property
    def sync_database_url(self) -> str:
        """Blocking DSN (used by Alembic's run-in-thread upgrade)."""
        return (
            self.database_url.replace("+asyncpg", "")
            .replace("+psycopg", "")
            .replace("+aiosqlite", "")
        )

    def config_path(self, server_id: int) -> Path:
        return self.configs_dir / f"{server_id}.json"

    def profile_dir(self, server_id: int) -> Path:
        return self.profiles_dir / str(server_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
