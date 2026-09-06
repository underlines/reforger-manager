from __future__ import annotations

from pydantic import BaseModel, Field


class BanCreateIn(BaseModel):
    """Body of `POST /api/servers/{id}/bans`.

    ``identifier`` is a single token: a playerId / identityId / name-without-
    spaces.  Multi-word player names must go through the raw ``POST /rcon``
    route (``?raw=1``) or by using an identityId as the identifier.
    """

    identifier: str = Field(min_length=1, max_length=128)
    duration_seconds: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=4096)


class BanCreateOut(BaseModel):
    echo: str


class BanRow(BaseModel):
    ban_id: str
    uid: str
    duration: str
    raw: str


class BanListOut(BaseModel):
    bans: list[BanRow]
    raw: str
    page: int


class RconCommandOut(BaseModel):
    response: str