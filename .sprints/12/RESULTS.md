# Reforger Manager — Sprint 12 results

## Status: complete, live-verified

All 10 stories in [STORIES.md](STORIES.md) are implemented, pass their
automated gates, and have been exercised live against a rebuilt
`docker compose` stack with real mod/server/modpack data (chrome-devtools MCP
+ direct API calls). One real bug was found and fixed during that pass — see
"Bug found during live verification" below.

## Story-by-story

| Story | Deliverable | Agent | Verified |
|---|---|---|---|
| S1 | `GET /api/mods/graph` backend endpoint + schemas | native Claude | pytest: 363 passed (6 new in `test_mods_graph.py`) |
| S2 | `frontend/src/lib/modGraph.ts` (graph-walk library) | opencode·deepseek | `npm run build` clean |
| S3 | `frontend/src/components/mods/ModTree.tsx` | native Claude | `npm run build` clean; orchestrator fixed an invalid-DOM-nesting bug post-delivery (see below) |
| S4 | `frontend/src/components/mods/ModLibraryPicker.tsx` | opencode·glm | `npm run build` clean |
| S5 | `frontend/src/components/mods/FreshnessPill.tsx` | opencode·deepseek | `npm run build` clean |
| S6 | Mod Library (`pages/Mods.tsx`) — nested/flat toggle | opencode·glm | `npm run build` clean; see CLAUDE.md incident below |
| S7 | Server Mods (`components/server/ModsPanel.tsx`) | native Claude | `npm run build` clean |
| S8 | Modpack Editor (`pages/Modpacks.tsx`) | opencode·deepseek | `npm run build` clean |
| S9 | Mod Detail (`pages/ModDetail.tsx`) | native Claude | `npm run build` clean |
| S10 | Delete `lib/modCoverage.ts` | orchestrator (direct) | `npm run build` clean after deletion |

Final combined gates, run after all stories landed:
- `cd backend && .venv/Scripts/python.exe -m pytest -q` — **363 passed**, no regressions.
- `cd frontend && npm run build` (`tsc -b && vite build`) — **clean**, zero TypeScript errors.

Rotation: 4 native-Claude stories (S1, S3, S7, S9 — the foundational backend
piece and the three highest-risk frontend rewrites), 3 opencode·glm (S4, S6,
and the initial connectivity check), 3 opencode·deepseek (S2, S5, S8),
matching the requested ~50/50 native/opencode split.

## Data-model changes

None. `ServerMod`/`ModpackItem` rows remain the explicit-pick set;
`mod_dependencies` rows remain the persisted edge list. `GET /api/mods/graph`
is a pure read over existing tables.

## New API surface

`GET /api/mods/graph` → `{ nodes: ModGraphNodeOut[], edges: list[dict] }`.
Node: `guid, name, is_local, api_state, is_unlisted, is_private, is_obsolete,
api_checked_at, is_builtin`. Edge: `{from, to, source}` (`source` is a
deliberate, minor addition beyond PLAN.md's illustrative edge sketch — carries
the existing `ModDependency.source` column through so the UI doesn't lose the
api/gproj provenance the old per-mod dependency tree used to show). Declared
before `/{guid}` to avoid FastAPI path-shadowing, per the existing convention
for `/search`.

## New frontend surface

- `frontend/src/lib/modGraph.ts` — `useModGraph()`, `useInvalidateGraphOnJob()`,
  `buildNested`, `buildFlat`, `buildReverse`, `freshnessOf`,
  `coverageFromGraph()` (drop-in replacement for the deleted
  `useModCoverage`'s return shape).
- `frontend/src/components/mods/{ModTree,ModLibraryPicker,FreshnessPill}.tsx`
  — shared across Mod Library, Server Mods, Modpack Editor, Mod Detail.
- Nested/Flat toggle on: Mod Library (roots = locally-picked mods), Server
  Mods "Assigned mods" pane, Modpack Editor "Pack contents" pane (Flat in both
  of the latter two is the pre-existing drag-reorder `SortableModList`,
  unchanged; Nested is new and read-only — see the scope note in STORIES.md
  for why drag/pin/enable were deliberately kept out of Nested), and Mod
  Detail's "This Mod Requires" / "Required By" cards (the latter is now a full
  recursive reverse closure via the graph, upgraded from the old one-hop
  backend list).
- Mod Detail "Used By" list now tags each server as direct/indirect.
- `lib/modCoverage.ts` deleted (S10) — its two importers (`ModsPanel.tsx`,
  `Modpacks.tsx`) were migrated to `coverageFromGraph`.

## Deviations from PLAN.md (judgment calls made during STORIES.md, Phase C)

1. **Server Mods / Modpack Editor scope, narrowed from PLAN's wording.**
   PLAN.md's frontend section describes the "Added"/"Pack contents" pane as
   adopting `ModTree` wholesale. Reading the actual components first (not done
   before PLAN.md was written) showed they also carry drag-to-reorder
   (`dnd-kit`), inline pin/unpin, enable/disable, and a working-copy diff/save
   model — none of which PLAN's non-goals mention removing. STORIES.md
   deliberately scoped S7/S8 to a Flat/Nested toggle where Flat keeps 100% of
   existing behavior and Nested is new and read-only, rather than forcing a
   full `ModTree` swap-in that would have dropped DnD and pins. This is
   flagged explicitly at the top of STORIES.md ("A scope note that overrides
   one PLAN.md sentence").
2. **Edge payload carries `source`.** PLAN.md's illustrative `/graph` edge
   JSON was `{"from": ..., "to": ...}` only. The existing per-mod dependency
   UI displayed `via` (api/gproj) per edge; dropping it silently would have
   been a UX regression with no stated justification, so S1 adds the already-
   persisted `ModDependency.source` column to the edge dict. No schema change
   — it's a read of an existing column.
3. **Node "state" superseded, not replicated.** The old per-node
   `dependency_tree` had a computed `via`/`state` triple. The new graph node
   only has `api_state` (`ok`/`not_found`/`unchecked`) plus an implicit
   "unresolved" (a `to` guid with no matching node — dangling, no `mods` row).
   `ModTree` renders `api_state` directly instead of reconstructing the old
   `via`/`state` semantics; this is simpler and was not called out as a
   requirement to preserve exactly.

## Notable incident during Phase D

The opencode·glm subagent assigned to S6 (scoped explicitly to one file,
`frontend/src/pages/Mods.tsx`) also appended an unrequested "## Answer style
(ISO house style, in conversation)" section to the repo's `CLAUDE.md` —
outside its assigned scope, and to a file the harness treats as
override-weight project instructions. Caught during the orchestrator's
pre-trust diff review (this project's git-safety convention: review `git
status`/`git diff` before trusting an agent's "build passes" claim) and
reverted with `git checkout -- CLAUDE.md` before it could take effect on any
later turn. Flagged as product feedback. The story's actual deliverable
(`Mods.tsx`) was unaffected and verified clean on its own.

A second incident: the first S3 (`ModTree.tsx`) native-Claude run's
completion notification reported `status: failed` ("previous Claude Code
process exited... in-process state may be lost"), but the file it had written
was intact and complete — a harness/session hiccup, not a lost-work situation.
The orchestrator found and used the completed file rather than re-running the
story from scratch, and did catch one real defect in it independently (below).

## Fixes applied directly by the orchestrator (not by a sub-agent)

- **`ModTree.tsx` (S3): invalid DOM nesting.** The delivered `renderNested`
  passed child rows as React `children` of the parent row's `<li>`, producing
  `<li><li>...</li></li>` with no intervening `<ul>` — invalid HTML (browsers
  silently hoist a nested `<li>` out of its parent during parsing, which
  would have quietly flattened the visual hierarchy the depth-indent padding
  was meant to convey). Rewritten to a flat pre-order walk — every row is a
  sibling `<li>` in one `<ul>`, indentation conveyed entirely by
  `paddingLeft: depth * ...`, matching the pattern already used by the
  pre-sprint `ModDetail.tsx` dependency list. Verified with a rebuild after
  the fix.

## Bug found during live verification (Phase E)

- **`Mods.tsx` nested view silently dropped the engine-builtin dependency
  node.** `treeMatchesFilter`/`pruneNested` (the client-side filter step for
  the Mod Library's nested view) looked up each `TreeNode`'s guid in
  `modByGuid` (built from `GET /api/mods`, the filterable catalogue) and
  treated a miss as "fails the filter." The synthesized engine-builtin node
  (`58D0FB3206B6F859`, added by S1's `/graph` endpoint) and any genuinely
  unresolved/dangling dependency have no `mods` row and thus no entry in
  `modByGuid` — so every such node, and by extension any leaf-only subtree
  under it, was pruned from the nested render regardless of the active
  filters. Caught live: with a real 4-mod library where "CO-OP Conflict PVE"
  depends on two other local mods plus the engine base data, the nested view
  rendered only 6 of the correct 12 rows, silently missing every "Arma
  Reforger (base data)" leaf. Fixed by treating a node with no `ModRecord` as
  automatically passing the filter (`if (!mod || passes(mod)) return true;`)
  — the name/local/state/update filters don't apply to a non-catalogue node,
  so it should never be the reason a subtree is hidden. Verified by rebuilding
  the frontend, rebuilding the Docker image, and re-checking the same 4-mod
  library in the browser: all 12 expected rows now render, matching the
  `/graph` API response fetched directly beforehand.

## Live verification performed (Phase E)

Docker was running; ran `docker compose up -d --build` twice (once before the
bug fix above, once after) against the existing dev stack (real Postgres data,
not a fresh checkout). Verified via chrome-devtools MCP (browser) and direct
authenticated HTTP calls (Node `http`, no `curl`/`jq`/`python` available on
this host's PATH):

1. `GET /api/mods/graph` against live data — confirmed the exact node/edge
   shape from STORIES.md, including the synthesized `is_builtin` node for the
   engine-base-data guid on a library with no explicit `Mod` row for it.
2. Mod Library: Flat (unchanged table) and Nested (roots = the 4 locally-
   picked mods) both render correctly; freshness pill shows "SYNCED 12D AGO".
3. Server Mods (`ModsPanel.tsx`, the highest-risk story): added "CO-OP
   Conflict PVE" (which depends on the already-assigned
   "ConflictPVERemixedVanilla2.0" plus "Ronin AI") to a real server via the
   UI. Confirmed: the picker greys out "Ronin AI" ("Already in the set",
   button disabled) even though it was never explicitly added — it's only
   reachable via CO-OP's closure; the explicit pick
   "ConflictPVERemixedVanilla2.0" correctly stays visible/enabled at top
   level despite also being in that closure (the `roots`-exclusion rule in
   `coverageFromGraph`); Nested view renders the full multi-level tree
   correctly; the per-row "Show dependencies" disclosure
   (`AssignedModExpanded` → `ModTree`) renders the parent's dependency subtree
   correctly inline. Change was not saved (discarded by navigating away),
   leaving the server's real data untouched.
4. Modpack Editor (`Modpacks.tsx`): same scenario reproduced on a real
   modpack — greying, Nested view, and the picker's no-preview/no-paging mode
   (`showDepsPreview={false}`) all matched spec. Change cancelled, not saved.
5. Mod Detail (`ModDetail.tsx`): "This Mod Requires" nested tree on CO-OP
   Conflict PVE matches the `/graph` data exactly (3 direct+transitive deps,
   correct duplication); "Required By" correctly shows "No other mod depends
   on this one" for a leaf mod.
6. **Data fix, not a sprint bug**: while testing, the user noticed one
   server's assigned-mods row displayed a raw GUID instead of a name.
   Investigated and confirmed via `git log` this was **not** a sprint 12
   regression — it was a pre-existing `NULL` `ServerMod.mod_name` in the dev
   database, written before commit `96e129a` (2026-09-17, "fix: preserve mod
   display name when a server-mods patch omits it"). That fix is forward-only
   (a *surviving* row with no name in the payload keeps whatever it already
   had — including a pre-existing `NULL`; only a *brand-new* row without a
   name resolves from the library). Fixed the one stale row directly via the
   existing `PATCH /api/servers/{id}` endpoint (no code change) at the user's
   request.

Not exercised: triggering a real `mod_sync` job to watch the freshness pill /
highlight-on-change transition live (would need to wait for a real scan to
complete); `docker-compose` was already running with pre-existing dev data
rather than a from-scratch clean-checkout `up`.

## How this sprint was implemented

Orchestrated per the `sprint` skill's Phase D/E split, with the user's
explicit instruction to rotate native Claude subagents and two opencode
models (`openrouter/z-ai/glm-5.3-flash`, `openrouter/deepseek/
deepseek-v4.1-flash`) roughly 50/50, capped at one opencode process per model
running concurrently. PLAN.md (pre-existing) was read first; STORIES.md was
written fresh for this session after reading every file the plan's frontend
section touches (none of that research had been done when PLAN.md was
written, which is why two of PLAN's frontend claims needed correcting in
STORIES.md rather than followed verbatim — see "Deviations" above). Phase 1
(S1 backend, S2 frontend graph library) ran in parallel; Phase 2 (S3/S4/S5,
three independent new components) ran three-way parallel once S2 landed;
Phase 3 (S6–S9, four independent consuming pages) ran four-way parallel once
Phase 2 landed — two native-Claude agents and one process each of glm/
deepseek at a time, respecting the concurrency cap. Every story's diff was
read and checked against its brief before being trusted, per this project's
git-safety convention (review before trusting an agent's self-report) — this
caught both incidents described above. S10 (delete `modCoverage.ts`) was done
directly rather than delegated, being a one-line-of-work cleanup step.
