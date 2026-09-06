# Reforger Manager — Sprint 7 stories

> Context + facts live in [PLAN.md](PLAN.md). `R#` = a report there, `§#` = a
> *Feature set* entry, `fact #n` = a *Code-level fact*. Line numbers verified
> 2026-09-06 — trust the symbol names if one drifted.

## Shape

Nine feature stories + a wrap-up. Two chains, plus standalones:

- **Mods-tab chain:** S1 → S7 (both edit `ModsPanel.tsx`).
- **RCON chain:** S5 → S8 → S9 → S10 (all edit `rcon/client.py` / `api/servers.py`
  / `PlayersPanel.tsx` in sequence). S10 is optional (migration).
- **Standalone, parallel:** S2, S3, S4.

| Story | Agent | Files | Depends on |
|---|---|---|---|
| S1 | native Claude (Sonnet 5) | `frontend/src/components/server/ModsPanel.tsx` (+ opt. 1 type in `frontend/src/lib/api.ts`) | — | **DONE** |
| S2 | opencode · glm | `frontend/src/pages/Servers.tsx`, `frontend/src/components/server/ServerRow.tsx`, `frontend/src/styles.css` | — |
| S3 | opencode · deepseek | `frontend/src/components/server/ServerForm.tsx` | — |
| S4 | native Claude (Sonnet 5) | `frontend/src/components/server/HistoryPanel.tsx` | — |
| S5 | native Claude (Sonnet 5) | `backend/app/rcon/client.py`, `backend/app/api/servers.py`, `backend/tests/test_rcon_client.py`, `frontend/src/components/server/PlayersPanel.tsx` | — |
| S7 | native Claude (Sonnet 5) | `frontend/src/components/server/ModsPanel.tsx`, `frontend/src/components/mods/SortableModList.tsx` | **S1** |
| S8 | native Claude (Sonnet 5) | `backend/app/rcon/client.py`, `backend/app/api/servers.py`, `backend/app/schemas/` (+ new `rcon.py`), `backend/tests/test_rcon_client.py` (+ new test file) | **S5** |
| S9 | native Claude (Sonnet 5) | `frontend/src/components/server/PlayersPanel.tsx` → `RconPanel.tsx`, `frontend/src/pages/ServerDetail.tsx`, `frontend/src/lib/api.ts` | **S5, S8** |
| S10 *(optional)* | native Claude (Sonnet 5) | `backend/app/models/server.py`, new Alembic migration, `backend/app/servers/config_gen.py`, `backend/app/schemas/server.py`, `frontend/src/components/server/RconPanel.tsx`, `backend/tests/` | **S8, S9** |
| S6 | native Claude — thin, after all | integration + `.sprints/7/RESULTS.md` | S1–S5, S7–S10 |

Gate per story: `cd frontend && npm run build` clean (FE stories) and
`cd backend && ./.venv/Scripts/python.exe -m pytest -q` green (BE stories;
regression for the rest). No commit/push. Don't touch `.sprints/` (S6 excepted),
models/migrations (S10 excepted), MCP.

---

## S1 — Mods tab: load modpack + button disambiguation · §1–§2, facts 1-3 — DONE

`ModsPanel.tsx` only (one optional exported type in `lib/api.ts`). Two
independent deliverables in one file. **S7 edits the same file next — leave the
assigned-mods `<section>` and the `preflight` query alone.**

### §1 — "Load modpack" dialog
1. Import `Modpack` from `../../lib/api` (type exists, `api.ts:58`). Add:
   ```ts
   type ApplyModpackResult = {
     applied: number;
     mode: "replace" | "append";
     dropped_pins: Array<{ mod_guid: string; mod_name: string | null }>;
   };
   ```
2. State by the `savePack` block (`:342-345`):
   `loadOpen`, `loadPackId` (`""`), `loadMode` (`"replace"|"append"`, default
   `"replace"`), `loadResult` (`ApplyModpackResult|null`), `loadError`
   (`string|null`).
3. `packs` query: `useQuery({ queryKey: ["modpacks"], queryFn: () =>
   api<Modpack[]>("/api/modpacks"), enabled: loadOpen })`.
4. `applyPack` mutation → `api<ApplyModpackResult>(
   \`/api/modpacks/${loadPackId}/apply/${id}\`, { method: "POST",
   body: JSON.stringify({ mode: loadMode }) })`.
   - `onSuccess(r)`: `setLoadResult(r)`, `setLoadError(null)`,
     `queryClient.invalidateQueries({ queryKey: ["server", id] })` +
     `["servers"]`, `void preflight.refetch()`. **No navigate.**
   - `onError(e)`: `setLoadResult(null)`; 409 (`e instanceof ApiError &&
     e.status === 409` — check `ApiError` is exported like `Modpacks.tsx:231`
     uses it; else substring-match the message) → "Stop the server before
     applying a modpack."; else `errText(e, "Apply failed")`.
5. Toolbar button after "Save as modpack" (`:388`):
   ```tsx
   <Button size="sm" variant="outline" disabled={dirty}
     title={dirty ? "Save or discard your staged changes first" : undefined}
     onClick={() => { setLoadResult(null); setLoadError(null);
       setLoadPackId(""); setLoadMode("replace"); setLoadOpen(true); }}>
     Load modpack…
   </Button>
   ```
6. Fourth `<Dialog>` beside the pin/pack ones (`:606-702`), mirroring
   `Modpacks.tsx:511-588`:
   - `title="Load a modpack into this server"`,
     `onClose={() => !applyPack.isPending && setLoadOpen(false)}`.
   - `packs.data?.length === 0` → body "No modpacks yet — create one with
     *Snapshot to modpack*." and no picker/apply.
   - `<select>` (tailwind classes from `Modpacks.tsx:519-524`),
     `value={loadPackId}`, first option `""` = "Select a modpack…", then
     `` `${p.name} (${p.items.length} mods)` `` per pack.
   - `<fieldset>` two radios `name="load-mode"`, wording verbatim from
     `Modpacks.tsx:546,555` ("Replace — clear the server's mod set, then write
     the pack" / "Append — add the pack's mods, keeping what is already there").
   - `text-xs text-stone-400` note: "Applies immediately and needs the server
     stopped. Replace drops version pins on mods the pack doesn't contain. All
     applied mods are set enabled."
   - `{loadError && <p className="error">{loadError}</p>}`.
   - `{loadResult && …}` — `Applied {loadResult.applied} mod{…}.` + if
     `dropped_pins.length` an amber line of `dp.mod_name ?? dp.mod_guid`
     (mirror `Modpacks.tsx:560-570`).
   - Footer: ghost button `loadResult ? "Done" : "Cancel"` → `setLoadOpen(false)`,
     `disabled={applyPack.isPending}`; primary `Apply modpack` →
     `applyPack.mutate()`, `disabled={applyPack.isPending || !loadPackId}`.

### §2 — disambiguation (pure copy / attribute)
- `:388` label `Save as modpack` → `Snapshot to modpack…`.
- `:655` dialog `title` → `Snapshot this mod set to a modpack`.
- `:659-662` body first sentence → "Creates a new reusable modpack from this
  definition's **last saved** mod set and load order." (keep the pins sentence).
- `:483-489` "Save mod set" button: add
  `title={dirty ? undefined : "No unsaved changes"}`. Label / `disabled` /
  `onClick` unchanged.

**Done when.** `npm run build` clean. Manual: "Load modpack…" disabled with
unsaved edits, otherwise applies a pack (append grows the list, replace swaps
it) without navigating away; the two pack buttons read "Snapshot to modpack… ·
Load modpack…".

**Watch out.**
- Reuse the existing `dirty` memo (fact 1) — don't recompute.
- After a successful apply the resync effect (`:160-166`) refreshes `working`
  *because it's clean* — that's why the button is `disabled={dirty}`. Don't add
  your own `setWorking`.
- Keep `packs` query `enabled: loadOpen`.
- Match the file's existing ellipsis style — it uses `...` ("Checking...",
  "Saving..."). Use `...` in your new strings too.
- Don't touch the `preflight` query options or the assigned-mods `<section>` —
  S7 owns those. If a merge is unavoidable, S7 rebases onto your result.

---

## S2 — /servers roster: column alignment + inline start/stop · §4, facts 4-5

`Servers.tsx` + `ServerRow.tsx` + `styles.css`.

**Deliver.**

1. **One grid for the whole roster.** In `styles.css` add a `.roster` (or reuse
   a Tailwind grid on the wrapper in `Servers.tsx:47`) with a single
   `grid-template-columns` covering: favourite star (fixed, e.g. `1.5rem`) ▸
   name+scenario (`minmax(0,1fr)`) ▸ mods (fixed, e.g. `5rem`, right-aligned) ▸
   status badge (fixed, e.g. `6rem`) ▸ actions (fixed, e.g. `7rem`). `ServerRow`
   returns its cells with `display: contents` on the wrapper (or `Servers.tsx`
   maps cells directly). Every row now shares column edges.
2. Keep the current row look: bottom border, hover colour, `.row-main`
   name/scenario stack, the star toggle, `{n} mods`, the Running/Standby
   `<Badge>`. Keep favourites-first sort (`Servers.tsx:13-16`).
3. **Start/Stop per row** — `<Button size="sm">` in the actions cell:
   - Label: `is_running ? "Stop" : "Start"`; `variant="outline"`.
   - `onClick`: `event.preventDefault(); event.stopPropagation();` (the row is a
     `<Link>`).
   - **Stop** → `api(\`/api/servers/${server.id}/stop\`, { method: "POST" })`.
   - **Start** → look at the other rows (pass the full `servers` list into
     `ServerRow`, or lift the control into `Servers.tsx`). If some other
     `other.is_running`: `if (!window.confirm(\`Stop '${other.name}' and start
     '${server.name}'?\`)) return;` then `await api(\`/api/servers/${other.id}/
     stop\`, …)` then `await api(\`/api/servers/${server.id}/start\`, …)`. Else
     just start.
   - `useMutation`; `onSettled` → `queryClient.invalidateQueries({ queryKey:
     ["servers"] })` + `["server", String(server.id)]`. Disable the button while
     pending.
   - On error show `err.message` — inline under the roster (lift error state to
     `Servers.tsx`) or minimally as the button `title` + a small `.error` line
     in the row. 409 messages from fact 5 are already human-readable.
4. Preserve the `@media (max-width:700px)` collapse intent (`styles.css:24`) —
   if you replace `.row` usage, add the equivalent narrow rules for `.roster`
   (hide the mods column, keep name + status + actions).

**Done when.** `npm run build` clean. Manual: every roster row's "mods" number,
status badge and Start/Stop button are vertically aligned regardless of name
length; Start on a standby row while another runs prompts to stop the other,
then starts; Stop works; errors surface.

**Watch out.**
- `ServerRow` is a `<Link>` — every button inside needs
  `preventDefault()+stopPropagation()` or it navigates.
- Don't regress the favourite-star toggle (`ServerRow.tsx:22-38`).
- If lifting start/stop to `Servers.tsx`, `ServerRow` needs the sibling list or
  an `onStart/onStop` callback prop — keep the prop surface small.

---

## S3 — remove the port-collision warning · §5, fact 6

`ServerForm.tsx` only.

**Deliver.**
1. Delete the `portCollisions` IIFE and its comment banner (`:580-603`).
2. Delete the render block (`:866-879`).
3. Delete the `allServers` query (`:582-585`).
4. If `Server` (the type) is now unused in the file, drop it from the import at
   `:1-…`; if still used, leave it. `tsc -b` will fail the build on an unused
   import/var — check.
5. Nothing else in the form changes — ports still save, `canSubmit` (`:418`) is
   untouched.

**Done when.** `npm run build` clean; the New/Edit server form no longer shows
a "Port collisions" box even when two definitions share a port.

**Watch out.**
- `keepPreviousData` / other `useQuery` imports may become unused if
  `allServers` was the only consumer — check the import at `:1`.

---

## S4 — History tab: time-series chart · §6, fact 7, 10

`HistoryPanel.tsx` only (no dependency; no backend change — the optional
`maxlen` bump is folded into S5).

**Deliver.**
1. Keep the "Current A2S sample" `<section>` (`:14-25`) as-is.
2. Replace the "Rolling history" `.list` (`:26-50`) with an inline `<svg>` line
   chart of `stats.data.history` (oldest→newest, fact 7):
   - X axis = sample position (or elapsed seconds if a timestamp field exists —
     inspect a sample; otherwise index). Y = `players` (0…`max_players` or
     0…max observed).
   - Primary polyline = player count. Secondary, faint polyline = `ping_ms`
     on its own scale (or omit if noisy — player count is the point).
   - Small labels: current / min / max player count; sample count + window.
   - Degrade cleanly: 0 samples → keep the existing
     `<Empty label="No A2S samples have been recorded." />`; 1 sample → a dot +
     the numeric read-out, no line.
   - `viewBox` + `preserveAspectRatio`, `width:100%`, fixed height (~160px),
     `overflow-x` not needed (it scales). Stroke colours from the existing
     palette (amber accent `#f59e0b`, stone grid). Respect `.dark` — use
     `currentColor` / tailwind `text-*` where possible.
3. Still `refetchInterval: 5000`; the chart just re-renders as `history` grows.

**Done when.** `npm run build` clean. Manual: History tab shows a line that
extends every ~5 s; with a player joined the line steps to 1; no more
per-second row spam.

**Watch out.**
- `history` samples are loosely typed (`Stats` — see `lib/api.ts`); coerce
  `Number(sample.players ?? 0)` and guard `NaN`.
- Don't assume a timestamp field — check `lib/api.ts` `Sample`/`Stats` and a
  real sample shape (`backend/app/api/servers.py` `asdict(info)` around
  `:143`); fall back to index-based X.
- Keep it one `<svg>`, hand-built — do **not** add recharts/d3.

---

## S5 — Players tab RCON fix (Reforger `#players` format) + raw diagnosis · §7, facts 8, 8a, 9

`backend/app/rcon/client.py` + `backend/app/api/servers.py` +
`backend/tests/test_rcon_client.py` + `frontend/src/components/server/PlayersPanel.tsx`.

The `#players` reply format is now known (PLAN *RCON `#players` format*, fact
8a): a `Players on server:` header, then rows `<decId> ; <uuid> ; <name>`, no
IP / ping / GUID. The current parser is built for the Arma-3 space-separated
line and matches nothing → the bug.

**Deliver.**

1. **Rewrite `parse_players`** (`client.py:248-273`) for the Reforger shape,
   keeping it a pure importable function:
   ```python
   _REFORGER_ROW = re.compile(
       r"^\s*#?(?P<id>\d+)\s*;\s*(?P<uid>[0-9A-Fa-f-]{8,})\s*;\s*(?P<name>.*)$"
   )
   _LEGACY_ROW = _PLAYER_RE   # keep the old space-separated regex as fallback
   _SKIP = re.compile(
       r"^\s*($|[-=_\s]+$|players on server:|processing command:|\(?\d+ players)",
       re.IGNORECASE,
   )
   ```
   - Iterate lines; `continue` on `_SKIP`.
   - Try `_REFORGER_ROW` first → `{"id": int(m["id"]), "name":
     m["name"].strip() or None, "uid": m["uid"], "ip": None, "ping": None,
     "raw": line}`.
   - Else try the legacy path (existing IP/ping extraction) so non-Reforger BE
     servers still parse; legacy rows get `"uid": None`.
   - `[]` when nothing matches is fine — the raw passthrough is the safety net.
2. **Raw passthrough (backend).**
   - `RconClient.players()` (`client.py:156-158`): return both the parsed list
     and the raw string — a small `@dataclass PlayersResult(players:
     list[dict], raw: str)`, or split into `players_raw() -> str` + let the
     route call `parse_players`. Keep `parse_players` standalone.
   - `/players` route (`get_server_players`, near `_rcon` `api/servers.py:148-
     162`): return `{"players": [...], "raw": "<text>"}`. The `/rcon` command
     route is unchanged.
   - Optional 1-liner in `api/servers.py`: `_STATS_HISTORY` `maxlen=60` →
     `maxlen=240` (`:74`, ~20-min window) — include only if it stays one line.
3. **Frontend (`PlayersPanel.tsx`).**
   - Query type → `api<{ players: Player[]; raw?: string }>(…)` (`:25-29`).
   - `Player` type (`lib/api.ts`): add optional `uid?: string | null`.
   - Table: the **IP** and **Ping** columns are always empty for Reforger —
     replace them with a single **Identity (UID)** column showing
     `player.uid ?? "—"` (`font-mono text-xs`, `break-all`); keep Player / ID /
     Actions. (If you'd rather not churn the table, at minimum stop rendering
     "not reported" for every row — show `—`.)
   - Empty branch (`:248-250`): if `players.data?.raw?.trim()`, render under the
     `<Empty>`:
     ```tsx
     <pre className="max-h-40 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs">
       {players.data.raw}
     </pre>
     ```
     caption (`text-xs text-stone-500`): "RCON replied but no player rows were
     parsed — paste this into `.sprints/7/PLAN.md` → *RCON #players format →
     live capture*."
   - kick/ban/#say/#restart untouched — `#kick`/`#ban` still use `player.id`.
4. **Tests (`test_rcon_client.py`).** Rewrite the player-parser test around the
   documented Reforger sample:
   ```
   Players on server:
   0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; underlines
   1 ; 9f8e7d6c-5b4a-3210-fedc-ba9876543210 ; Some Player
   ```
   → two records `{id:0, name:"underlines", uid:"1a2b…", ip:None, ping:None}`,
   `{id:1, name:"Some Player", uid:"9f8e…", …}`. Add: `processing command:
   players` + header + count lines are skipped; a legacy space-separated line
   still parses (`uid:None`) via the fallback; empty / `Unknown command` reply →
   `[]`. If the user drops a real capture into PLAN, add a test asserting it.

**Done when.** `pytest -q` green incl. the rewritten + new cases; `npm run
build` clean; `/api/servers/{id}/players` returns `raw` and `uid` per player;
the Players tab lists the joined player (was empty), and shows the raw text if a
future format change breaks parsing again.

**Watch out.**
- `parse_players` stays pure and importable — don't fold it into the client
  method; the tests import it directly.
- Don't change `_validate_command` (`#players` already allowed) or the command
  string sent (`#players` is correct per the BI wiki).
- `RconClient._collect_response` already reassembles BE multipart, so
  `parse_players` gets the whole text — no client-side buffering.
- Grep callers of the `/players` route / `_rcon` and the MCP
  `get_server_players` before changing the return shape; add `raw: str | None`
  (and player `uid`) to any response model / schema so `response_model`
  validation and the MCP tool description stay correct.
- Keep `raw` and `uid` optional on the FE types.
- **S8 and S9 build on these same files** — keep the `PlayersPanel.tsx` change
  to the query-type + UID column + raw `<pre>`; don't restructure the tab (S9
  does that, and renames the file). Leave `_validate_command` alone (S8 widens
  it).

---

## S7 — Mods tab: on-request pre-flight + linked titles + dependency expander · §3, §8, facts 1-3, 11-12

`ModsPanel.tsx` + `SortableModList.tsx`. **After S1 merges** (same file).

### §3 — pre-flight on request only + button style
- `preflight` query (`ModsPanel.tsx:119-122`): add `enabled: false`,
  `refetchOnMount: false`, `refetchOnWindowFocus: false`,
  `refetchOnReconnect: false`.
- Render (`:431-468`): add a leading branch — when
  `!preflight.isFetched && !preflight.isFetching` show a muted
  "Pre-flight has not run yet — press **Refresh pre-flight**." Keep the
  `isFetching`/`isError`/`data` branches; verdict `<Badge>` only when
  `preflight.data`.
- `:413` "Refresh pre-flight" button → `variant="outline"` (drop
  `variant="ghost"`); leave it last in the toolbar row.
- Keep the `preflight.refetch()` calls at `:255` and `:278` — a save / pin /
  modpack-apply still refreshes an already-run pre-flight.

### §8 — linked titles + dependency expander
1. `SortableModList.tsx` — two new optional props, both default to today's
   output (Modpacks list must be byte-identical when they're absent):
   - `renderName?: (item: T) => ReactNode` — replaces the text node at
     `:130-132` (keep the `truncate font-display …` wrapper; swap only the
     inner text).
   - `renderExpanded?: (item: T) => ReactNode` — rendered inside the `<li>`
     (`:105-140`) as a full-width block **after** the flex row
     (`renderActions` span), e.g. `{renderExpanded && <div className="w-full">
     {renderExpanded(item)}</div>}`.
2. `ModsPanel.tsx` assigned list (`:503-557`):
   - `renderName={(m) => <Link to={\`/mods/${m.mod_guid}\`} className="…
     hover:text-amber-400">{m.mod_name ?? m.mod_guid}</Link>}` — import `Link`
     from `react-router-dom` (`useNavigate` is already imported from it).
   - A caret toggle (`▸`/`▾`) in `renderActions` (or `renderMeta`) with local
     per-row open state — simplest: a small child component
     `<DepsToggle guid={m.mod_guid} />` that owns its `open` + the
     `useQuery(["mod", guid, "deps"], () => api<ModDeps>(\`/api/mods/${guid}\`))`
     (mirror `AddModRow`, `:707-…`), and renders both the caret and, when open,
     the subtree. Pass its rendered body through `renderExpanded`, or let the
     component render its own collapsible block and pass a no-op `renderExpanded`
     — pick whichever keeps `SortableModList` generic.
   - Subtree rows: `dependency_tree.nodes` with `depth > 0` (or all except the
     mod itself), sorted by `depth` then name. Resolved node → `<Link
     to={\`/mods/${n.guid}\`}>{n.name ?? n.guid}</Link>` + a muted `via` note;
     `state === "unresolved"` → plain `{n.guid}` + an "unresolved" `<Badge
     tone="warn">`. Empty tree → "No dependencies." Loading → "…". Error →
     "Dependency lookup failed."
3. `addModWithDeps` → rename to `addMod` and cut the dependency loop
   (`:197-226`): fetch nothing, just
   `setWorking((cur) => present.has(mod.guid) ? cur : [...cur,
   makeWorkingMod(mod.guid, mod.name)])`. Update its call site
   (`onAdd={() => void addMod(mod)}`, `:597`) and the comment (`:194-196`) to
   "Dependencies are resolved into the generated config at start time
   (config_gen closure) — the assigned list holds only explicit picks."
4. **Dedup render rule** (PLAN R8 point 4 + decision 10a). Compute a
   `covered: Set<guid>` from the working copy: for each explicit row, take its
   `dependency_tree.nodes` (the same `useQuery(["mod", guid, "deps"])` the
   `DepsToggle` already fetches — reuse/share the cache), and mark every *other*
   explicit row whose guid appears in that closure. Cycle-guard (visited set)
   like `resolve_dependencies`.
   - Feed `SortableModList` only the **uncovered** rows (`working.filter((m) =>
     !covered.has(m.mod_guid))`). Order/DnD/save still operate on the full
     `working` array — covered rows keep their slot, they're just not rendered
     at top level.
   - In each covering parent's `renderExpanded` subtree, render a covered row as
     a **pick-styled** entry: the `<Link to={\`/mods/${guid}\`}>`, its enable
     toggle, its pin badge — visually distinct from pure dependency nodes
     (which stay read-only text/links). If the covered row is `enabled === false`
     while the parent is enabled, add a `<Badge tone="warn">loaded as a
     dependency of {parent.mod_name}</Badge>` — the closure loads it regardless.
   - Removing (or disabling) a parent recomputes `covered`; a row that is no
     longer covered reappears in the top-level list, sorted by its retained
     `load_order`. Renumber `load_order` densely on save (`buildSavePayload` /
     wherever order is serialised) so a reappearing row lands sensibly.
   - A row may be covered by more than one parent → it shows in each parent's
     subtree; it returns to top level only when **no** covering parent remains.
   - No change to the save payload, `from-server`, or any backend file — the
     rule is view-only.
5. `AddModRow` (`:707-…`) keeps its own deps preview — leave it as is, or, if
   trivial, reuse the same `DepsToggle`. Not required.

**Done when.** `npm run build` clean. Manual: opening the Mods tab fires **no**
`/preflight` request; "Refresh pre-flight" (now bordered) runs it; a mod title
in the assigned list navigates to `/mods/<guid>`; the caret expands to that
mod's dependencies, each resolved one a link; adding a mod with dependencies
adds **one** row, not the whole tree; "Snapshot to modpack" on that server
captures only the explicit picks. Dedup: explicitly add mod `X`, then add parent
`P` that depends on `X` → `X` disappears from the top-level list and shows
pick-styled inside `P`'s caret subtree; remove `P` → `X` reappears at top level
at its old position; a snapshot taken while `X` is nested still lists both
guids, and re-applying it to a fresh server nests `X` under `P` there too.

**Watch out.**
- `SortableModList` is also consumed by the Modpacks page — with the new props
  omitted its output must not change. Verify that call site still builds and
  looks the same.
- The drag handle is the only drag surface (`SortableModList.tsx:114-125`) and
  `PointerSensor` needs 4px of movement — a `<Link>` in the name is safe, but
  still give it `onClick={(e) => e.stopPropagation()}` for belt-and-braces.
- A dependency GUID may not be in the local library → its `/mods/{guid}` detail
  page can 404. Link it anyway (ModDetail shows its own error) **only** when
  `state !== "unresolved"`; otherwise plain text.
- Don't change the save payload (`:235-257`), `from-server`, or any backend
  file. §8 is view + add-behaviour only.
- Keep `ModDeps` type (`:32-33`) — reuse it for the `DepsToggle` query.
- Dedup is **filter-on-render, never delete**. A covered row stays in `working`
  and in the save payload; only the top-level `SortableModList` input is
  filtered. Deleting the row would drop its `enabled` / pin state and (if it
  ever stopped being covered) silently change what the server loads.
- `covered` must be recomputed on every `working` change (add/remove/toggle/
  reorder) and on every dep-query settling — memo on `[working, depTrees]`.
- Cycle-guard the closure walk (`A → B → A` metadata exists in the wild) and
  tolerate a dep query still loading (treat unknown closure as "covers
  nothing" until it resolves, then re-filter).
- Don't hide a covered row's controls — a nested pick keeps its enable toggle
  and pin badge; only its *position* moves.
- `dependency_tree` from `GET /api/mods/{guid}` may be stale vs. a just-updated
  mod; that's acceptable (PLAN decision 10a-e), don't try to force-refresh it
  here.

---

## S8 — RCON command surface + bans API · §10, facts 8a, 14

`backend/app/rcon/client.py` + `backend/app/api/servers.py` +
`backend/app/schemas/` + `backend/tests/`. **After S5** (same client/route
files; build on its rewritten `parse_players`).

**Deliver.**

1. `_validate_command` (`rcon/client.py:232-240`) — add, with tight regexes
   (spec §12–15):
   - `re.fullmatch(r"#ban create \S+ \d+( .+)?", cmd)` — identifier is one
     token (playerId / identityId / name-without-spaces; multi-word names go
     through the raw route);
   - `re.fullmatch(r"#ban remove \S+", cmd)`;
   - `re.fullmatch(r"#ban list( \d+)?", cmd)`;
   - `cmd == "@logout"`.
   Keep everything else as-is; `#say` stays allowed.
2. `parse_bans(text) -> list[dict]` (next to `parse_players`): skip
   header/echo/blank/`\d+ bans` lines; row regex
   `^\s*(?P<ban_id>\S+)\s*;\s*(?P<uid>\S+)\s*;\s*(?P<duration>.+?)\s*$` →
   `{"ban_id":…, "uid":…, "duration":…, "raw":line}`. Pure, importable.
3. `RconClient`: `async def bans(self, page: int = 1)` →
   `(parse_bans(txt), txt)`; `ban_create(identifier, seconds, reason=None)` and
   `ban_remove(identity_id)` → return the command echo string. In
   `__aexit__` / `close`, best-effort `await self.command("@logout")` before
   the socket closes (swallow any error; skip if not authenticated).
4. Routes in `api/servers.py`, each `dependencies=authed`, each through the
   `_rcon` guard (active server + `rcon_enabled` + honour `rcon_permission` —
   a `monitor` definition may only `#players` / `@logout`, enforce here):
   - `GET /servers/{id}/bans?page=1` → `{"bans":[…], "raw":str, "page":int}`.
   - `POST /servers/{id}/bans` body `{identifier:str, duration_seconds:int>=0,
     reason:str|None}` → runs `#ban create …`, returns `{"echo":str}`; 422 on
     a bad identifier / negative duration.
   - `DELETE /servers/{id}/bans/{identity_id}` → `#ban remove …`, 204.
   - `POST /servers/{id}/rcon` — add optional `?raw=1` (or add
     `POST /servers/{id}/rcon/raw`): skips `_validate_command`, still `authed` +
     active-server + `rcon_permission`. Cap length (e.g. 512) and strip control
     chars.
   New request/response models in a new `backend/app/schemas/rcon.py` (or
   extend `schemas/server.py`); wire `response_model=`.
5. Tests:
   - `test_rcon_client.py`: `_validate_command` accepts the 4 new forms and
     still rejects `#ban wipe`, `#ban create x -1`, `rm -rf`, etc.; `parse_bans`
     on a `BanID ; UID ; Duration` sample incl. header/echo skipping and `[]`
     on empty.
   - new `test_rcon_bans_api.py` (mirror existing route tests): happy path for
     the three `/bans` routes + `?raw=1` with a stubbed `RconClient`
     (monkeypatch `command`); 400 when RCON disabled, 409 when not the running
     server, `monitor`-permission rejection.

**Done when.** `pytest -q` green incl. new cases; the three `/bans` routes and
the raw passthrough respond against a stubbed client; `@logout` is sent on
client close.

**Watch out.**
- `parse_bans` / `parse_players` stay standalone pure functions.
- Don't loosen `#kick` / `#ban <digits>` (S5/existing) — the new `#ban create`
  regex is additive.
- `?raw=1` must still refuse a `monitor` server everything but `#players` /
  `@logout` — do the permission check server-side, never from a client flag.
- `#ban create` with a **playerName containing spaces** won't pass the
  whitelist regex — that's intentional; the UI sends multi-word identifiers via
  the raw route or by identityId. Note it in the route docstring.
- Grep for existing `/rcon` route tests and the MCP `send_rcon_command` tool —
  keep them green; the `?raw` param is opt-in so default behaviour is unchanged.

---

## S9 — RCON tab UI · §9, facts 9, 13, 16

`frontend/src/components/server/PlayersPanel.tsx` → **rename to `RconPanel.tsx`**
+ `frontend/src/pages/ServerDetail.tsx` + `frontend/src/lib/api.ts`.
**After S5 and S8** (needs `uid` + the `/bans` + raw endpoints).

**Deliver.**

1. **Rename** `PlayersPanel.tsx` → `RconPanel.tsx`, component `PlayersPanel` →
   `RconPanel`. In `ServerDetail.tsx`: import, the tabs array entry
   `"Players"` → `"RCON"` (`:131-142`), and the panel line
   `{tab === "RCON" && <RconPanel id={id} />}` (`:150`). Grep for any other
   `PlayersPanel` / `"Players"` tab reference.
2. **Players section** — keep the 5 s `players` query. Table columns: Name,
   `playerId`, **Identity (UID)** (`font-mono text-xs break-all` + a copy
   button), **Status** (badges: `Banned` if `uid` ∈ the `/bans` list this tab
   also loads; `Admin` if `uid` ∈ `server.game_admins` — omit if S10 deferred),
   Actions. Actions: **Kick** (`#kick <id>`, confirm), **Ban…** (opens a small
   form — preset 1h / 24h / permanent + custom seconds, optional reason →
   `POST /servers/{id}/bans` with `identifier: String(playerId)`), **Copy UID**,
   **Make admin** / **Remove admin** (S10). Keep the empty-state raw `<pre>`
   from S5.
3. **Bans section** — `useQuery(["server-bans", id, page])` →
   `GET /servers/{id}/bans?page=`. Table: BanID, UID (mono, copy), Duration,
   Remove (`DELETE …/bans/{uid}`, confirm). Prev/Next page buttons (25/page).
   An **offline ban** form above it: identifier (UID or name) + duration + reason
   → same `POST …/bans`.
4. **Server control section** — move the existing Restart / Shutdown buttons and
   the whole scheduled-restart widget (`:91-183`) here verbatim.
5. **Raw command** — a `<details>` (collapsed): `<input>` + Send →
   `POST /servers/{id}/rcon?raw=1`, show `response` in the existing `<pre>`.
   Move the `#say` box here, relabel "Broadcast (#say — best-effort, not a
   vanilla RCON command)".
6. `lib/api.ts`: `Player.uid?: string | null`; new `Ban` type
   `{ban_id:string; uid:string; duration:string}`; `BansResponse
   {bans:Ban[]; raw?:string; page:number}`.

**Done when.** `npm run build` clean; the tab is labelled **RCON**; joined
players show with UID + copy; Kick / Ban / Copy work; the Bans section lists,
pages, and removes; the raw box runs an arbitrary command; server-control and
scheduled-restart behave exactly as before.

**Watch out.**
- Pure rename — update every import/reference; `git grep PlayersPanel` must come
  back empty afterwards.
- Don't lose any existing behaviour when relocating the restart/schedule
  widgets — they're the most complex part of the old file.
- Badges depend on two queries (`players` + `bans`); render players fine even if
  `bans` errors (show the section error, not a blank tab).
- `#kick`/`#ban` still use `player.id` (session playerId), not `uid`.
- Confirm dialogs via `window.confirm` (codebase pattern).

---

## S10 — persistent server admin list *(optional; migration)* · §11, fact 15

`backend/app/models/server.py` + new Alembic migration +
`backend/app/servers/config_gen.py` + `backend/app/schemas/server.py` +
`frontend/src/components/server/RconPanel.tsx` + `backend/tests/`.
**After S8 and S9.** Skip this story to hold the sprint's no-migration line —
S9 already ships without the `Admin` badge / `Make admin` button in that case.

**Deliver.**
1. `Server.game_admins: Mapped[list[str] | None] = mapped_column(JSONVariant)`
   (match the `game_properties` / `extra_config` column style in the model).
2. Alembic revision: `add_column("servers", Column("game_admins", <JSON>,
   nullable=True))` + a downgrade that drops it. Follow the existing migration
   naming / `down_revision` chain; note `DB_MIGRATE_ON_STARTUP=create_all` is the
   default so also confirm `create_all` produces the column on a fresh DB.
3. `config_gen.build_config` (`:79-89`): add
   `"admins": list(server.game_admins or [])` inside the `game` dict.
4. `schemas/server.py`: `game_admins: list[str] | None = None` on
   `ServerCreate` and `ServerUpdate`; include in `ServerOut`.
5. `RconPanel.tsx`: **Make admin** adds `player.uid` to `game_admins` via
   `PATCH /servers/{id}` (send only that field, like the favourite toggle);
   **Remove admin** removes it. Toast/inline note "applies on next server
   restart". `Admin` badge = `server.game_admins?.includes(player.uid)`.
6. Tests: `config_gen` emits `game.admins`; round-trip `game_admins` through
   `POST` / `PATCH` / `GET`; migration upgrade+downgrade runs.

**Done when.** `pytest -q` green; a fresh DB and a migrated DB both have
`servers.game_admins`; generated config carries `game.admins`; the UI toggles it.

**Watch out.**
- This is the sprint's **only** migration — call it out in `RESULTS.md`.
- `game.admins` takes **identity ids**, not playerIds — store `player.uid`.
- Don't touch `passwordAdmin` / `rcon_permission` handling.
- A hand-edited `extra_config.game.admins` would deep-merge over this
  (`config_gen:104-105`) — acceptable, mention it.

---

## S6 — integration + RESULTS.md (native, thin, after S1–S5, S7–S10)

1. `cd frontend && npm run build` — clean (`tsc -b` covers S1's generics, S4's
   SVG typing, S3's import pruning, S5's widened query type, S7's
   `SortableModList` prop generics, S9's new `Ban`/`BansResponse` types +
   `RconPanel` rename).
2. `cd backend && ./.venv/Scripts/python.exe -m pytest -q` — full suite green
   incl. S5's rewritten `parse_players`, S8's `parse_bans` / `_validate_command`
   / `/bans` route tests, and S10's migration + `config_gen` tests (if taken).
3. Consistency read: S1+S7 coexist in `ModsPanel.tsx` (no key clashes, toolbar
   still `flex-wrap`s); S7's `SortableModList` props don't alter the Modpacks
   page; S7's dedup filters the top-level list only — the save payload / snapshot
   still carry every explicit pick, and a covered row reappears when its parent
   is removed; S2's roster grid and the mobile media query agree; S3 left no unused
   imports; S4 added no dependency (`git diff frontend/package.json` empty);
   `git grep PlayersPanel` is empty after S9; the S5→S8→S9(→S10) chain landed in
   order and `/players` + `/bans` + `/rcon?raw=1` response shapes match their
   `response_model`s and the MCP tool descriptions; if S10 was skipped, S9 has
   no dangling `game_admins` / `Admin`-badge references.
4. Write `.sprints/7/RESULTS.md` (shape of `.sprints/6/RESULTS.md`): status;
   story table (S1–S5, S7–S10 + agents; mark S10 taken/deferred); report→fix
   map (R1–R9); UI changes (load-modpack dialog, button renames, on-request
   pre-flight + button style, roster alignment + inline start/stop,
   port-collision box removed, history chart, **Players→RCON tab**: Reforger
   `#players` parser + UID column + Kick/Ban/Copy + Bans section + Server
   control + raw command [+ Make admin if S10], linked mod titles + dependency
   expander + explicit-picks-only add + dedup of an explicit pick that is also
   another pick's dependency: hidden from the top-level list, shown pick-styled
   under the covering parent, restored on parent removal); tests added; note the `#players` /
   `#ban list` parsers were built to the *documented* format
   (`docs/rcon-reforger-spec.md` + PLAN sources) — a live capture is still worth
   pasting into PLAN; whether S10's migration shipped; "not exercised" list
   (Docker, live stack, real Workshop, real RCON server, browser); date
   2026-09-06.

**Done when.** Both commands green; `RESULTS.md` written. No commit.
