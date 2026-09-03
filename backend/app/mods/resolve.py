"""Dependency resolution — API first, local ``addon.gproj`` as the fallback.

Used both by the mod-sync job / API routes now and by pre-flight later.

For every GUID reached by the BFS:

* If the mod **resolves** — a ``mods`` row with ``api_state == "ok"``, or a live
  ``get_dependencies`` 200 — its children come from the API (``via = "api"``).
* If the parent **404s** or has **no API record**, its children come from the
  on-disk ``addon.gproj`` list persisted as ``mod_dependencies`` rows with
  ``source == "gproj"`` (``via = "gproj"``).
* A GUID with **neither a local dir nor an API record** is still emitted, as a
  node with ``state = "unresolved"`` (``via = "unknown"``). That is the point of
  this helper — server 9's blocked parent must still produce *something*.

No exception is raised for missing data.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Mod, ModDependency
from .scanner import parse_gproj, resolve_addon_dir
from .workshop import ModNotFound, WorkshopClient, WorkshopError, workshop

logger = logging.getLogger("reforger.mods.resolve")

_MAX_DEPTH = 12

# Engine-owned addon GUIDs that appear in ``addon.gproj`` dependency blocks but
# are not Workshop mods: the base-game data package and the ``core`` package.
# They are always present, never downloadable, and must never reach a server
# ``config.json`` ``mods[]`` list.
ENGINE_BUILTIN_GUIDS = frozenset({
    "58D0FB3206B6F859",  # data/ArmaReforger.gproj
    "5614BBCCBB55ED1C",  # core/core.gproj
})


def _disk_gproj_children(guid: str) -> list[tuple[str, str | None]]:
    """Dependency GUIDs read straight from the on-disk ``addon.gproj``.

    The offline fallback used when neither the Workshop API nor a persisted
    ``mod_dependencies`` row can supply a mod's dependency list — e.g. a
    dependency that is present in the addon cache but was never enriched by a
    ``mod_sync``. Returns ``[]`` when the addon dir or file is absent.
    """
    try:
        addon_dir = resolve_addon_dir(guid)
        if addon_dir is None:
            return []
        return [
            (dep.upper(), None)
            for dep in parse_gproj(addon_dir).get("dep_guids", [])
            if dep.upper() != guid.upper()
        ]
    except OSError:
        return []


@dataclass
class ResolvedNode:
    guid: str
    name: str | None
    via: str          # "api" | "gproj" | "unknown"  (how this node's deps were read)
    state: str        # "ok" | "not_found" | "unresolved"  (this node itself)
    depth: int


@dataclass
class ResolvedTree:
    roots: list[str]
    nodes: list[ResolvedNode] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (parent, child)

    def as_dict(self) -> dict:
        return {
            "roots": self.roots,
            "nodes": [
                {
                    "guid": n.guid,
                    "name": n.name,
                    "via": n.via,
                    "state": n.state,
                    "depth": n.depth,
                }
                for n in self.nodes
            ],
            "edges": [{"from": a, "to": b} for a, b in self.edges],
        }


async def _gproj_children(session: AsyncSession, guid: str) -> list[tuple[str, str | None]]:
    rows = (
        await session.execute(
            select(ModDependency).where(ModDependency.mod_guid == guid)
        )
    ).scalars().all()
    # Prefer richer "api" rows if both are somehow present for the same edge.
    best: dict[str, tuple[str, str | None]] = {}
    for r in rows:
        cur = best.get(r.depends_on_guid)
        if cur is None or (r.source == "api" and cur[0] != "api"):
            best[r.depends_on_guid] = (r.source or "gproj", r.depends_on_name)
    return [(g, name) for g, (_src, name) in best.items()]


async def resolve_dependencies(
    session: AsyncSession,
    root_guids: list[str],
    *,
    client: WorkshopClient | None = None,
    use_api: bool = True,
    use_disk: bool = False,
    max_depth: int = _MAX_DEPTH,
) -> ResolvedTree:
    """BFS the dependency graph from ``root_guids``. See module docstring.

    ``use_disk`` adds a final offline fallback: a node with no API record and no
    persisted ``mod_dependencies`` edge still has its dependency list read from
    the on-disk ``addon.gproj`` when the addon dir exists. Lets the closure be
    complete before a ``mod_sync`` has enriched every dependency.
    """
    client = client or workshop
    roots = [g.upper() for g in root_guids if g]
    tree = ResolvedTree(roots=roots)

    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque((g, 0) for g in roots)
    edge_seen: set[tuple[str, str]] = set()

    while queue:
        guid, depth = queue.popleft()
        if guid in visited:
            continue
        visited.add(guid)

        mod: Mod | None = await session.get(Mod, guid)
        name = mod.name if mod is not None else None
        is_local = bool(mod is not None and mod.is_local)
        api_ok = bool(mod is not None and mod.api_state and mod.api_state.value == "ok")
        api_not_found = bool(
            mod is not None and mod.api_state and mod.api_state.value == "not_found"
        )

        children: list[tuple[str, str | None]] = []
        via = "unknown"
        state = "unresolved"

        # 1) API path — when the mod already resolved, or we can try live.
        api_children: list[tuple[str, str | None]] | None = None
        if use_api and depth < max_depth and (api_ok or (mod is None) or not api_not_found):
            try:
                deps = await client.get_dependencies(guid)
                api_children = [
                    (str(d.get("id")).upper(), d.get("name"))
                    for d in deps
                    if d.get("id")
                ]
                via = "api"
                state = "ok"
                if name is None:
                    # get_mod is cached; cheap, and fills the node name.
                    try:
                        mo = await client.get_mod(guid)
                        name = mo.get("name") or name
                    except (WorkshopError, ModNotFound):
                        pass
            except ModNotFound:
                api_children = None
            except WorkshopError as exc:
                logger.info("resolve: transient API error for %s: %s", guid, exc)
                api_children = None

        if api_children is not None:
            # API is authoritative for the child list, but a locally-declared
            # gproj dependency the API omits (e.g. a GUID with no Workshop mod
            # record, like Kingmaker's 58D0FB3206B6F859) must still be surfaced
            # so pre-flight can flag it. Merge those in, tagged as gproj-sourced.
            children = list(api_children)
            api_child_guids = {g for g, _n in api_children}
            for g, n in await _gproj_children(session, guid):
                if g not in api_child_guids:
                    children.append((g, n))
        else:
            # 2) local gproj fallback — persisted rows first, then, when asked,
            #    the on-disk addon.gproj (a dependency the sync never enriched).
            gproj_children = await _gproj_children(session, guid)
            disk_present = False
            if not gproj_children and use_disk and depth < max_depth:
                disk_children = _disk_gproj_children(guid)
                disk_present = resolve_addon_dir(guid) is not None
                if disk_children:
                    gproj_children = disk_children
            if gproj_children:
                children = gproj_children
                via = "gproj"
                if disk_present:
                    state = "ok"
                else:
                    state = "not_found" if (api_not_found or not is_local) else "ok"
            elif api_ok:
                # resolved, genuinely no dependencies
                children, via, state = [], "api", "ok"
            elif is_local or disk_present:
                # on disk, empty/absent addon.gproj, API unusable
                children, via, state = [], "gproj", (
                    "not_found" if (api_not_found and not disk_present) else "ok"
                )
            else:
                # neither a local dir nor an API record
                children, via, state = [], "unknown", "unresolved"

        tree.nodes.append(
            ResolvedNode(guid=guid, name=name, via=via, state=state, depth=depth)
        )

        for child_guid, child_name in children:
            if not child_guid or child_guid == "NONE":
                continue
            key = (guid, child_guid)
            if key not in edge_seen:
                edge_seen.add(key)
                tree.edges.append(key)
            if child_guid not in visited:
                queue.append((child_guid, depth + 1))
            # Backfill a name onto an already-seen child node if we have one now.
            if child_name:
                for n in tree.nodes:
                    if n.guid == child_guid and not n.name:
                        n.name = child_name

    return tree
