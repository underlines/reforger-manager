"""Settings request/response schemas (GET/PATCH /api/settings)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SettingsOut(BaseModel):
    nightly_check_enabled: bool
    nightly_check_hour: int
    log_spam_patterns: list[str]


class SettingsUpdate(BaseModel):
    """PATCH semantics: every field optional; only provided keys change."""

    nightly_check_enabled: bool | None = None
    nightly_check_hour: int | None = Field(default=None, ge=0, le=23)
    log_spam_patterns: list[str] | None = None
