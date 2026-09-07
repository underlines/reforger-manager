# Reforger Manager — Sprint 8 stories

> Context + code anchors: [PLAN.md](PLAN.md). `R#` = a report there. Line
> numbers verified 2026-09-07 — trust the symbol names if one drifted.

## Shape

Seven stories. Two chains + standalones:

- **/mods chain:** S1 (backend) → S2 (`Mods.tsx`, needs S1's fields/route).
- **preflight chain:** S4 (backend) → S5 (`ModsPanel.tsx`).
- **Standalone, parallel:** S3, S6.

| Story | Agent | Files | Depends on |
|---|---|---|---|
| S1 | native Claude | `backend/app/api/mods.py`, `backend/tests/` | — |
| S2 | native Claude | `frontend/src/pages/Mods.tsx`, `frontend/src/lib/api.ts` | **S1** |
| S3 | opencode · glm | `frontend/src/pages/ModDetail.tsx` | — |
| S4 | native Claude | `backend/app/servers/preflight.py`, `backend/app/schemas/` (preflight), `backend/tests/` | — |
| S5 | native Claude | `frontend/src/components/server/ModsPanel.tsx`, `frontend/src/lib/api.ts` | **S4** |
| S6 | opencode · deepseek | `frontend/src/app.tsx`, `frontend/src/components/Shell.tsx`, delete `frontend/src/pages/Storage.tsx` | — |
| S7 | native Claude | integration + `.sprints/8/RESULTS.md` | S1–S6 |

Gate per story: `cd frontend && npm run build` clean (FE) and
`cd backend && ./.venv/Scripts/python.exe -m pytest -q` green (BE; regression for
FE stories). No commit/push. Don't touch `.sprints/` (S7 excepted), migrations,
`/api/storage`, or MCP.

---

## S1 — mods list `?refs=1` fields + `/references` route · R2, R4

`backend/app/api/mods.py` only (+ tests).

**Deliver.**
1. `list_mods`: add `refs: bool = Query(default=False)`. When true, after
   loading `rows`, compute once:
   `directly_referenced, closure_owners = await orphan_reference_sets(session)`
   (already imported from `.storage`); `closure = set(closure_owners)`; a
   `guid -> name` map from `rows`. For each `ModOut`:
   - `cache_bytes = storage._bytes_for(m.guid) if m.is_local else None`
     (import `_bytes_for` from `..api.storage`, or add a thin public alias).
   - `is_orphan = m.is_local and m.guid not in directly_referenced and m.guid not in closure`
   - `is_unreferenced = (not m.is_local) and m.guid not in directly_referenced and m.guid not in closure`
   - `kept_by = [name_or_guid for owner in sorted(closure_owners.get(m.guid, set())) if owner != m.guid]`
     when `m.is_local and m.guid not in directly_referenced and m.guid in closure`, else `None`.
   Without `refs=1` none of these are set (leave as schema defaults).
2. `ModOut` (`schemas/mod.py`): add `cache_bytes: int | None = None`,
   `is_orphan: bool = False`, `is_unreferenced: bool = False`,
   `kept_by: list[str] | None = None` under the `# computed` block.
3. Refactor the reference-gathering half of `_delete_block_detail` into
   `async def _mod_references(session, guid) -> tuple[list[str], list[str], list[ModRefOut]]`
   returning `(server_names, modpack_names, required_by)`. `_delete_block_detail`
   calls it and keeps building its 409 string (parents come from
   `closure_owners`, unchanged).
4. New route **before** `/{guid}` (path-shadow order — put it by
   `/{guid}/dependencies`):
   ```python
   @router.get("/{guid}/references", response_model=ModReferencesOut)
   async def get_mod_references(guid: str, session=Depends(get_session)):
       guid = guid.upper()
       if await session.get(Mod, guid) is None:
           raise HTTPException(404, "mod not found")
       servers, packs, required_by = await _mod_references(session, guid)
       return ModReferencesOut(servers=servers, modpacks=packs, required_by=required_by)
   ```
   `ModReferencesOut` in `schemas/mod.py`: `servers: list[str]`,
   `modpacks: list[str]`, `required_by: list[ModRefOut]`.
5. Tests (`backend/tests/`): `?refs=1` marks a downloaded-but-unassigned mod
   `is_orphan`, an assigned mod not orphan, a dependency-only mod
   `kept_by=[parent]`; `/references` returns the assigning server + pack names;
   404 on unknown guid; default list response has no `refs` fields set.

**Done when.** `pytest -q` green. `GET /api/mods` unchanged without `?refs=1`.

**Watch out.**
- `_bytes_for` walks the addon dir per local mod — acceptable (it's what
  `/api/storage` already does), but only under `refs=1`.
- `orphan_reference_sets` returns upper-cased guids; `Mod.guid` is stored
  upper-case — compare directly, don't re-normalise inconsistently.

---

## S2 — /mods page rework · R1, R3, R5, R6, R2/R4 (frontend)

`frontend/src/pages/Mods.tsx` (+ types in `lib/api.ts`). One file, several
deliverables — do them in this order.

**Deliver.**
1. **Types** (`lib/api.ts` or local): extend the mods list row with
   `cache_bytes?: number \| null`, `is_orphan?: boolean`,
   `is_unreferenced?: boolean`, `kept_by?: string[] \| null`; add
   `ModReferences = { servers: string[]; modpacks: string[]; required_by: {guid:string;name:string\|null}[] }`.
2. **`modsQuery`**: fetch `/api/mods?refs=1` merged with the existing filter
   string (`filterString ? \`?${filterString}&refs=1\` : "?refs=1"`).
3. **Free-space bar (R7-of-storage / PLAN R10 move):** add a `storageQuery`
   (`useQuery(["storage"], () => api<StorageReport>("/api/storage"))`, reuse the
   `StorageReport`/`FreeSpaceBar` shape lifted from the old `Storage.tsx`) and
   render `FreeSpaceBar` + `mods_path` in a `Card` **above** the toolbar/filters.
4. **Toolbar (R5):** `PageHeading` `actions` = **Add mod** (see 7) · **Scan
   cache** · **Check updates** · **Apply all updates** (move from the filter
   card `:236-244`). Keep "Verify and repair library" in the secondary row.
5. **Inventory table (R1):** replace the `ModRow` card list with a `<table>` in
   an `overflow-x-auto` wrapper. Columns: **Name · GUID · Installed · Latest ·
   Cache size · Orphan · State · Actions**. Each `<th>` is a `<button>` cycling
   `none → asc → desc → none`; sort `modsQuery.data` client-side (string compare;
   `cache_bytes`/versions numeric-ish; nulls last). Orphan cell:
   `is_orphan ? "Orphan" : is_unreferenced ? "Stale row" : kept_by?.length ? \`Kept: ${kept_by.join(", ")}\` : "—"`
   (badge tones warn/neutral/neutral). State cell = the existing
   `availability`/`availabilityTone` logic. Move summary/pin-reason/tags into a
   collapsible detail row (click the Name cell) or drop to a `title` — keep rows
   one line tall.
6. **Row actions:** **Download**/**Re-download** by `mod.is_local` (R3) ·
   **Verify** (unchanged, needs `is_local`) · **Pin/Unpin** (unchanged) ·
   **Remove from disk** (disabled when `!is_local`) · **Remove from library**.
   Both deletes open a confirm `<Dialog>` that first fetches
   `GET /api/mods/{guid}/references` and lists blocking servers / modpacks /
   required-by (mirror `ModDetail.tsx:413-454` wording). Mutations:
   `DELETE /api/mods/{guid}/local` and `DELETE /api/mods/{guid}`; on success
   invalidate `["mods"]` + `["storage"]`; a 409 body shows inline.
7. **Add mod card (R6):** replace the "Workshop Search" card with an **Add mod**
   card — one `<Input>` (`addInput`) + two buttons:
   - **Add mod** → `addMutation.mutate(addInput.trim())`
     (`POST /api/mods/add {url_or_id}`).
   - **Search mod** → `setWsQuery(addInput.trim())` → existing `wsSearchQuery`
     + `SearchResultRow` results below.
   Remove the `addOpen` header button and the `<Dialog open={addOpen}>`
   (`:372-398`); keep `addError` inline under the input.

**Done when.** `npm run build` clean. Manual per PLAN "Verification".

**Watch out.**
- `?refs=1` rows may still be missing the fields if S1 isn't merged — guard with
  `?? undefined` and default the Orphan cell to `—`.
- Don't break the existing `local` / `state` / `updatesOnly` filters — they
  still go in the query string alongside `refs=1`.
- `FreeSpaceBar` + `formatSize` already exist in the old `Storage.tsx` — copy,
  don't re-invent; S6 deletes that file.
- Keep `SearchResultRow` as-is.

---

## S3 — /mods/:guid Dependency Tree links · R7

`frontend/src/pages/ModDetail.tsx` only.

**Deliver.**
- In the Dependency Tree map (`:331-343`), replace the
  `<span>{node.name ?? node.guid}</span>` with a `<Link to={\`/mods/${node.guid}\`}
  className="... hover:text-amber-400" onClick={(e)=>e.stopPropagation()}>` when
  `node.state !== "unresolved" && node.depth > 0`; otherwise keep the plain
  `<span>` (root node = the mod itself; unresolved has no detail page). Keep the
  `via`/`state` badges and the `paddingLeft` depth indent.

**Done when.** `npm run build` clean; dependency children navigate to their own
detail page, the root row and unresolved rows do not.

**Watch out.** `dependency_tree.nodes` includes the root (`depth 0`, guid ===
the page's guid) — don't render a self-link.

---

## S4 — preflight checks carry `guid` · R8 (backend)

`backend/app/servers/preflight.py` + tests. The `/preflight` route returns
`PreflightReport.as_dict()` as a bare `dict` (`api/servers.py:365-368`) — no
Pydantic response model to touch.

**Deliver.**
1. `PreflightCheck`: add `guid: str | None = None` (after `fix`). `as_dict`
   pops `guid` when `None` (same pattern as `fix`).
2. Pass `guid=guid` on every per-mod check in `preflight()`: the two
   `f"Workshop {guid}"` checks (`:124`, `:130`, `:138`, `:145`), every
   `f"Engine compatibility {guid}"` (`:165`, `:171`, `:176`, `:183`), and
   `f"Stale pin {guid}"` (`:195`). Leave `dependency enumeration`,
   `Download space`, the bare `Engine compatibility` (`:203`) and the green
   `preflight` line with `guid=None`. `resolved_mods` already carries
   `{guid,name,state,version,...}` for the frontend name lookup.
3. Tests: a report whose Workshop/compat/stale checks expose the offending
   `guid`; checks without a mod keep no `guid` key in `as_dict()`.

**Done when.** `pytest -q` green; `GET /api/servers/{id}/preflight` checks
include `guid` where a single mod is implicated.

**Watch out.** `preflight_all` and the MCP `get_server_preflight` tool just
pass the dict through — an extra optional key is safe; don't change their
signatures.

---

## S5 — server Mods tab: grouped pre-flight + paginated picker · R8, R9

`frontend/src/components/server/ModsPanel.tsx` (+ `Preflight` type in
`lib/api.ts`). **S5 shares this file's history with Sprint 7 — only touch the
pre-flight `<section>` (`:588-629`) and the "Add mods from the library"
`<section>` (`:763-800`).**

**Deliver.**
1. **Types** (`lib/api.ts`): `Preflight.checks[]` gains `guid?: string \| null`;
   add `Preflight.resolved_mods: Array<{ guid: string; name: string \| null;
   state?: string; version?: string \| null }>` (replace the `unknown[]`).
2. **Grouped pre-flight render** (`:602-624`): build
   `nameByGuid` from `preflight.data.resolved_mods`. Partition `checks` into
   groups keyed by `guid` (order: first appearance) + one trailing **Other
   checks** group for `guid == null`. Per mod group: a heading —
   `<Link to={\`/mods/${guid}\`}>{nameByGuid[guid] ?? guid}</Link>` — then each
   check as a `level` badge + `detail`/`fix` line (reuse the existing badge-tone
   switch). Keep the top-level `verdict` `<Badge>`. Keep the
   not-run-yet / fetching / error branches untouched.
3. **Paginate the picker** (`:786-799`): keep `candidates` but slice to
   `page * 10 .. page * 10 + 10`; add Prev/Next buttons + `"{start}–{end} of
   {candidates.length}"`. `useState` `page = 0`; `useEffect` resets `page` to 0
   when `modSearch` or `showNonLocal` changes. Empty-state unchanged.

**Done when.** `npm run build` clean; pre-flight warnings sit under a linked mod
name grouped by mod; the library picker shows 10 rows with working paging.

**Watch out.**
- Don't disturb the dependency-dedup / `topLevel` / `SortableModList` block
  (Sprint 7) — R9 pages the plain `candidates` `<ul>`, not the sortable list.
- `resolved_mods` entries use upper-case guids like `check.guid` — match
  directly.
- A check's `guid` may name a mod that has no `resolved_mods` entry (rare) —
  fall back to the guid string, still link it.

---

## S6 — remove the /storage page · R10

`frontend/src/app.tsx` + `frontend/src/components/Shell.tsx`; delete
`frontend/src/pages/Storage.tsx`.

**Deliver.**
1. `app.tsx`: drop the `StoragePage` import (`:12`) and its `<Route path="/storage">` (`:29`).
2. `Shell.tsx`: drop `{ to: "/storage", label: "Storage" }` from `navigation` (`:12`).
3. Delete `pages/Storage.tsx`.
4. `git grep -n storage frontend/src` — only unrelated hits (`["storage"]`
   query keys in `Mods.tsx`/`ModDetail.tsx`, which stay valid against
   `/api/storage`). Fix any now-unused import so `tsc -b` is clean.

**Done when.** `npm run build` clean; `/storage` is absent from the nav and any
`/storage` URL falls through to `<Navigate to="/" />`.

**Watch out.** `S2` copies `FreeSpaceBar` + the `StorageReport` type out of this
file first — coordinate: if S2 lands after S6, pull them from git history. Don't
touch `backend/app/api/storage.py` or `schemas/storage.py`.

---

## S7 — integration + RESULTS

`.sprints/8/RESULTS.md`. Run both gates clean. Smoke-check the PLAN
"Verification" list on a running stack. Record per-story status, any deviations,
and follow-ups. No commit/push.
