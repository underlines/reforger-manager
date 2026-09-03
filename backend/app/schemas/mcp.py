from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator


class McpTokenCreate(BaseModel):
    label: str
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _expires_at_must_be_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if value <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return value


class McpTokenOut(BaseModel):
    # Never carries ``token_hash`` — the digest must not leave the server.
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class McpTokenCreatedOut(McpTokenOut):
    # The raw token, in this one response only; it is never retrievable again.
    token: str
