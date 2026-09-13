# Reforger Manager — Sprint 9 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-13 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All three stories implemented and verified. The Modpacks editor now stores
**explicit picks only** and renders the dependency closure the same way the
server Mods tab does (covered picks hidden under their parent with a `pick`
badge + Remove, pure deps as read-only links). `useModCoverage` is a shared
module consumed by both pages. No backend file, no `SortableModList.tsx`, no
`lib/api.ts` change; no migration; no commit/push (working tree left
uncommitted per sprint rules).

## Story-by-story

| # | Story | State | Agent | Notes |
|---|---|---|---|---|
| S1 | Extract coverage pipeline into `lib/modCoverage.ts` | done | opencode (general-flash subagent) | `frontend/src/lib/modCoverage.ts` created exporting `DepNode`, `ModDeps`, `useModCoverage<T extends { mod_guid: string }>`; ModsPanel's `depResults`/`depSig`/`depEdges`/`coverageByParent`/`covered` moved verbatim (same `["mod", guid, "deps"]` key, `staleTime: 60_000`, `depth > 0 && state !== "unresolved"` filter, `seen`-set stack walk, eslint pragma carried). ModsPanel lost 73 lines, consumes `useModCoverage(working)`, imports `ModDeps` (not `DepNode` — no local use remains, unused import fails `tsc -b`). Two justified deviations: explicit `useMemo<Map<string, ReadonlySet<string>>>` annotations (Map invariance) and keeping `EMPTY_GUIDS` in ModsPanel. |
| S2 | Modpacks editor: explicit picks only + dependency-aware view | done | opencode (general-flash subagent) | `frontend/src/pages/Modpacks.tsx` only (227 lines changed): `addItem` is now a synchronous explicit-pick add (Set dedup, no `fetchQuery`, eager dep insertion deleted); `useModCoverage(items)` + `topLevel` filter + `reorderVisible` splice-back + `expandedRows`/`toggleExpanded`; `SortableModList` gets `items={topLevel}` with count header staying `items.length` (stored count); ▸/▾ disclosure in `renderActions` (needed `type="button"` — rows sit inside the editor `<form>`); new `PackItemExpanded` (modeled on `AssignedModExpanded` minus enable/pin): covered picks first (`pick` badge + Remove via `onRemove`), then pure dep nodes from a shared-cache `["mod", parent.mod_guid, "deps"]` query (depth sort, `via`, unresolved → guid + badge, no link), loading/`...`/error/`No dependencies.` branches. `save` untouched. |
| S3 | Integration + RESULTS | done | orchestrator | Both gates re-run independently; stack rebuilt (`docker compose up -d --build`); manual matrix walked in the browser against the rebuilt image (below). No commit/push. |

## Data-model changes

None. (Per PLAN decision 3 — existing packs are not migrated; stored dep rows
classify as covered picks at view time.)

## New / changed API + frontend surface

- New module: `frontend/src/lib/modCoverage.ts` (`useModCoverage` hook +
  `DepNode`/`ModDeps` types).
- Changed page: `/modpacks` editor (add path, top-level list, disclosure,
  expanded subtrees, splice-back reorder). "Packs" list, apply/export/import,
  picker section and dialogs untouched.
- No API, schema or backend change at all. No new frontend deps.

## Verification performed

- `cd frontend && npm run build` — clean after each story and after S3
  (`tsc -b && vite build`, only the pre-existing chunk-size advisory).
- `cd backend && ./.venv/Scripts/python.exe -m pytest -q` — **327 passed**,
  92 pre-existing warnings (no backend file touched; regression gate only).
- `git grep -n "DepNode" -- frontend/src` — canonical type only in
  `lib/modCoverage.ts`; the remaining matches are `pages/ModDetail.tsx`'s
  pre-existing local copy (untouched, out of scope).
- Stack rebuilt with the new SPA baked in; `/api/health` ok.

### Manual matrix (PLAN "Testing", cases 1–7)

Run 2026-09-13 on the local dev stack (`http://localhost:18090`), driven
through the browser with the fixture trio **CO-OP Conflict PVE**
(`62E960A6A1BA0985`, deps: ConflictPVERemixedVanilla2.0, Ronin AI, one
unresolved node `58D0FB3206B6F859`) / **ConflictPVERemixedVanilla2.0**
(`61B514B96692C049`) / **Where Am I** (`5965550F24A0C152`):

1. **Add with deps → only the pick stored** — created pack "S9 Matrix", added
   CO-OP: PACK CONTENTS (1), one top-level row, ▸ expands to read-only dep
   links (ConflictPVERemixed, Ronin AI with `via api`) + the unresolved guid
   with badge and no link. Save + re-open: exactly 1 stored item. ✔
2. **Covered pick** — added ConflictPVERemixed (also a CO-OP dep): hidden from
   top level, appears under CO-OP's expansion with `pick` badge + Remove;
   stored count (2) includes it. ✔
3. **Remove parent → covered pick survives** — removed CO-OP:
   ConflictPVERemixed resurfaced at the top level, count (1); never-stored
   deps left nothing behind. ✔
4. **Drag-reorder with covered slots** — re-added CO-OP + Where Am I
   (count 3, top level [CO-OP, Where Am I]); keyboard DnD (dnd-kit
   KeyboardSensor: Space/ArrowUp/Space) moved Where Am I to #1; after save +
   re-open: stored `load_order` = ConflictPVERemixed 0, Where Am I 1, CO-OP 2 —
   covered row kept its slot, top-level order persisted. ✔
5. **Old (pre-change) pack** — seeded "S9 Legacy" via API with parent + dep as
   stored rows (what eager insertion used to produce): dep row hides under the
   parent as a `pick`; removing the parent surfaces it. Pack left intact
   (editor cancelled, not saved), then both test packs deleted. ✔
6. **Apply → closure resolved at start-time** (adapted: engine start skipped
   on dev per operator instruction) — applied "S9 Matrix" (3 stored picks) to
   stopped server `test`; `GET /api/servers/2/config` shows `game.mods` with
   **4** entries: the 3 stored picks + **Ronin AI — the dep never stored in
   the pack** — ordered deps-first; the unresolved node is dropped. This is
   `config_gen.resolved_mod_entries` doing exactly what PLAN decision 2
   asserted, verified empirically. ✔
7. **Server Mods tab regression** (S1 refactor) — assigned set of 3 on server
   `test`: ASSIGNED MODS (3), top level [Where Am I, CO-OP], covered pick
   hidden; ▸ disclosure shows covered pick with `pick` badge + full controls
   (Disable, Pin version) + pure dep links + unresolved badge — byte-identical
   behaviour to pre-refactor. ✔

Test artifacts cleaned up afterwards: packs "S9 Matrix" and "S9 Legacy"
deleted, server `test` restored to its original single-mod assignment.

## Not exercised

- **Starting the engine / a live server on dev** — per operator instruction
  the dev stack must not run the actual game engine; case 6 was verified via
  the generated `config.json` (`GET /api/servers/{id}/config`) instead of a
  live start. The deployed server (BADIS | Conflict HQ PvPvE, running) was
  never touched.
- **Pointer-drag DnD** — exercised via the KeyboardSensor (Space/Arrow/Space)
  instead of synthetic pointer events; same `onDragEnd` code path in
  `SortableModList`.
- Multi-parent coverage (one covered pick under two parents' subtrees
  simultaneously) — no fixture with two parents sharing a closure was present;
  logic is shared verbatim with the Mods tab where it has been in production
  since Sprint 7.
- Modpack import/export round-trip of the new-style pack — unchanged code
  paths (pack is an item list either way); covered by existing backend tests.

## Minor observations

- The S1 subagent left a stale doc comment in `AssignedModExpanded`
  ("the dedup `useQueries` above" — now lives in `lib/modCoverage.ts`);
  cosmetic, deliberately not touched to keep the diff minimal.
- `pages/ModDetail.tsx` still carries its own local `DepNode`/`ModDeps` copy
  (pre-existing, out of scope this sprint) — a future tidy-up could import
  from `lib/modCoverage.ts`.
- Drag-during-edit on the Modpacks editor form: the disclosure button needed
  `type="button"` to avoid submitting the editor form — noted in S2's
  implementation; ModsPanel's copy doesn't need it (different DOM nesting).

## How this sprint was implemented

Orchestrator (opencode · z-ai/glm-5.3) ran Phase D/E per the sprint skill:
briefs distilled from PLAN/STORIES into self-contained prompts (no
`.sprints/` reads by story agents), one subagent per story, strictly
sequential (S2 imports S1's module; STORIES.md marks all three sequential — no
parallel launches). Both code stories went to `general-flash` subagents as
directed (the skill's native-Claude/deepseek/glm rotation was overridden by
the operator's instruction). No stalls, no restarts, no talk-themselves-out
writes — both agents wrote code on first pass; each gate was re-verified by
the orchestrator independently (build + pytest) before advancing. S3 was
orchestrator-run: Docker daemon started on demand (Docker Desktop), stack
rebuilt, matrix driven via browser DOM inspection (this model cannot read
screenshots — text/DOM assertions used instead, which is also more precise for
this purpose), API cross-checks via `curl`/`Invoke-RestMethod` with a JWT
login. One environment hiccup: 12 stale camoufox processes from an unrelated
session (bbfs-thailand profile) held the browser profile; killed with explicit
user approval before the matrix could run.
