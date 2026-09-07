# Reforger Manager — Sprint 8 plan

## Context

UX pass on the mod-management surface (2026-09-07). Eight reports across
`/mods`, `/mods/:guid`, the server **Mods** tab, and `/storage`. Mostly
frontend; three small backend additions (mods-list computed fields, a mod
`references` route, a `guid` field on preflight checks). No schema/migration.

`/storage` is deleted **as a page** — the `/api/storage` route stays (MCP
`get_storage_usage`, `orphan_reference_sets` lives there and `api/mods.py`
imports it). Its data folds into `/mods`.

## Reports

### R1 — /mods Addon Inventory → sortable table
`pages/Mods.tsx` `ModRow` is a card `<article>` (`:403-508`). Replace the
inventory card body with a `<table>`: **Name · GUID · Installed · Latest ·
Cache size · Orphan · State · Actions**. Every column header is a click-to-sort
toggle (asc → desc → none), client-side over `modsQuery.data`. Row actions stay
(Download/Verify/Pin/Unpin + R4 deletes). Summary/tags/pin-reason move into an
expandable detail row or a title — keep the table scannable.

### R2 — Cache size + Orphan columns (backend)
`GET /api/mods` today returns `size` = Workshop-declared bytes, not on-disk.
Add `?refs=1` → each `ModOut` also carries:
- `cache_bytes: int | null` — real on-disk size (`storage._bytes_for(guid)`,
  `null` when not local).
- `is_orphan: bool` — `is_local` AND not in `directly_referenced` AND not in the
  dependency closure (`storage.orphan_reference_sets`), i.e. the `/storage`
  "Orphans" condition.
- `is_unreferenced: bool` — same but `not is_local` (the `/storage`
  "Unreferenced Library Entries" condition).
- `kept_by: list[str] | null` — parent mod names when the mod is on disk, not
  directly referenced, but held by another mod's closure (`/storage`
  "Kept As Dependencies").

The **Orphan** column renders: `Orphan` (is_orphan), `Stale row`
(is_unreferenced), `Kept: <names>` (kept_by), else `—`. Folds all three
`/storage` sections into one column. `?refs=1` is opt-in so the ModsPanel
library picker (`/api/mods?local=true`) stays cheap.

### R3 — "Re-download" → "Download" when absent
`Mods.tsx:485-487` (and `ModDetail.tsx:216-218`): button label is
`mod.is_local ? "Re-download" : "Download"`.

### R4 — deleting a mod: warn with what references it
`/mods` inventory has no delete today (it lives on `/mods/:guid`). With
`/storage` gone, add per-row **Remove from disk** (`DELETE /api/mods/{guid}/local`)
and **Remove from library** (`DELETE /api/mods/{guid}`), each behind a confirm
dialog. The dialog lists blocking references from a new
`GET /api/mods/{guid}/references` → `{ servers: string[], modpacks: string[],
required_by: {guid,name}[] }` (the same sets `_delete_block_detail` already
computes for the 409). Backend delete guards are unchanged — the dialog just
surfaces them before the click.

### R5 — top toolbar: Scan cache · Check updates · Apply all updates
`Mods.tsx` — "Apply all updates" is in the filter card (`:236-244`). Move it up
next to "Scan cache" / "Check updates" in the `PageHeading` actions. "Verify and
repair library" stays in the secondary row.

### R6 — "Workshop Search" → "Add mod" box, two buttons
`Mods.tsx:261-303` card + the separate header "Add mod" dialog (`:187`,
`:372-398`). Collapse into one **Add mod** card: a single `<Input>` + two
buttons —
- **Add mod** → `POST /api/mods/add {url_or_id}` (accepts a Workshop URL or a
  16-hex GUID; existing `addMutation`).
- **Search mod** → `GET /api/mods/search?q=` by name/keyword (existing
  `wsSearchQuery` + `SearchResultRow`).
Drop the header "Add mod" button and its `<Dialog>`; inline errors under the box.

### R7 — /mods/:guid Dependency Tree: clickable child mods
`ModDetail.tsx:329-344` renders each `dependency_tree.nodes` entry as a
`<span>`. Make it a `<Link to={\`/mods/${node.guid}\`}>` when
`node.state !== "unresolved"` and `node.depth > 0` (root is the mod itself;
unresolved has no page) — mirror the "Required By" list (`:363-372`).

### R8 — server Mods tab pre-flight: resolve names, group by mod, link
`ModsPanel.tsx:602-624` lists raw `preflight.data.checks` — `check.name` is
`"Workshop <GUID>"` / `"Engine compatibility <GUID>"` / `"Stale pin <GUID>"`.
Two parts:
- **Backend** (`servers/preflight.py`): add `guid: str | None` to
  `PreflightCheck` (+ `as_dict` drops it when `None`, like `fix`). Pass
  `guid=guid` on every per-mod check (`Workshop`, `Engine compatibility`,
  `Stale pin`). Report already carries `resolved_mods[* ].{guid,name}`.
- **Frontend**: group checks by `guid`. Each group = a heading linking to
  `/mods/{guid}` with the resolved name (from `resolved_mods`), then the
  per-check `level` badge + `detail`/`fix`. Checks with no `guid` (`dependency
  enumeration`, `Download space`, the green `preflight` line) go in an
  "Other checks" group.

### R9 — "Add mods from the library" list: paginate 10/page
`ModsPanel.tsx:430-441` caps `candidates` at 60 with no paging. Page it 10 at a
time (Prev/Next + "x–y of n"); reset to page 1 when `modSearch` / `showNonLocal`
changes.

### R10 — remove the /storage page
Delete `pages/Storage.tsx`, its `<Route>` (`app.tsx:29`, and the import
`:12`), and the nav entry (`Shell.tsx:12`). Leave `api/storage.py`,
`schemas/storage.py`, the MCP tool, and `orphan_reference_sets` untouched.

## Decisions locked

| # | Decision |
|---|---|
| 1 | R1 table is client-sorted only — no new query params for sort/order. Default order = current (`name` nulls last, then `guid`). Sort state is local component state, not URL. |
| 2 | R2 heavy fields are behind `?refs=1`. Without it `ModOut` is byte-identical to today (`cache_bytes` etc. absent / `None`). Only `Mods.tsx` sends `?refs=1`. Reuse `storage._bytes_for` + `storage.orphan_reference_sets` verbatim (import from `..api.storage`). |
| 3 | R2/R4: no change to any delete guard or the `/storage` route. New `GET /api/mods/{guid}/references` is read-only and reuses the queries inside `_delete_block_detail` (refactor those into a helper returning the structured sets; the 409 string builder calls the same helper). |
| 4 | R4 dialogs mirror the current `ModDetail.tsx` delete dialog wording. "Remove from disk" disabled/greyed when `!is_local`; "Remove from library" always available. A 409 from the backend still surfaces inline (belt and braces). |
| 5 | R6: `POST /api/mods/add` already extracts a 16-hex GUID from a URL or bare id — "Add mod" needs no backend change. "Search mod" reuses `/api/mods/search`. One shared input; the two buttons read the same `addInput` state. |
| 6 | R8: `guid` on `PreflightCheck` (dataclass) is the only backend change — `/preflight` returns a bare `dict`, no response model. `lib/api.ts` `Preflight` type gains `guid?: string \| null` on the check shape and a typed `resolved_mods` entry (`{guid,name,state?,version?}`). `preflight_all` / MCP unaffected (extra optional key). |
| 7 | R10 is frontend-only. `git grep storage` in `frontend/` after: only comments/unrelated. `npm run build` must stay clean (drop now-unused imports). |
| 8 | No commit/push. No `.sprints/` edits by story agents (S7 writes `RESULTS.md`). No migration. |

## Backend API changes

- `GET /api/mods?refs=1` — adds `cache_bytes`, `is_orphan`, `is_unreferenced`,
  `kept_by` to each row. Default (no param) unchanged.
- `GET /api/mods/{guid}/references` — `{servers, modpacks, required_by}`, new,
  read-only.
- `GET /api/servers/{id}/preflight` — each check may now include `guid`.

## Non-goals

- Server-side sorting/pagination for the mods list.
- Touching `orphan_reference_sets`, `schemas/storage.py`, `/api/storage`, or the
  `get_storage_usage` MCP tool.
- New delete semantics — only surfacing the existing guards earlier.
- Reworking `SortableModList` (R9 paginates the plain `<ul>` above it, not the
  sortable list).
- Any change to the dependency resolver or `config_gen`.
- A charting/table dependency — hand-rolled `<table>` like `ModDetail.tsx`.

## Verification

```bash
cd frontend && npm run build                              # tsc -b && vite build — clean
cd backend && ./.venv/Scripts/python.exe -m pytest -q     # green, incl. new mods-refs + references + preflight-guid tests
```

Manual on a running stack: `/mods` shows the free-space bar on top, a sortable
inventory table with Cache size + Orphan columns, "Download" on not-local rows,
delete dialogs that name the blocking servers/packs; the Add mod box adds by
URL/GUID and searches by name; `/mods/:guid` dependency children link through;
the server Mods tab pre-flight groups warnings under a linked mod name and pages
the library picker 10 at a time; `/storage` is gone from the nav and routes to
`/`.
