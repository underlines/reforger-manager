"""Server DB row -> Arma Reforger ``config.json`` dict, written to
``CONFIGS_DIR/<server_id>.json``.

Field names verified 2026-09-02 against the Bohemia server-config documentation
(community.bistudio.com/wiki/Arma_Reforger:Server_Config; the BI wiki blocks
automated fetches, cross-checked against xgamingserver.com's config reference
which mirrors it):

* top level: ``bindAddress`` ``bindPort`` ``publicAddress`` ``publicPort``
  ``a2s`` ``rcon`` ``game`` ``operating`` (also optional ``dedicatedServerId``
  ``region`` — omitted here).
* ``a2s``  : ``address`` (req), ``port`` (default 17777).
* ``rcon`` : ``address`` (req), ``password`` (req, >= 3 chars, no spaces),
  ``port`` (default 19999), ``permission`` ("admin" | "monitor"),
  ``maxClients`` (default 16, 1..16), ``blacklist`` [], ``whitelist`` [].
  -> confirmed key names: address / port / password / permission / maxClients
     / blacklist / whitelist. (RCON transport is BattlEye.)
* ``game`` : ``name`` ``password`` ``passwordAdmin`` ``admins`` ``scenarioId``
  ``maxPlayers`` ``visible`` ``crossPlatform`` ``supportedPlatforms``
  ``modsRequiredByDefault`` ``gameProperties`` ``mods``.
* ``game.mods[]`` : ``modId`` (req), ``name`` (opt), ``version`` (opt — only
  emitted when a pin is set).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..core.config import settings

DEFAULT_GAME_PROPERTIES: dict = {
    "serverMaxViewDistance": 2500,
    "serverMinGrassDistance": 50,
    "networkViewDistance": 1000,
    "disableThirdPerson": False,
    "fastValidation": True,
    "battlEye": True,
    "VONDisableUI": True,
    "VONDisableDirectSpeechUI": True,
}


@dataclass
class ModEntry:
    mod_id: str
    name: str | None = None
    version: str | None = None  # set only when pinned

    def to_dict(self) -> dict:
        entry: dict = {"modId": self.mod_id}
        if self.name:
            entry["name"] = self.name
        if self.version:
            entry["version"] = self.version
        return entry


def build_config(server, mods: list[ModEntry]) -> dict:
    """Assemble the config.json dict for a ``models.server.Server`` row."""
    game_properties = dict(DEFAULT_GAME_PROPERTIES)
    if server.game_properties:
        game_properties.update(server.game_properties)

    config: dict = {
        "bindAddress": server.bind_address or "",
        "bindPort": server.bind_port,
        "publicAddress": server.public_address or "",
        "publicPort": server.public_port,
        "a2s": {
            "address": server.a2s_address or "0.0.0.0",
            "port": server.a2s_port,
        },
        "game": {
            "name": server.game_name or server.name,
            "password": server.game_password or "",
            "passwordAdmin": server.admin_password or "",
            "scenarioId": server.scenario_game_id or "",
            "maxPlayers": server.max_players,
            "visible": bool(server.visible),
            "supportedPlatforms": ["PLATFORM_PC"],
            "gameProperties": game_properties,
            "mods": [m.to_dict() for m in mods],
        },
    }

    if server.rcon_enabled and server.rcon_password:
        config["rcon"] = {
            "address": server.rcon_address or "0.0.0.0",
            "port": server.rcon_port,
            "password": server.rcon_password,
            "permission": server.rcon_permission or "admin",
            "maxClients": server.rcon_max_clients or 16,
            "blacklist": [],
            "whitelist": [],
        }

    # Verbatim escape hatch, merged last.
    if server.extra_config:
        _deep_merge(config, server.extra_config)

    return config


def _deep_merge(base: dict, overlay: dict) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def write_config(server_id: int, config: dict) -> Path:
    settings.configs_dir.mkdir(parents=True, exist_ok=True)
    path = settings.config_path(server_id)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def server_mod_entries(server) -> list[ModEntry]:
    """Build the ordered mod list from ``server.mods`` (per-server pin wins)."""
    entries: list[ModEntry] = []
    for sm in sorted(server.mods, key=lambda x: x.load_order):
        if not sm.enabled:
            continue
        entries.append(
            ModEntry(
                mod_id=sm.mod_guid,
                name=sm.mod_name,
                version=sm.pinned_version or None,
            )
        )
    return entries
