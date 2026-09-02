from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EngineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    installed_build: str | None = None
    installed_version: str | None = None
    target_build: str | None = None
    beta_key: str | None = None
    latest_build: str | None = None
    latest_version: str | None = None
    latest_time_updated: datetime | None = None
    update_available: bool = False
    last_checked: datetime | None = None
    last_updated_at: datetime | None = None
