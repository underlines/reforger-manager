"""Re-export every mapped class and Base so ``import app.models`` registers the
full metadata (Alembic autogenerate + create_all rely on this).
"""

from ..core.db import Base
from .app_settings import APP_SETTINGS_SINGLETON_ID, AppSettings
from .base import (
    TERMINAL_JOB_STATES,
    ApiState,
    JobState,
    TimestampMixin,
)
from .engine import ENGINE_SINGLETON_ID, Engine
from .job import Job
from .mod import Mod, ModDependency, ModScenario
from .modpack import Modpack, ModpackItem
from .server import Server, ServerConfigRevision, ServerMod
from .user import User

__all__ = [
    "Base",
    "TimestampMixin",
    "ApiState",
    "JobState",
    "TERMINAL_JOB_STATES",
    "AppSettings",
    "APP_SETTINGS_SINGLETON_ID",
    "Engine",
    "ENGINE_SINGLETON_ID",
    "Job",
    "Mod",
    "ModDependency",
    "ModScenario",
    "Modpack",
    "ModpackItem",
    "Server",
    "ServerMod",
    "ServerConfigRevision",
    "User",
]
