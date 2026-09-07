# Reforger Manager — Sprint 8 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-07 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: done, green

All seven stories implemented — S1–S6 plus the S7 integration pass. No schema
change, no migration (as planned). Backend suite green (**327 passed**, Sprint 7
baseline 319 → +8: S1 +6, S4 +2); frontend `npm run build` clean
(`tsc -b && vite build`). No Docker, no live stack.

## Report → fix mapping (R1–R10)

| Report | Story | Fix |
| --- | --- | --- |
| R1 — `/mods` inventory is a card list | S2 | `ModRow` `<article>` cards replaced by a `<table>` in `overflow-x-auto`: Name · GUID · Installed · Latest · Cache size · Orphan · State · Actions. Each data `<th>` is a `<button>` cycling `none → asc → desc → none`, sorted client-side over `modsQuery.data` (local `useState`, nulls last, `localeCompare({numeric:true})` for versions, numeric for `cache_bytes`). Summary / pin reason / tags / required-by / game version moved to a collapsible detail row toggled from the Name cell. |
| R2 — cache size + orphan columns (backend) | S1 | `GET /api/mods?refs=1` adds `cache_bytes` (real on-disk via `storage._bytes_for`, `null` when not local), `is_orphan`, `is_unreferenced`, `kept_by` to each `ModOut`. Opt-in — no param = byte-identical response (four keys serialize at defaults). Reuses `storage.orphan_reference_sets` verbatim. |
| R3 — "Re-download" shown when mod absent | S2, S3 | Button label `mod.is_local ? "Re-download" : "Download"` on both `/mods` rows and `/mods/:guid`. |
| R4 — deleting a mod: warn with what references it | S1, S2 | New read-only `GET /api/mods/{guid}/references` → `{servers, modpacks, required_by}` (reuses the queries from `_delete_block_detail`, refactored into `_mod_references`). Per-row **Remove from disk** / **Remove from library** each open a confirm `<Dialog>` that fetches `/references` first and lists blockers; a 409 still surfaces inline. |
| R5 — top toolbar consolidation | S2 | `PageHeading` actions = Add mod · Scan cache · Check updates · Apply all updates ("Apply all updates" moved up from the filter card). "Verify and repair library" stays in the secondary row. |
| R6 — "Workshop Search" → "Add mod" box | S2 | One **Add mod** card: a single `<Input>` (`addInput`) + **Add mod** (`POST /api/mods/add`) and **Search mod** (`setWsQuery` → existing `wsSearchQuery` + `SearchResultRow`). Header "Add mod" dialog and `addOpen` state removed; `addError` inline. |
| R7 — dependency tree children not clickable | S3 | Each `dependency_tree.nodes` name becomes `<Link to={/mods/${node.guid}}>` when `node.state !== "unresolved" && node.depth > 0`; root and unresolved rows stay plain `<span>`. `via`/`state` badges + depth indent untouched. |
| R8 — server Mods tab pre-flight: raw `Workshop <GUID>` names | S4, S5 | **Backend:** `PreflightCheck` gains `guid: str \| None = None` (dropped from `as_dict` when `None`, like `fix`); passed on every per-mod check (`Workshop`, `Engine compatibility`, `Stale pin`). **Frontend:** checks partitioned into per-`guid` groups (first-appearance order) + one trailing "Other checks" group; each group heads with `<Link to={/mods/${guid}}>{resolved name ?? guid}</Link>` over the per-check `level` badge + `detail`/`fix`. |
| R9 — library picker: no paging (capped at 60) | S5 | `candidates` sliced 10/page with Prev/Next (disabled at ends) + `"{start}–{end} of {n}"`; `page` resets to 0 when `modSearch` / `showNonLocal` changes; `pageIndex` clamp keeps the slice non-empty on a shrinking list. |
| R10 — remove the `/storage` page | S6 | `pages/Storage.tsx` deleted; `<Route path="/storage">` + import dropped from `app.tsx`; nav entry dropped from `Shell.tsx`. `/api/storage`, `schemas/storage.py`, `orphan_reference_sets`, and the MCP tool untouched — the remaining `["storage"]` query keys in `Mods.tsx` / `ModDetail.tsx` still hit that live route. |

## What shipped

### Backend

- **S1 — `api/mods.py` + `schemas/mod.py` + new `tests/test_mod_refs.py`.**
  `list_mods` gains `refs: bool = Query(default=False)`; when set it computes
  `orphan_reference_sets` once, builds a `guid → name` map from `rows`, and fills
  `cache_bytes` / `is_orphan` / `is_unreferenced` / `kept_by` per row. `ModOut`
  gains those four fields under `# computed` (defaults keep the no-param response
  behaviourally unchanged). `_delete_block_detail`'s reference-gathering half
  extracted into `async _mod_references(session, guid) -> (server_names,
  modpack_names, required_by)`; the 409 string builder now calls it and is
  otherwise unchanged. New `GET /{guid}/references` → `ModReferencesOut`,
  registered **before** `/{guid}` (path-shadow order), upper-cases the guid, 404s
  on unknown. 6 new tests (orphan / assigned / kept-by / default-no-fields /
  `/references` server+pack / 404).
- **S4 — `servers/preflight.py` + `tests/test_preflight.py`.** `PreflightCheck`
  gains `guid: str | None = None` after `fix`; `as_dict` pops it when `None`.
  `guid=guid` passed on the four `Workshop {guid}` checks, the four
  `Engine compatibility {guid}` checks, and the `Stale pin {guid}` check; the
  bare `Engine compatibility`, `dependency enumeration`, `Download space`, and
  the green `preflight` line keep `guid=None`. `preflight_all` / MCP untouched
  (extra optional key). 2 new tests.

### Frontend

- **S2 — `pages/Mods.tsx`** (full page-body rewrite; `lib/api.ts` left untouched —
  types kept local). `ModRecord` extended with the four `refs=1` fields; new
  `ModReferences` + trimmed `StorageReport` types. `FreeSpaceBar` + `formatSize`
  recovered verbatim from `git show HEAD:frontend/src/pages/Storage.tsx` and
  inlined. `modsQuery` → `/api/mods?…&refs=1` (existing `local`/`state`/`update`
  params preserved). New `storageQuery` (`["storage"]`) rendered as an "Addon
  Cache Free Space" `Card` between the heading and the filter card. Sortable
  `<table>` inventory, per-row Download/Re-download · Verify · Pin/Unpin · Remove
  from disk (disabled when `!is_local`) · Remove from library, both deletes
  behind a `/references`-fetching confirm `<Dialog>`; success invalidates
  `["mods"]` + `["storage"]`. "Add mod" card replaces "Workshop Search".
- **S3 — `pages/ModDetail.tsx`.** Dependency-tree name → `<Link>` for
  resolved, non-root nodes; `onClick` `stopPropagation`, `hover:text-amber-400`.
  11-line change; `Link` was already imported (Required By list).
- **S5 — `components/server/ModsPanel.tsx` + `lib/api.ts`.** `Preflight.checks[]`
  gains `guid?: string | null`; `resolved_mods` typed as `{guid, name, state?,
  version?}[]` (was `unknown[]`). New `preflightGroups` `useMemo` partitions
  checks by `guid` + "Other checks" tail; per-group `<Link>` heading reuses the
  existing badge-tone ternary. Library picker paginated 10/page with Prev/Next +
  range label + page reset. The Sprint 7 dedup / `topLevel` / `SortableModList`
  block was not touched.
- **S6 — `app.tsx` + `components/Shell.tsx`; `pages/Storage.tsx` deleted.**
  Import + route + nav entry removed; unmatched paths fall through to the
  existing `<Navigate to="/" />`. `git grep storage frontend/src` → only the
  legit `["storage"]` react-query keys remain.

## Data-model changes

**None.** No migration. `PreflightCheck.guid` is a dataclass field, not
persisted; the four `ModOut` `refs=1` fields are computed per-request.

## New API / FE surface

- `GET /api/mods?refs=1` — each row also carries `cache_bytes: int | null`,
  `is_orphan: bool`, `is_unreferenced: bool`, `kept_by: string[] | null`.
  Without `?refs=1` the four keys serialize at their defaults (`null` / `false`).
- `GET /api/mods/{guid}/references` → `{servers: string[], modpacks: string[],
  required_by: {guid, name}[]}`; 404 on unknown guid. Read-only.
- `GET /api/servers/{id}/preflight` — a per-mod check may now include `guid`.
- FE: `/storage` route + nav entry removed (`/api/storage` route unchanged);
  `Preflight` type gains `checks[].guid?` + typed `resolved_mods`.

## Verification performed

- `cd backend && ./.venv/Scripts/python.exe -m pytest -q` → **327 passed,
  91 warnings in ~86s** (full suite, sqlite+aiosqlite). Run independently by the
  orchestrator after each backend story and once at S7.
- `cd frontend && npm run build` → clean (`tsc -b && vite build`; pre-existing
  >500 kB chunk advisory only). Run independently after every FE story and at S7.
- `git grep -n "StoragePage\|pages/Storage" -- frontend/src` → empty.
- `git grep -n storage -- frontend/src` → only `["storage"]` query keys in
  `Mods.tsx` / `ModDetail.tsx` (valid against the retained `/api/storage`).
- `git diff frontend/package.json backend/requirements.txt` → empty (no new
  deps; hand-rolled `<table>` per PLAN non-goal).
- Diffs reviewed per story: S1 route ordering + `_mod_references` refactor; S4
  `guid` propagation sites; S3 self-link guard; S5 changes confined to the
  pre-flight and picker `<section>`s; S6 no stray import.

## Not exercised

- Docker / live stack / real Workshop API / real addon cache — no `docker
  compose up`, no browser click-through. All FE stories are build-gated only
  (project rule: `npm run build` is the sole frontend check).
- The PLAN "Manual verification" list (free-space bar on `/mods`, sortable table
  with Cache size + Orphan, "Download" on not-local rows, delete dialogs naming
  blockers, Add mod by URL/GUID + search by name, `/mods/:guid` dependency
  children linking, grouped pre-flight, 10/page picker, `/storage` 404→`/`) — not
  driven on a running stack this session.
- `_bytes_for` on a real multi-GB addon dir under `?refs=1` — cost is the same
  walk `/api/storage` already does, but only measured against the sqlite test
  fixtures here (`cache_bytes=0`).
- CI / deploy; nothing committed or pushed.

## Minor observations

- **S2 kept the new types local to `Mods.tsx`** rather than adding them to
  `lib/api.ts` (the brief permitted either). This incidentally removed a
  concurrent-write hazard: S5 was editing `lib/api.ts` in parallel, and S2
  never touching that file meant no merge risk. S5's `Preflight.guid` change is
  intact.
- S2 deviations (all sensible, all noted by the agent): free-space `Card` sits
  below `PageHeading` and above the filter card (read of "above the toolbar/filter
  area"); the Actions `<th>` is plain text (nothing to sort by); `formatSize` is
  now the recovered `(bytes: number)` signature and every new call site guards
  `null` first; the toolbar "Add mod" action scrolls to + focuses the Add mod
  input (there is no dialog any more).
- `GET /api/mods` without `?refs=1` now emits four extra keys at their defaults.
  No existing test asserted an exact body and the full suite is green, but a
  strict external consumer of that response shape would see the new keys.
- S1's `_mod_references` adds a `required_by` (reverse-dependency) query that
  `_delete_block_detail` did not previously run; `_delete_block_detail` discards
  it (`_required_by`) and its 409 message is unchanged.

## How this sprint was implemented

Orchestrator (Claude Sonnet 5) stayed a thin coordinator — PLAN + STORIES held
every fact; no story was implemented by the orchestrator and no re-research was
done beyond reviewing diffs to accept a hand-off. Three agent types rotated,
even counts:

| Story | Agent | Notes |
| --- | --- | --- |
| S1 | native Claude subagent | foundational backend (`?refs=1` + `/references` + `_mod_references` refactor); clean first pass, `327 passed`. |
| S2 | native Claude subagent | the largest story — full `Mods.tsx` rewrite (R1/R3/R4/R5/R6 + free-space move); ~8 min, both gates green first pass. |
| S3 | opencode · glm (`z-ai/glm-5.3-flash`) | dependency-tree links; clean in one pass, ~1 min. |
| S4 | opencode · deepseek (`deepseek/deepseek-v4-flash-0731`) | `PreflightCheck.guid`; clean first pass, ran full `pytest` itself (`321 passed` pre-S1-merge), orchestrator re-verified at `327`. |
| S5 | opencode · glm | grouped pre-flight + paginated picker; changes correctly confined to the two target `<section>`s, build clean. |
| S6 | opencode · deepseek | `/storage` page removal; clean first pass, build clean. |
| S7 | orchestrator | full `pytest` + `npm run build` + `git grep` checks + per-story diff review + this file. |

Rotation totals: native Claude ×2 (S1, S2 — the foundational backend story and
the largest FE story), opencode·glm ×2 (S3, S5), opencode·deepseek ×2 (S4, S6).

Dependency handling: two chains (S1→S2 on `/mods`, S4→S5 on pre-flight) plus
standalones S3, S6. Wave 1 ran S1 (native) ‖ S3 (glm) ‖ S6 (deepseek) in
parallel over disjoint files. S4 (independent backend) was pulled onto deepseek
in wave 1's tail so it did not block behind S1. Wave 2 ran S2 (native, after S1)
‖ S5 (glm, after S4). S2 and S5 both nominally touch `lib/api.ts` and ran
concurrently — accepted because S5 writes only the `Preflight` type and S2 was
briefed to keep its types local (it did). No opencode stalls, no restarts, no
reassignments this sprint. Nothing committed or pushed.
