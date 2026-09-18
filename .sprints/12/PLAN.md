# Reforger Manager — Sprint 12 plan

Rework the mods UI around explicit user intent vs. resolved dependencies, and
fix the dependency-lookup performance problem underneath it.

## Context

The current dependency UI (`useModCoverage`, `frontend/src/lib/modCoverage.ts`)
issues one `GET /api/mods/{guid}` per explicitly-picked mod, each of which calls
`resolve_dependencies()` (`backend/app/mods/resolve.py`), a BFS that can hit the
live Workshop API per node. On a server with dozens of picks this is dozens of
requests, each potentially doing live API BFS, just to know which picks cover
which other picks. It also only really reasons about coverage (a flat "is X
reachable from Y" set), not a genuine multi-level nested hierarchy.

Separately, "explicit selection" already exists as a persisted concept —
`ServerMod` and `ModpackItem` rows are exactly the user's explicit picks;
dependencies are never persisted as their own rows, only resolved on demand.
So the nested/flat split this sprint asks for is a new *read path and UI*, not
a new data model.

## Decisions locked

| # | Decision |
|---|---|
| 1 | No schema change. `ServerMod` / `ModpackItem` rows are the explicit set; dependency edges already live in `mod_dependencies`. Nested vs. flat is a client-side projection over graph data, not a new column or table. |
| 2 | New `GET /api/mods/graph` returns the whole library's nodes + edges from Postgres in one query — no Workshop calls, no BFS. Node payload is compact: `guid`, `name`, `is_local`, `api_state`, `is_unlisted/private/obsolete`, and a freshness timestamp (`api_checked_at`). Anything else (thumbnail, tags, versions, size) stays on the existing per-mod `GET /api/mods/{guid}`. |
| 3 | All tree-shaping (nested-from-roots, flat-dedup, reverse/"required by") is a client-side graph walk over the one edge list, reused across Mod Library, Server Mods, Modpack Editor, and Mod Detail. The backend stops producing view-shaped trees per caller. |
| 4 | Workshop reconciliation stays exactly what it is today — the existing `mod_sync` job — nothing new to build there. The UI just needs to (a) show a freshness pill sourced from node timestamps and (b) not block its initial render on it. |
| 5 | Reparenting on reconciliation gets a brief highlight/fade on the affected node(s), not a measured FLIP animation. No animation library exists in the frontend today; this is the smallest thing that still tells the user something moved. |
| 6 | `resolve_dependencies()` / live Workshop BFS is untouched and keeps its existing callers (pre-flight, config generation, mod_sync). This sprint only removes it from the *UI's* hot path. |

## Backend

### `GET /api/mods/graph` (`backend/app/api/mods.py`)

One query each against `mods` and `mod_dependencies`, joined in Python, no
per-node Workshop calls:

```
{
  "nodes": [{"guid", "name", "is_local", "api_state", "is_unlisted",
             "is_private", "is_obsolete", "api_checked_at"}],
  "edges": [{"from", "to"}]   # from = mod_guid, to = depends_on_guid
}
```

Include `ENGINE_BUILTIN_GUIDS` (`resolve.py`) nodes if they appear as edge
targets (so a client walking edges never hits a dangling guid), but flag them
so the frontend can hide them the way pre-flight already does — reuse the
constant, don't re-derive it.

No pagination, no filters server-side; the whole thing is small enough (a few
hundred rows) to project and filter client-side, and that keeps every view
(library flat, library nested, "required by", per-server added-set) working
off the same cached fetch. Cache with a generous `staleTime` in react-query and
invalidate on mod_sync job completion.

### Mod Detail (`backend/app/api/mods.py`, `get_mod_detail`)

No change to `resolve_dependencies` usage for the authoritative single-mod
tree, but the route already builds `dependency_tree` — leave it, the frontend
Mod Detail page will prefer the shared `/graph` walk for consistency with the
rest of the UI and keep this field only as a fallback / for depth-limited
sanity in isolation. Add a reverse-edge query already partially present
(`required_by` in `_to_out` / `get_mod_detail`) — confirm it returns the full
list, not just direct parents, since "is required by" now needs the full
recursive reverse hierarchy; if it's direct-only today, that's computed
client-side from `/graph` instead, so no backend change needed here either.

## Frontend

### `frontend/src/lib/modGraph.ts` (new, replaces `modCoverage.ts`)

Single hook `useModGraph()` fetching `/api/mods/graph` once (shared cache key
across all consumers). Pure functions on top, no React:

- `buildNested(roots: string[], edges): TreeNode[]` — recursive, duplicates a
  node under every parent that reaches it (including as a second root if it's
  also explicitly selected elsewhere in the same root set). This is the one
  piece of real logic in the sprint: a node can legitimately appear more than
  once in the same render.
- `buildFlat(roots: string[], edges): Set<guid>` — full reachable closure,
  deduplicated.
- `buildReverse(root: string, edges)` — same two shapes, walking edges
  backward, for Mod Detail's "required by".
- `freshnessOf(guids: string[], nodes)` — oldest `api_checked_at` among the
  given nodes, for the freshness pill.

Delete `modCoverage.ts` once `ModsPanel.tsx` and `Modpacks.tsx` no longer
import it.

### Shared components (new, under `frontend/src/components/mods/`)

- `ModTree.tsx` — renders a `TreeNode[]` (nested) or `guid[]` (flat) with a
  Nested/Flat toggle, given a `roots` list and click/remove/select callbacks.
  Reused by Mod Library, the "Added" pane of Server Mods, the "Added" pane of
  Modpack Editor, and both directions of Mod Detail's dependency panels.
- `ModLibraryPicker.tsx` — the bottom-pane library browser (Nested/Flat +
  filters) used identically by Server Mods and Modpack Editor, taking a
  `disabledGuids: Set<string>` (already-present, explicit or indirect) and an
  `onAdd(guid)` callback.
- `FreshnessPill.tsx` — small badge, timestamp → "synced Xm ago" / "syncing…"
  while a `mod_sync` job is active; brief highlight class applied to a guid
  when its parent-set changes between two graph fetches (compare previous
  `buildNested` output to the new one, tag the diff).

### Mod Library (`frontend/src/pages/Mods.tsx`)

- Nested: roots = mods with `is_local` (the user's downloaded/picked set,
  per spec — "explicitly downloaded/picked into the library"); render via
  `ModTree`.
- Flat: existing table view, unchanged in spirit, but dedup is now graph-based
  rather than the current flat list.
- Filters (Local only / Has update / Orphans / Not resolved) apply to the flat
  view as today; for nested, filtering hides non-matching leaves but keeps
  ancestors needed to reach a matching descendant.

### Server Mods (`frontend/src/components/server/ModsPanel.tsx`)

- Top ("Added"): roots = `ServerMod` rows (explicit). `ModTree` with
  Nested/Flat toggle. Remove acts only on the explicit `ServerMod` row (removing
  a dependency-only node isn't offered — it isn't a row to remove).
- Bottom: `ModLibraryPicker`, `disabledGuids` = explicit `ServerMod` guids ∪
  their resolved closure. Adding calls the existing add-mod endpoint, creating
  a new explicit `ServerMod` row.
- This replaces the current `AddModRow` / `AssignedModExpanded` coverage-based
  hide logic entirely.

### Modpack Editor (`frontend/src/pages/Modpacks.tsx`)

Same shape as Server Mods, roots = `ModpackItem` rows. Share `ModTree` +
`ModLibraryPicker` rather than duplicating Server Mods' JSX — this is the
sprint's main de-duplication opportunity given the two pages are ~1200 and
~800 lines today with near-identical picker logic.

### Mod Detail (`frontend/src/pages/ModDetail.tsx`)

- "This mod requires": `ModTree` over `buildNested([guid], edges)` /
  `buildFlat`.
- "This mod is required by": `ModTree` over `buildReverse(guid, edges)`,
  same Nested/Flat toggle.
- Usage: existing `used_by`/`ModReferences`-shaped server/modpack lists, each
  entry tagged direct vs. indirect by checking membership in that
  server's/modpack's explicit root set vs. its closure (both already fetchable
  from `/graph` plus that server's/modpack's existing mod list response — no
  new endpoint).

## Testing

Backend (`backend/tests/`):

1. `GET /api/mods/graph` returns every `mods` row and every `mod_dependencies`
   edge, with no Workshop client invoked (mock/spy asserts zero calls).
2. Graph response omits size/tags/thumbnail/versions fields.
3. `ENGINE_BUILTIN_GUIDS` present as edge targets appear as nodes (not
   dangling), flagged.

Frontend: `npm run build` only, per project convention. Manually exercise in
the browser per CLAUDE.md's UI-change rule:

1. Mod Library nested shows only locally-picked mods as roots, deps nested
   beneath; flat shows the deduplicated full set.
2. A mod that is both an explicit server pick and a dependency of another pick
   appears twice in Server Mods nested (once as its own root, once nested).
3. Server Mods bottom pane greys out mods already covered (direct or
   indirect); adding one creates a new explicit row and updates the top pane.
4. Modpack Editor mirrors the above.
5. Mod Detail's requires/required-by trees match `/graph` edges by hand-check
   on one mod with known multi-level deps (e.g. an RHS or WCS mod).
6. Trigger a `mod_sync`; confirm the freshness pill updates and a node whose
   parent set changed gets the highlight, without a full page reload.

## Cut from this sprint / Non-goals

- A generic DAG visualization — explicitly rejected in favor of nested/flat
  projections.
- A real FLIP animation library/implementation — highlight-on-change instead
  (Decision 5).
- Any change to `resolve_dependencies`, pre-flight, or config generation —
  those keep using live/API-aware resolution as today; this sprint only
  changes what the *UI* reads.
- A persisted "explicit" flag or dependency-row table — `ServerMod` /
  `ModpackItem` already are the explicit set (Decision 1).

## Verification

```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm run build
docker compose up -d --build   # then the manual matrix above at :18090
```
