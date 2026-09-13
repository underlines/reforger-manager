# Reforger Manager — Sprint 9 plan

## Context

On `/modpacks`, the editor's **Pack contents (n)** list renders every stored
pack item as an equal top-level row. There is no visual distinction between a
mod the user added and a mod that landed in the pack only because it is a
dependency of a user-added mod — and because `addItem` eagerly inserts the
whole dependency closure as pack rows (2026-09-13 behavior,
`pages/Modpacks.tsx:139-168`), most packs are full of such rows.

The server **Mods** tab already solved this (Sprint 7,
`components/server/ModsPanel.tsx:257-362`):

- user-added mods are top-level (draggable) rows;
- each row has a ▸/▾ disclosure whose expanded subtree shows the resolved
  dependency closure — read-only links for pure deps, full controls for
  *other explicit picks the parent covers*;
- a pick that is both user-added **and** inside another pick's dependency
  closure ("covered") is hidden from the top level but kept in storage; if
  the covering parent is removed, the covered pick resurfaces at the top
  level — hidden, never deleted.

This sprint replicates that exact model on `/modpacks`.

## Guiding principle

A modpack stores the same thing a server's assigned set stores: **explicit
picks only**. Dependencies are derived data — resolved for display from the
`["mod", guid, "deps"]` cache and for loading by `config_gen` at server
start. Storage gains no provenance column; "user-added vs dependency" is a
view-time computation over the dependency graph, identical to ModsPanel's.

## Decisions locked

| # | Decision |
|---|---|
| 1 | **Kill the eager dep insertion.** `addItem` on `/modpacks` adds only the explicit pick. This is forced by the spec: without it, stored dep rows are indistinguishable from user picks, so "deps go away with their parent" and "user picks survive their parent's deletion" cannot both hold. It also unifies the two pack-creation paths — `create_modpack_from_server` already snapshots explicit-only sets. |
| 2 | **No backend change.** A pack holding only explicit picks still loads correctly: `config_gen.resolved_mod_entries` (`backend/app/servers/config_gen.py:148`) expands the dependency closure (deps-first, de-duplicated) into `config.json` at every server start — verified by `backend/tests/test_config_deps.py`. `apply_modpack`, export/import, backup and MCP are item-list agnostic. |
| 3 | **Existing packs are not migrated.** Old packs that contain eagerly-inserted dep rows keep them (they are stored items); under the new view those rows classify as *covered picks* — hidden under their covering parent, with a Remove button. Removing the parent surfaces them. Harmless, no migration, no provenance back-fill. |
| 4 | **Extract the coverage computation into a shared module** (`frontend/src/lib/modCoverage.ts`) and have ModsPanel consume it too — generalize the ModsPanel pattern, don't re-derive a third copy. ModsPanel's refactor is mechanical: its inline `depResults`/`depSig`/`depEdges`/`coverageByParent`/`covered` memos move behind the shared hook; `topLevel`/`reorderVisible`/`AssignedModExpanded` stay where they are (shape-specific). |
| 5 | Pack-contents count in the editor header keeps showing the **stored** item count (`items.length`) — covered picks are still in the pack. The main "Packs" list's per-pack mod-count badge is likewise unchanged. |
| 6 | The expanded subtree in the pack editor shows: covered picks (with a `pick` badge + their own Remove button) first, then pure dep nodes as read-only `Link`s (with `via`, `unresolved` badge when applicable) — mirroring `AssignedModExpanded`, minus the enable/pin controls (packs have neither). |
| 7 | No commit/push. No `.sprints/` edits by story agents (S3 writes `RESULTS.md`). No migration. No backend files touched. |

## Code-level facts the implementation must respect

1. `components/server/ModsPanel.tsx` — the pattern to generalize:
   - `:268-274` `depResults` — `useQueries` keyed `["mod", guid, "deps"]`
     (`staleTime: 60_000`), one query per explicit pick.
   - `:278-304` `depSig` + `depEdges` — guid → set of **directly declared,
     resolved** deps (`depth > 0 && state !== "unresolved"`), `undefined`
     while loading → "covers nothing".
   - `:308-327` `coverageByParent` — parent guid → every OTHER explicit pick
     reachable transitively through `depEdges`; stack walk with a `seen` set so
     A→B→A metadata cycles terminate; the parent itself is excluded.
   - `:329-341` `covered` (union) and `topLevel` (uncovered rows only).
   - `:345-352` `reorderVisible` — a drag reorders the top-level list; splice
     that order back over the covered rows' existing slots in the full array.
   - `:355-362` `expandedRows` disclosure `Set` + toggle.
   - `:1174-1298` `AssignedModExpanded` — expanded subtree: covered picks
     (`pick` badge, full controls) sorted first, then pure dep nodes
     (`depth > 0 && !explicitGuids.has(guid)`), sorted by depth then name;
     unresolved nodes render guid + `unresolved` badge, no link.
2. `components/mods/SortableModList.tsx:46` — `renderExpanded` prop already
   exists (added for ModsPanel); `SortableRow` = `{ key, guid, name }`.
   **No changes to this file.**
3. `pages/Modpacks.tsx` — the target:
   - `:16` `EditorItem = { key, guid, name, mod_guid }` — satisfies
     `SortableRow` directly; **no `enabled`/pin fields** (decisions 6).
   - `:18-19` local `DepNode`/`ModDeps` types — replace with the shared
     module's exports.
   - `:52-61` `itemsFromPack` sorts by `load_order` then guid.
   - `:139-168` `addItem` — the eager insertion to remove. Its
     `queryClient.fetchQuery(["mod", mod.guid, "deps"])` already uses the
     shared cache key; the new add path needs no fetch at all.
   - `:169-170` `removeItem` — stays, now also reachable from expanded rows.
   - `:366-389` the "Pack contents" `<section>`: `SortableModList` +
     `renderActions` Remove button — gains `renderExpanded` and the
     disclosure toggle.
   - `:172-192` `save` maps `items` order → `load_order` index; with
     splice-back reorder the covered rows keep their slots. No change needed.
4. `lib/api.ts:54-58` — `ModpackItem = { mod_guid, load_order, mod_name }`;
   the saved pack round-trips through `POST/PATCH /api/modpacks` unchanged.
5. `pages/ModDetail.tsx:331-343` and `ModsPanel`'s `AddModRow` — dep-node
   `Link` styling convention (`hover:text-amber-400`,
   `stopPropagation` on click inside draggable rows).
6. Backend closure proof: `backend/app/servers/config_gen.py:148-161`
   (`resolved_mod_entries` — closure, deps-first, de-duped, unloadable deps
   dropped) and `backend/tests/test_config_deps.py:61`
   (`test_transitive_closure_is_ordered_deps_first`).
7. Query-cache key discipline: everything deps-related is keyed
   `["mod", guid, "deps"]` so ModsPanel, AddModRow, the new shared hook and
   any `fetchQuery` share one cache entry per guid.

## Feature set

### F1 — shared `useModCoverage` module
`frontend/src/lib/modCoverage.ts` (new): exports `DepNode`, `ModDeps` (moved
from the two pages' local copies) and
`useModCoverage<T extends { mod_guid: string }>(items: T[])` returning
`{ coverageByParent: Map<string, ReadonlySet<string>>, covered: ReadonlySet<string> }`
internally built from `useQueries` + the `depEdges`/`coverageByParent`/`covered`
pipeline above. ModsPanel switches to it; behavior byte-identical.

### F2 — Modpacks editor parity
`pages/Modpacks.tsx`: `addItem` adds only the explicit pick (dedup against
current items); wire `useModCoverage`; render `topLevel` (uncovered items)
through `SortableModList` with a ▸/▾ disclosure in `renderActions` and a new
`renderExpanded` subtree (covered picks with Remove, then pure dep links);
`reorderVisible`-style splice-back for drags; delete the eager-insertion code
and the now-unused `DepNode`/`ModDeps`/`fetchQuery` path.

## Data-model changes

None. (Deliberately — see decision 3.)

## Testing

Frontend: no JS harness — `cd frontend && npm run build` (`tsc -b && vite
build`) is the only gate, per story. Backend: `cd backend &&
./.venv/Scripts/python.exe -m pytest -q` must stay green as a regression gate
(no backend edits are expected — a red suite means a story touched something
it shouldn't have).

Manual matrix (S3, on a running stack, `/modpacks` editor):

1. Add mod A with deps {X, Y} → list shows only A; A's ▸ expands to X, Y as
   read-only links; the pack's stored count stays 1; save + re-open confirms
   only A is stored.
2. Add mod B (also a dep of A, or A's dep chain) → B hidden from top level,
   appears under A with a `pick` badge + Remove; stored count includes B.
3. Remove A → B resurfaces at the top level, still in the pack (preserved);
   X/Y were never stored, so they vanish with nothing left behind.
4. Drag-reorder top-level rows → save → reload: covered rows keep their
   relative slots, top-level order persisted.
5. Old pack created pre-change (deps stored eagerly): dep rows hide under
   their parent as `pick` rows; removing the parent surfaces them.
6. Apply the pack to a stopped server → start it → the RCON console/config
   shows the full closure loaded (deps resolved at start, not stored).
7. Server Mods tab still behaves exactly as before (S1 regression).

## Cut from this sprint / Non-goals

- No backend routes, schemas or migrations (decision 2).
- No dep-preview ("Deps") disclosure on the library picker rows in the
  Modpacks editor — the server tab's `AddModRow` has one; parity there is a
  nice-to-have, not asked for.
- No change to `SortableModList`, pack apply semantics, export/import, the
  "Packs" summary list, or the delete-pack flow.
- No provenance tracking / back-fill for existing packs (decision 3).
- No migration of `items.length` counts to "top-level picks" counts.

## Verification

```bash
cd frontend && npm run build                              # tsc -b && vite build — clean
cd backend && ./.venv/Scripts/python.exe -m pytest -q     # green (regression only)
```

Then S3 runs the manual matrix above on a rebuilt stack
(`docker compose up -d --build`, SPA baked into the image).