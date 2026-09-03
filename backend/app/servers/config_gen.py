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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models import Mod

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
    """Build the ordered mod list from ``server.mods`` alone (per-server pin wins).

    Explicit assignments only — **no dependency expansion**. Kept for callers and
    tests that want exactly what the user assigned; the server-start and
    config-preview paths use :func:`resolved_mod_entries` instead.
    """
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


async def resolved_mod_entries(session: AsyncSession, server) -> list[ModEntry]:
    """Full, load-ordered mod list for ``config.json`` — closure included.

    The Reforger dedicated server advertises the mod set from its config to the
    lobby and to joining clients; when that list is missing a mod's
    dependencies, client admission fails (``RoomsAcceptPlayerS2S`` /
    ``InvalidSessionTicket``) even though the engine auto-mounts the deps
    server-side. So the emitted list is the resolved dependency **closure** of
    the enabled assignments:

    * dependencies come before the mod that needs them (post-order DFS);
    * every GUID appears once — a mod assigned explicitly *and* pulled in as a
      dependency is not duplicated;
    * engine-owned GUIDs (base game / ``core``) are dropped;
    * a dependency that cannot be loaded at all (no local dir, unresolvable on
      the Workshop) is dropped rather than emitted — listing it would make the
      engine refuse to start ("addons are not downloadable"). Pre-flight
      surfaces that case separately;
    * ``name`` and the version pin are taken from the explicit ``ServerMod``
      row when there is one, otherwise from the library ``Mod`` row.
    """
    # late import: config_gen <- supervisor <- mods.downloader <- mods package
    from ..mods.resolve import ENGINE_BUILTIN_GUIDS, resolve_dependencies

    explicit = [
        sm for sm in sorted(server.mods, key=lambda x: (x.load_order, x.mod_guid))
        if sm.enabled
    ]
    roots = [sm.mod_guid.upper() for sm in explicit]
    if not roots:
        return []

    tree = await resolve_dependencies(session, roots, use_api=False, use_disk=True)
    node_by_guid = {node.guid: node for node in tree.nodes}
    child_map: dict[str, list[str]] = {}
    for parent, child in tree.edges:
        child_map.setdefault(parent, []).append(child)

    ordered: list[str] = []
    seen: set[str] = set()
    on_stack: set[str] = set()

    def visit(guid: str) -> None:
        if guid in seen or guid in on_stack:
            return
        on_stack.add(guid)
        for child in child_map.get(guid, ()):
            visit(child)
        on_stack.discard(guid)
        seen.add(guid)
        ordered.append(guid)

    for root in roots:
        visit(root)

    explicit_by_guid = {sm.mod_guid.upper(): sm for sm in explicit}
    lib = (
        {
            mod.guid: mod
            for mod in (
                await session.execute(select(Mod).where(Mod.guid.in_(list(seen))))
            ).scalars()
        }
        if seen
        else {}
    )

    entries: list[ModEntry] = []
    for guid in ordered:
        if guid in ENGINE_BUILTIN_GUIDS:
            continue
        sm = explicit_by_guid.get(guid)
        node = node_by_guid.get(guid)
        libmod = lib.get(guid)

        if sm is None:
            loadable = (
                (libmod is not None and libmod.is_local)
                or (libmod is not None and libmod.api_state and libmod.api_state.value == "ok")
                or (node is not None and node.state == "ok")
            )
            if not loadable:
                continue

        name = (
            (sm.mod_name if sm is not None else None)
            or (node.name if node is not None else None)
            or (libmod.name if libmod is not None else None)
        )
        version = None
        if sm is not None and sm.pinned_version:
            version = sm.pinned_version
        elif libmod is not None and libmod.pinned_version:
            version = libmod.pinned_version

        entries.append(ModEntry(mod_id=guid, name=name, version=version))

    return entries
