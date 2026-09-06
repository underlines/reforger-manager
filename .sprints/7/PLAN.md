# Reforger Manager — Sprint 7 plan

## Context

A batch of UX/behaviour reports from the live stack (2026-09-06), plus one
functional bug. All small, mostly frontend; one backend fix (RCON player list).

### R1 — the two "save" buttons on the Mods tab

`/servers/13` → **Mods** tab. Verified in
`frontend/src/components/server/ModsPanel.tsx`:

| Button | Where | Action | Why greyed |
|---|---|---|---|
| **"Save as modpack"** | top toolbar, `:380-389` | opens a dialog → `POST /api/modpacks/from-server/{id}` `{name,description}` → creates a **new reusable pack** from the server's *last-saved* mod set; invalidates `["modpacks"]`; navigates to `/modpacks` | — (worked for the user) |
| **"Save mod set"** | Assigned-mods header, `:483-489` | `PATCH /api/servers/{id}` `{mods:[…]}` → **persists the staged working-copy edits** (add/remove/reorder/enable) to *this* definition | `disabled={!dirty || save.isPending}` — purely "no unsaved changes" |

Not the same function. The near-identical wording is the whole problem.

### R2 — load a modpack from the Mods tab

Already a backend capability: `POST /api/modpacks/{pack_id}/apply/{server_id}`
with `body.mode ∈ {"replace","append"}` (`backend/app/api/modpacks.py:170-253`,
schema `backend/app/schemas/modpack.py:74-75`). The `/modpacks` "Apply to…"
dialog (`frontend/src/pages/Modpacks.tsx:196-253,510-588`) already drives it.
This sprint calls the same endpoint from the Mods tab — frontend only.

- `replace` — clears the set, writes the pack in load order; surviving GUIDs
  keep `pinned_*`; pins on dropped GUIDs are lost and returned in
  `dropped_pins`.
- `append` — adds only pack GUIDs not already assigned, numbered from
  `max(load_order)+1`; existing rows/order/pins untouched; `dropped_pins` empty.
- `409` if the target server is running.
- Response `ModpackApplyOut{applied:int, mode, dropped_pins:[{mod_guid,mod_name}]}`.

### R3 — pre-flight should be on-request, and the button looks wrong

`ModsPanel.tsx:119-122` runs the pre-flight query on mount (and on every
tab-open / refocus, React Query defaults). The user wants it to run **only** on
"Refresh pre-flight". That button (`:413`) is `variant="ghost"` — no border, so
it doesn't match the bordered `variant="outline"` "Save as modpack" button next
to it.

### R4 — /servers "Deployment Roster" is misaligned, and needs inline start/stop

`frontend/src/pages/Servers.tsx` renders one `<ServerRow>` per server inside a
`.list`. Each `ServerRow` (`ServerRow.tsx`) is its **own** `.row` CSS grid —
`grid-template-columns: minmax(0,1fr) auto auto` (`styles.css:23`). Because
`auto` columns size to each row's own content, the "N mods" column and the
Running/Standby badge **don't line up between rows** — that's the "different
column lengths" the user sees. Fix: one shared column grid for the whole
roster. Also: add a Start/Stop button per row (single-server rule still holds —
starting B while A runs → `SingleServerError` → 409 at
`backend/app/api/servers.py:470-478`; the button offers to stop the other
first).

### R5 — the per-server "Port collisions" warning is noise

`ServerForm.tsx:580-603` (logic) + `:866-879` (render) + the `allServers` query
(`:582-585`) warn when another definition reuses `bind_port` / `a2s_port` /
`rcon_port`. The warning itself admits "Not a runtime conflict (only one server
runs at a time)". Given the single-server rule, reusing ports across
definitions is normal and expected — remove the warning entirely.

### R6 — History tab: rolling per-sample list → chart

`HistoryPanel.tsx` refetches `/api/servers/{id}/stats` every 5 s
(`:10`) and renders `stats.data.history` as a reversed `.list` of one `.row`
per A2S sample. Backend keeps `_STATS_HISTORY = deque(maxlen=60)` and appends a
sample **on each GET** (`backend/app/api/servers.py:74,144-145`) — so ~1 sample
/ 5 s, a ~5-minute window. A time-series **chart** (players over time, + ping)
reads far better than a scrolling list of near-identical rows.

### R7 — Players tab shows nothing while History shows the player (BUG)

User had RCON enabled (port 19999, password set, permission `admin`), joined
the server, saw themselves in the **History** tab (A2S, independent of RCON) —
but the **Players** tab showed *"No players reported by RCON."*

That exact string is `PlayersPanel.tsx:249`, the branch for
`players.data?.players.length === 0` **on a successful request** (an *error*
would render the `:199-202` "Players are unavailable" text instead). So RCON
connected and returned a `#players` response, and `parse_players()`
(`backend/app/rcon/client.py:248-273`) parsed **zero** rows from it.

**Root cause (web-researched 2026-09-06 — see *RCON `#players` format* below).**
Arma Reforger's `#players` reply is **semicolon-delimited**, three fields:
`<decimalId> ; <identity-UUID> ; <name>`, under a `Players on server:` header.
It carries **no IP, no ping, no BattlEye GUID** (Reforger RCON exposes none of
them). The current parser is built for the Arma-3-style *space*-separated line:
`_PLAYER_RE = r"^\s*#?(?P<id>\d+)\s+(?P<body>.+?)\s*$"` needs whitespace right
after the id and never splits on `;`, and `_IP_RE` / `_PING_RE` look for fields
that aren't there (and would mis-read a name ending in digits as a ping). On the
real reply it matches nothing → zero players. Fix = parse the documented
Reforger shape.

### R8 — Mods tab assigned list: link titles, expandable dependencies, stop storing dep rows

`ModsPanel.tsx` renders the assigned mods through `SortableModList`
(`frontend/src/components/mods/SortableModList.tsx`), which prints the mod name
as plain text (`:129-133`) — no link. Three asks:

1. The mod title should link to `/mods/{guid}` (route `app.tsx:27`,
   `/mods/:guid` → `ModDetailPage`).
2. A per-row expand caret that reveals that mod's **dependency subtree**,
   read-only, each resolved dependency linking to its own detail page. The data
   is already available: `GET /api/mods/{guid}` returns
   `dependency_tree.nodes` (`DepNode{guid,name,via,state,depth}`), and
   `AddModRow` at the bottom of `ModsPanel.tsx` (`:707-…`) already does exactly
   this lazy-fetch-and-expand for the *add* list — reuse that pattern.
3. Dependencies should **not** be stored as their own assigned rows (and so
   never land in a "Snapshot to modpack"). Today `addModWithDeps`
   (`ModsPanel.tsx:194-226`) materialises every resolved dependency as a
   sibling row. This is redundant: `config_gen` builds the **full dependency
   closure itself** at start time from the explicit `server.mods` roots —
   `backend/app/servers/config_gen.py:147-178`
   (`resolve_dependencies(session, roots, use_api=False, use_disk=True)`,
   post-order DFS, closure emitted into `config.json`). So dropping the
   auto-added dep rows changes nothing about what the running server loads;
   it just makes the assigned list (and modpacks) reflect only the operator's
   explicit picks, with dependencies visible on demand via the caret.
4. **Dedup an explicit pick that is also another pick's dependency.** If the
   operator has explicitly added mod `X`, and later adds parent `P` whose
   dependency closure contains `X`, the assigned list should stop showing `X`
   as its own top-level row and instead show it **nested under `P`** — but
   styled as a real pick (keeps its enable + version-pin controls), not greyed
   like a pure dependency. `X` is **not deleted** from `server_mods` — it is
   only visually nested. Remove `P` (or any covering parent) and `X` returns to
   the top-level list automatically once no remaining explicit row's closure
   covers it. This is a **pure render rule** over the existing explicit rows +
   live `dependency_tree` metadata — no second row, no new column, no schema
   change. `server_mods` already has `UNIQUE(server_id, mod_guid)`, so "`X`
   exists internally as both a standalone row and a dependency row" is neither
   possible nor needed; dependency membership is always derivable and never
   persisted. Rule: an explicit row is hidden from the top-level list iff its
   guid is in **any other** explicit row's dependency closure (cycle-guarded
   like the resolver); in the read-only subtree it may appear under more than
   one covering parent. Edge cases + defaults are decision 10 below.

### R9 — grow the Players tab into a full RCON tab

With R7 fixing the player list, the user wants the tab to become the server's
RCON console: the player roster with per-player actions and identifiers, ban
management, server control, and a raw-command escape hatch.

`docs/rcon-reforger-spec.md` is the authoritative wire + command reference. The
**complete vanilla RCON command surface** is:

| Command | Effect | admin / monitor |
|---|---|---|
| `#players` | list players + `playerId` | yes / yes |
| `#kick <playerId>` | disconnect (can rejoin) | yes / no |
| `#ban create <playerId\|identityId\|playerName> <seconds> [reason]` | ban; `0` = permanent; `playerId` needs the player connected | yes / no |
| `#ban remove <identityId>` | unban | yes / no |
| `#ban list [page]` | rows `BanID ; Player UID ; Duration`, 25/page | yes / no |
| `#restart` | restart scenario, clients stay | yes / no |
| `#shutdown` | kill the server process | yes / no |
| `@logout` | de-auth this RCON client, free its slot | yes / yes |

Notes that shape the UI:
- **No "make admin" RCON command exists.** Admin is granted via the server
  config's `game.admins` (a list of identity ids) or in-game `#login` / voting.
  So a persistent "Make admin" button must write the player's `identityId` into
  the server *definition* and take effect on the next restart — that is the
  optional §11 below (it needs a model column + migration).
- `#say` / broadcast is **not** in the vanilla surface (Arma-3 legacy, "likely
  silently dropped"). The current tab has a `#say` box — keep it but label it
  best-effort, or fold it into the raw console.
- `#login` / `#logout` / `#roles` / `#id` are **in-game only**, not RCON.
- `playerId` (session) and `identityId` (the UUID from `#players`) are
  different — bans by `playerId` only work while connected; offline bans need
  the `identityId`.
- The manager opens a short-lived `RconClient` per request; sending `@logout`
  on close is tidier than relying on the 45 s idle timeout (spec §8, §10).

Current tab (`PlayersPanel.tsx`): player table (kick/ban per row), `#say` box,
Restart / Shutdown buttons, and the scheduled-restart widget
(`/servers/{id}/schedule-restart`). RCON plumbing: `_rcon` guard
(`api/servers.py:148-162`), `RconClient` (`rcon/client.py`),
`_validate_command` (`:232-240`) currently whitelists only `#players`,
`#restart`, `#shutdown`, `#kick N`, `#ban N`, `#say …` — it rejects
`#ban create/remove/list` and `@logout`.

## Goal

Nine reports (R1–R9). R1–R8 are small, mostly independent, no schema change.
R9 grows the Players tab into a full **RCON tab** — that one adds RCON commands
(bans, raw), new `/bans` routes, and (only if the optional admin-list piece is
taken) one migration. Every feature verified by `npm run build` (frontend) and
`pytest -q` (backend).

## Decisions locked

| # | Decision |
|---|---|
| 1 | R2 reuses `POST /api/modpacks/{pack_id}/apply/{server_id}` verbatim — no backend edits, no new route, no schema change. Two modes worded exactly as the `/modpacks` dialog (`replace` default). |
| 2 | R2 apply **persists immediately** (like this tab's inline pin/unpin, `ModsPanel.tsx:285-301`), then invalidates `["server",id]` + `["servers"]` + `preflight.refetch()`. The "Load modpack…" button is disabled while the working copy is `dirty` (tooltip "Save or discard your staged changes first"). |
| 3 | R1: rename **"Save as modpack"** → **"Snapshot to modpack…"**; reword the from-server dialog to say "snapshot … last saved"; add `title="No unsaved changes"` to the disabled "Save mod set" button. No behaviour change. |
| 4 | R3: the pre-flight query gets `enabled: false` + `refetchOnMount/WindowFocus/Reconnect: false`; it runs **only** via `preflight.refetch()` — on "Refresh pre-flight" and after a save / pin / modpack-apply (the existing `refetch()` call sites, `:255,278`). Initial state shows "Pre-flight not run yet — press Refresh". The button becomes `variant="outline"` to match its neighbours; keep it last in the toolbar row. |
| 5 | R4 alignment: the roster becomes **one** CSS grid. `Servers.tsx` wraps the rows in a container with a fixed `grid-template-columns` (star ▸ name/scenario ▸ mods ▸ status ▸ actions); `ServerRow` renders `display: contents` cells into it (or the roster maps plain cells itself). Column widths identical for every row. Keep the existing `.row` look (border-bottom, hover) and the `@media (max-width:700px)` collapse. |
| 6 | R4 start/stop: a Start/Stop button per row. **Stop** → `POST /{id}/stop`. **Start** → if another row has `is_running`, `window.confirm("Stop '<other>' and start '<this>'?")`; on OK `POST /{other}/stop` then `POST /{id}/start`; else straight `POST /{id}/start`. On any `409` show the server's message inline. Invalidate `["servers"]` after. `window.confirm` is the established pattern in this codebase (`PlayersPanel.tsx:78,85`). |
| 7 | R5: delete the port-collision block and its `allServers` query outright. No replacement, no setting. |
| 8 | R6: render the history as an inline **SVG** line chart (no new npm dependency — matches the project's minimal-deps stance). X = sample index/time, Y = player count, with a faint secondary ping line. Keep the "Current A2S sample" `<pre>` above it. Empty state unchanged. Optionally bump `_STATS_HISTORY` `maxlen` 60 → 240 (backend one-liner, ~20 min window) — include only if trivial. |
| 9 | R7 fix (format now known — no guessing): rewrite `parse_players` for the Reforger shape. Skip lines up to and including `Players on server:` (case-insensitive) and any `processing command:` / separator / count line; per player row match `^\s*#?(?P<id>\d+)\s*;\s*(?P<uid>[0-9A-Fa-f-]{8,})\s*;\s*(?P<name>.*)$` → `{id:int, name:name.strip() or None, uid:uid, ip:None, ping:None, raw:line}`. Add `uid` as a new field (the BI identity UUID — useful, and what offline `#ban create <identityId>` wants). Keep the old space-separated branch as a fallback for non-Reforger BE servers. Also thread the raw text out: `/api/servers/{id}/players` returns `{players, raw}`; `PlayersPanel` shows `raw` in a `<pre>` when `players` is empty. Keep the `## RCON #players sample` block below updated with any real capture the user provides; a regression test in `test_rcon_client.py` uses the documented sample. |
| 10 | R8: mod title in the assigned list → `<Link to={\`/mods/${guid}\`}>`. Add a per-row expand caret → lazy `GET /api/mods/{guid}` → dependency subtree read-only (resolved deps link to their detail page; unresolved shown as plain text). Change `addModWithDeps` → add **only the selected mod** as a row (no dep rows); `config_gen` already resolves the closure at start (fact 12). Pre-existing dep rows on saved servers are left alone — no migration, operator can remove them by hand. `SortableModList` gains optional `renderName` + `renderExpanded` props (backward-compatible; Modpacks list keeps its current look). |
| 10a | R8 dedup / dependency-resolution rule (R8 point 4). **Pure render rule, no migration.** An explicit assigned row is *hidden* from the top-level sortable list when its guid is in **any other** explicit row's dependency closure (built from live `dependency_tree`, cycle-guarded like `resolve_dependencies`). A hidden row renders inside each covering parent's caret subtree, styled as a **real pick** (enable toggle + pin controls shown), visually distinct from pure (never-a-row) dependencies. It is never deleted from `server_mods`; removing every covering parent returns it to the top-level list. Edge-case defaults: **(a)** hidden rows stay `config_gen` roots, so their `enabled=false` / version pin still take effect while nested — never "collapse" by deleting the row. **(b)** a hidden row with `enabled=false` under an enabled parent is contradictory (the parent's closure loads it anyway) → show a `warn` badge "loaded as a dependency of \<parent>"; don't imply the toggle won. **(c)** a reappearing row sorts by its retained `load_order`, clamped into range; renumber densely on every save. **(d)** "covered" = in *any* other explicit closure; in the subtree the row may appear under multiple parents; each parent removal recomputes coverage. **(e)** the hidden set is a projection of current metadata, not persisted — a mod update that drops a dependency makes the row reappear with no operator action; acceptable. **(f)** the rule must run at *every* list display site (assigned list, modpack detail, apply-modpack preview, preflight) or the same set looks deduped in one place and doubled in another. Modpack snapshot stays a flat `{mod_guid, load_order}` list (fact: `modpack_items` has no enabled/pin/flag columns) — the receiving server's render rule collapses duplicates on display; carrying the explicit-vs-dependency distinction into modpacks is a **non-goal** (would need a second migration). If a *persisted* explicit-vs-dependency flag on rows is ever wanted, that is a `server_mods.explicit` migration and §8 becomes migration-bearing like S10 — out of scope here. |
| 11 | R9: rename `PlayersPanel.tsx` → `RconPanel.tsx`, tab label **RCON** (`ServerDetail.tsx:131-142,150`). Sections: **Players** (roster + per-row actions), **Bans** (`#ban list` table + remove + offline-ban form), **Server control** (Restart / Shutdown / scheduled restart — move the existing widgets here), **Raw command** (free-text → `command(text)`, collapsed by default). `#say` box stays under Raw/Server-control, relabelled "broadcast (best-effort — not vanilla RCON)". |
| 12 | R9 backend: widen `_validate_command` to also allow `#ban create <id> <seconds> [reason]`, `#ban remove <id>`, `#ban list [page]`, `@logout` (tight regexes, per `docs/rcon-reforger-spec.md`). Add `parse_bans()` (`BanID ; UID ; Duration`, same `;` style as players) + `RconClient.bans(page)` / `ban_create(...)` / `ban_remove(id)`. New routes: `GET /servers/{id}/bans?page=`, `POST /servers/{id}/bans` `{identifier, duration_seconds, reason?}`, `DELETE /servers/{id}/bans/{identity_id}` — all behind the existing `_rcon` guard. A **raw** passthrough: `POST /servers/{id}/rcon` already exists; add an opt-in `?raw=1` (or a separate `/rcon/raw`) that skips the whitelist so mod commands work — still gated by auth + active-server + `rcon_permission`. |
| 13 | R9 "Make admin" — **optional, migration-bearing (§11).** Add `game_admins: list[str]` JSON column to `Server` + Alembic migration + `config_gen` emits `"admins": server.game_admins or []` under `game` + schema fields. "Make admin" / "Remove admin" buttons `PATCH /servers/{id}` that list, with a toast "applies on next server restart". If the sprint wants to hold the line on *no migrations*, defer §11 to a follow-up sprint and ship R9 without persistent admin (kick/ban/bans/raw still land). |
| 14 | No commit / push. No `.sprints/` edits by story agents (S6 writes `RESULTS.md`). No MCP tool changes. One migration **only** if §11 is taken — otherwise no models/migrations. |

## Code-level facts (verified 2026-09-06)

1. `ModsPanel.tsx` working-copy: `working`/`baseline` (`:150-151`),
   `dirty=!modsEqual(...)` (`:153`), `dirtyRef` (`:154-155`), resync effect
   `:160-166` (skips while dirty). Inline pin/unpin persist immediately and
   `preflight.refetch()` (`:265-301`).
2. `preflight` query `ModsPanel.tsx:119-122` — no `enabled`/`refetch*` opts, so
   it fires on mount. `refetch()` called at `:255` (after save), `:278` (after
   pin), and the button `:413`. Render branches `:431-468`
   (`isLoading`/`isError`/`data`).
3. Top toolbar `ModsPanel.tsx:379-416`: "Save as modpack" (outline),
   "Check updates" (outline), "Apply updates" (solid), "Refresh pre-flight"
   (**ghost** — the odd one).
4. `Servers.tsx` — `.list` wrapping `servers.map(s => <ServerRow>)` (`:47-51`);
   favourites sorted first (`:13-16`). `ServerRow.tsx` — a `<Link className=
   "row">` with: favourite `<button>`, `.row-main` (name + scenario), a
   `<span>{n} mods</span>`, a `<Badge>` Running/Standby. `.row` /
   `.row-main` CSS at `styles.css:23`; mobile collapse `styles.css:24`.
5. `/servers/{id}/start` → `supervisor.start` → `SingleServerError` → **409**
   with message (`api/servers.py:470-478`); `/stop` → 409 "that server is not
   the one running" if not active (`:481-488`). `Server.is_running` is on every
   list row (`ServerOut`).
6. `ServerForm.tsx` port-collision: `allServers` query `:582-585`,
   `portCollisions` IIFE `:586-603`, render block `:866-879`. `Server` type
   imported for that query — check it's not otherwise used before dropping the
   import.
7. `HistoryPanel.tsx` — `stats` query `refetchInterval:5000` (`:7-11`);
   `Stats` type in `lib/api.ts` (`{current, history: Sample[]}`); sample fields
   used: `name`/`map_name`, `players`, `max_players`, `ping_ms`. Backend
   `_STATS_HISTORY` deque `maxlen=60`, append-on-GET (`api/servers.py:74,
   140-145`); `history` is `list(deque)` oldest→newest.
8. RCON: `_rcon` helper `api/servers.py:148-162` → `client.players()` →
   `parse_players(await self.command("#players"))` (`rcon/client.py:156-158`).
   `parse_players` `:248-273`; regexes `_IP_RE`/`_PLAYER_RE`/`_PING_RE`
   `:243-245`. `_validate_command` allows `#players` (`:234`). Existing test
   `tests/test_rcon_client.py:90-101` fixes the current (wrong-for-Reforger)
   expected shapes — it will need rewriting.
8a. **`#players` format** (web research 2026-09-06; sources listed under *RCON
   `#players` format*): Reforger's `#players` is a *verified* BI wiki command
   (the `@`-prefix is only for BI-custom commands like `@logout`; plain
   `players` is the equivalent BE-RCON wire form — most clients accept both, so
   keep sending `#players`). The reply: an optional `processing command:
   players` echo, then a `Players on server:` header line, then one line per
   player as **`<decimalId> ; <identityUUID> ; <name>`** — whitespace around the
   `;` varies; the UUID is the 36-char lowercase-hex-with-dashes BI identity id;
   **no IP / ping / BE-GUID is present**. Proven parser (ReforgerJS
   `reforger-server/rcon.js`): header test `/players on server:/i`, row regex
   `/^(\d+)\s*;\s*([a-z0-9-]+)\s*;\s*(.*)$/i`. The manager's
   `RconClient._collect_response` already reassembles BE multipart, so
   `parse_players` sees the whole text (no client-side buffering needed).
   `#kick` / `#ban create` still take the numeric `id` — unchanged.
9. `PlayersPanel.tsx` — `players` query `:25-29` (`{players: Player[]}`,
   `refetchInterval:5000`); success-but-empty branch `:248-250`
   ("No players reported by RCON."); error branch `:199-202`. `Player` type in
   `lib/api.ts`.
10. No charting library in `frontend/package.json`.
11. `SortableModList` (`components/mods/SortableModList.tsx`) prints the name at
    `:129-133`; drag listeners are only on the `⠿` handle (`:114-125`), and
    `PointerSensor` has `activationConstraint.distance: 4` — a `<Link>` in the
    name area won't trigger a drag. `AddModRow` (`ModsPanel.tsx:707-…`) is the
    reference expandable-deps component: `useQuery(["mod", guid, "deps"])` +
    local `open` state. Route `/mods/:guid` → `ModDetailPage` (`app.tsx:27`);
    `ModDetail` list rows already link deps that way (`ModDetail.tsx:366`).
12. `config_gen` emits the **full dependency closure** into `config.json`
    itself (`servers/config_gen.py:147-178`): `roots` = explicit `server.mods`
    only (`:171`), then `resolve_dependencies(session, roots, use_api=False,
    use_disk=True)` (`:178`), post-order DFS. There is a sibling
    "explicit-only, no expansion" builder at `:126-133` for other callers.
    So the assigned list does **not** need dependency rows for the server to
    load them.
13. `PlayersPanel.tsx` already has: `players` table with kick/ban
    (`moderate()` `:83-89`, `#kick N` / `#ban N`), `#say` box (`send("#say")`
    `:72-81,185-190`), Restart / Shutdown (`send("#restart"/"#shutdown")`
    `:140-145`), scheduled restart via `/servers/{id}/schedule-restart`
    (`:91-126,146-183`), `rcon()` helper hitting `POST /servers/{id}/rcon`
    (`:51-57`). `runRcon` wraps a call + `players.refetch()` (`:59-70`).
14. `_validate_command` (`rcon/client.py:232-240`): allows exactly `#players`,
    `#restart`, `#shutdown`, `#kick \d+`, `#ban \d+`, `#say \S.*`. Rejects
    `#ban create/remove/list` and `@logout`. `RconClient` has `command()`
    (`:150-154`) and `players()` (`:156-158`); no `bans()` / ban helpers.
15. `config_gen.build_config` (`servers/config_gen.py:64-107`) writes `game.{
    name,password,passwordAdmin,scenarioId,maxPlayers,visible,supportedPlatforms,
    gameProperties,mods}` — **no `game.admins`**. `Server` model
    (`models/server.py`) has `admin_password`, `rcon_permission`, no admin list.
    `docs/rcon-reforger-spec.md` and BI config both put `admins` (identity ids)
    under `game`.
16. `ServerDetail.tsx` tabs: array at `:131-142` (`role="tablist"`), panels
    `:146-152` (`{tab === "Players" && <PlayersPanel id={id} />}`). Renaming the
    tab touches the array entry + that line + the import.

## Feature set

### §1 — "Load modpack" dialog on the Mods tab (R2)
`ModsPanel.tsx`. New "Load modpack…" toolbar button (disabled while `dirty`) →
dialog with a pack `<select>`, replace/append radios (`replace` default),
immediate `POST …/apply/…`, success shows `applied N` + dropped-pin names, then
`preflight.refetch()`. 409 → inline "stop the server first". Empty pack list →
a no-packs body. Mirror `Modpacks.tsx:511-588` and the existing pack/pin
dialogs (`ModsPanel.tsx:606-702`).

### §2 — disambiguate the two save controls (R1)
`ModsPanel.tsx`. `:388` label → "Snapshot to modpack…"; `:655` dialog title →
"Snapshot this mod set to a modpack"; `:659-662` body → "Creates a new reusable
modpack from this definition's **last saved** mod set and load order."; `:483`
button gets `title={dirty ? undefined : "No unsaved changes"}`.

### §3 — pre-flight on request only + button style (R3)
`ModsPanel.tsx`. `preflight` query: `enabled: false`,
`refetchOnMount: false`, `refetchOnWindowFocus: false`,
`refetchOnReconnect: false`. Add an idle render branch: when
`preflight.isFetched === false` show "Pre-flight has not run yet." + point at
the button. "Refresh pre-flight" button → `variant="outline"`. The `:255,278`
`refetch()` calls stay (a save / pin still refreshes it). Verdict badge only
when `preflight.data` exists.

### §4 — roster alignment + inline start/stop (R4)
`Servers.tsx` + `ServerRow.tsx` + `styles.css`.
- One shared grid for the roster: fixed columns
  `[star] minmax(0,1fr) [mods] [status] [actions]` applied once, every row
  aligned. Simplest: a `.roster` class (new, in `styles.css`) or a Tailwind
  grid on the wrapper + `ServerRow` returns `display:contents`.
- Per-row Start/Stop `<Button size="sm">` (stop `event.preventDefault()` so the
  `<Link>` navigation doesn't fire). Logic per decision 6; a `useMutation` in
  `ServerRow` invalidating `["servers"]`; disable while pending or while the row
  is mid-transition; inline error text under the roster or as a row `title`.
- Preserve mobile collapse behaviour.

### §5 — remove the port-collision warning (R5)
`ServerForm.tsx`. Delete `:580-603` and `:866-879`; drop the now-unused
`allServers` query and (if unused elsewhere) the `Server` import. `npm run
build` must stay clean (no unused-var TS error).

### §6 — History tab time-series chart (R6)
`HistoryPanel.tsx` (+ optional 1-line backend `maxlen` bump). Replace the
"Rolling history" `.list` with an inline `<svg>` line chart: player count over
the sample window, a faint ping line, min/max/now labels, graceful with 0–1
points. Keep "Current A2S sample" `<pre>` and the empty state. No new
dependency.

### §8 — assigned mods list: linked titles + dependency expander (R8)
`ModsPanel.tsx` + `SortableModList.tsx`.
- `SortableModList`: add optional `renderName?: (item) => ReactNode` (used for
  the name node, default = current text) and `renderExpanded?: (item) =>
  ReactNode` (full-width block rendered inside the `<li>`, below the flex row).
  Both undefined → identical output to today (Modpacks list unaffected).
- `ModsPanel` assigned list (`:503-557`): pass `renderName={(m) => <Link
  to={\`/mods/${m.mod_guid}\`} className="hover:text-amber-400">{m.mod_name ??
  m.mod_guid}</Link>}` and a `renderExpanded` that shows a caret-toggled
  dependency subtree (lazy `useQuery(["mod", guid, "deps"])`, same as
  `AddModRow`); resolved dep → `<Link to={\`/mods/${dep.guid}\`}>`, unresolved →
  plain text with an "unresolved" badge; "No dependencies" when the tree is
  empty. The caret lives in `renderActions` or `renderMeta`.
- `addModWithDeps` → `addMod`: add only `makeWorkingMod(mod.guid, mod.name)`
  when not already present; drop the dependency-row loop. Update the comment at
  `:194-196`. `candidates` / `AddModRow` still work unchanged.
- **Dedup render rule (R8 point 4, decision 10a).** Derive, from the working
  copy + each explicit row's `dependency_tree`, the set of explicit rows whose
  guid is covered by *another* explicit row's closure (cycle-guarded). The
  top-level `SortableModList` is fed only the *uncovered* rows. A covered row is
  rendered inside every covering parent's `renderExpanded` subtree as a
  **pick-styled** entry (enable toggle + pin badge, links to `/mods/{guid}`),
  distinct from pure dependency nodes; if it is `enabled:false` under an enabled
  parent, add a `warn` badge "loaded as a dependency of \<parent>". Covered rows
  stay in the working copy and the save payload unchanged — they are only
  filtered out of the top-level render. Removing a parent (or disabling it)
  recomputes coverage; an uncovered row reappears at the position implied by its
  `load_order`. Renumber `load_order` densely on save so reappearing rows land
  sensibly.
- No change to the save payload shape, `from-server`, or any backend file. The
  dedup rule is view-only; `server.mods` still carries every explicit pick.

### §7 — Players tab RCON fix (R7)
`backend/app/rcon/client.py` + `backend/app/api/servers.py` +
`frontend/src/components/server/PlayersPanel.tsx` +
`backend/tests/test_rcon_client.py`.
- Thread the raw `#players` text out: `RconClient.players()` returns
  `(players, raw)` or a small dataclass; `_rcon` / the `/players` route returns
  `{"players": [...], "raw": "<text>"}`.
- `PlayersPanel` empty branch: if `raw` present, render it in a `<pre>` with
  "Server returned this, but no player rows were parsed — paste it into the
  sprint notes."
- Rewrite `parse_players` for the researched Reforger shape (see *RCON
  `#players` format*); keep a fallback branch for the old space-separated
  Arma-3-style line so non-Reforger BE servers still parse.
- Rewrite the existing `test_rcon_client.py` player test around the Reforger
  sample below; add cases for the header/echo/empty-list lines.
- If the user later captures a real reply that differs, drop it into the sample
  block and adjust.

### §9 — RCON tab UI (R9)
`PlayersPanel.tsx` → **`RconPanel.tsx`** + `pages/ServerDetail.tsx` (tab
label/import) + `lib/api.ts` (types). Sections:
- **Players** — table: name, `playerId`, identity UID (mono, `break-all`, copy
  button), badges (`Admin` if UID ∈ the definition's admin list [§11; skip the
  badge if §11 is deferred], `Banned` if UID ∈ the current `#ban list`).
  Per-row actions: **Kick** (`#kick <playerId>`), **Ban…** (opens a form:
  duration presets 1h/24h/permanent + free seconds, optional reason →
  `#ban create <playerId> <sec> [reason]`), **Copy UID**, **Make admin** (§11).
  Keep the 5 s refresh.
- **Bans** — `GET /servers/{id}/bans` table (BanID, UID, duration/expiry),
  paginated (`?page=`), each row **Remove** (`#ban remove <UID>`). Plus an
  **offline ban** form: identifier (UID or player name) + duration + reason →
  `POST /servers/{id}/bans`.
- **Server control** — move the existing Restart / Shutdown buttons and the
  scheduled-restart widget here unchanged.
- **Raw command** — a collapsed `<details>`: text input → `POST
  /servers/{id}/rcon?raw=1`, response in a `<pre>`. The `#say` box moves here,
  relabelled "broadcast (best-effort — not a vanilla RCON command)".
- Errors per section; a 400 "RCON is not configured" / 409 "not the running
  server" surfaces inline (already how `_rcon` fails).

### §10 — RCON command surface + bans API (R9)
`backend/app/rcon/client.py` + `backend/app/api/servers.py` +
`backend/app/schemas/` + `backend/tests/`.
- `_validate_command`: also accept, with tight regexes,
  `#ban create <playerId|identityId|name> <digits> [reason]`,
  `#ban remove <token>`, `#ban list( <digits>)?`, `@logout`.
- `RconClient`: `bans(page=1)` → `parse_bans(command("#ban list …"))`;
  `ban_create(identifier, seconds, reason=None)`; `ban_remove(identity_id)`;
  send `@logout` in `__aexit__`/`close` before the socket closes (best-effort,
  swallow errors).
- `parse_bans(text)` — rows `BanID ; Player UID ; Duration` (same `;` style,
  skip header/echo/empty). Returns `[{ban_id, uid, duration}]`; keep `raw`.
- Routes (all via `_rcon` guard, `dependencies=authed`):
  `GET /servers/{id}/bans?page=` → `{bans, raw, page}`;
  `POST /servers/{id}/bans` `{identifier, duration_seconds, reason?}` → runs
  `#ban create`, returns the command echo;
  `DELETE /servers/{id}/bans/{identity_id}` → `#ban remove`.
  Extend `POST /servers/{id}/rcon` with `?raw=1` (or add `/rcon/raw`) that
  bypasses the whitelist — still `authed` + active-server + honours
  `rcon_permission` (a `monitor` server rejects everything but `#players` /
  `@logout`; enforce that server-side, don't trust the client).
- Tests: `parse_bans` shapes; `_validate_command` accepts the new forms and
  still rejects junk; ban routes happy-path + 400/409 with a stubbed client.

### §11 — persistent server admin list (R9, optional, migration)
`backend/app/models/server.py` + Alembic migration +
`backend/app/servers/config_gen.py` + `backend/app/schemas/server.py` +
`RconPanel.tsx`.
- `Server.game_admins: Mapped[list[str] | None]` (JSON), migration adds the
  nullable column.
- `build_config`: `config["game"]["admins"] = server.game_admins or []`.
- `ServerCreate`/`ServerUpdate`: `game_admins: list[str] | None`.
- UI "Make admin" / "Remove admin" → `PATCH /servers/{id}` toggling the UID in
  `game_admins`; toast "applies on next server restart". Badge reads this list.
- If the sprint keeps *no migrations*: cut §11, drop the "Make admin" button and
  the `Admin` badge; R9 still ships kick/ban/bans/raw.

## RCON `#players` format

Researched 2026-09-06 (not from a live capture of this server — from the
BI wiki + two independent implementations/guides). Confirm against the raw
`<pre>` once §7 ships.

- Command: **`#players`** — a *Verified* BI wiki command. The `@` prefix is only
  for BI-custom RCON commands (`@logout`); plain `players` (no prefix) is the
  BattlEye-RCON wire spelling and most clients accept either. Keep `#players`.
- Reforger reuses the **Arma 3 BattlEye RCON wire protocol** (BattleWarden,
  bercon, BattleMetrics all work unchanged).
- Reply shape:
  ```
  processing command: players            (optional echo line)
  Players on server:                      (header)
  0 ; 1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809 ; underlines
  1 ; 9f8e7d6c-5b4a-3210-fedc-ba9876543210 ; Some Player
  ```
  - fields: `<decimalId> ; <identity-UUID> ; <name>`, `;`-separated, whitespace
    around the `;` is variable.
  - `identity-UUID` = the 36-char lowercase-hex-with-dashes Bohemia identity id
    (what offline `#ban create <identityId>` / `#ban remove` take).
  - **No IP, no ping, no BattlEye GUID** — Reforger RCON does not expose them.
  - a trailing count line may or may not be present — skip any non-matching line.
- Proven parser (ReforgerJS `reforger-server/rcon.js`, in production use):
  header detect `/players on server:/i`; row `/^(\d+)\s*;\s*([a-z0-9-]+)\s*;\s*(.*)$/i`
  → `{ id:int, uid:str, name:str }`.

Sources:
- BI wiki — Arma Reforger: Server Management (command list, `#players`
  "Lists session's players and their playerId", `@` = custom prefix):
  https://community.bistudio.com/wiki/Arma_Reforger:Server_Management
- ReforgerJS `reforger-server/rcon.js` — sends `players`, header regex
  `/players on server:/i`, row regex `/^(\d+)\s*;\s*([a-z0-9-]+)\s*;\s*(.*)$/i`:
  https://github.com/ZSU-GG-Reforger/ReforgerJS
- Loafhosts — "Find a Player's ID": `Players on server: [Player#] ; [Player UID]
  ; [Player Name]`, UID is a 36-char UUID, "no way to get a Steam ID, BattlEye
  GUID, IP, or ping": https://loafhosts.com/guides/find-arma-reforger-player-ids
- XGamingServer RCON command reference (`#players` Verified; `#` vs unprefixed;
  default RCON port 19999; `permission: admin`):
  https://xgamingserver.com/tools/arma-reforger/rcon-commands
- `docs/rcon-reforger-spec.md` (in-repo) — full wire protocol + the complete
  vanilla command set, permissions matrix, `#ban list` row format
  `BanID ; Player UID ; Duration`, `@logout`, "no response JSON — build parsers
  defensively", `playerId` ≠ `identityId`.

### live capture (fill when available)
```
(pending — paste the raw <pre> from the Players tab here)
```

## Data-model changes
Only if **§11** (persistent "Make admin") is taken: one Alembic migration adding
a nullable `servers.game_admins` JSON column. Otherwise none. Every other change
is API-behaviour only: `/players` gains `raw` + per-player `uid`; new
`/servers/{id}/bans` routes; `/servers/{id}/rcon?raw=1`.

## Non-goals
- Backend changes to `apply_modpack` (a third "merge + reorder" mode).
- Client-side modpack merge into the staged working copy without persisting.
- Persisting A2S history beyond the in-memory deque; a real metrics store.
- Adding a charting dependency.
- Reworking the single-server rule; queuing starts.
- Any change to `config_gen` or the dependency resolver for §8 (it relies on the
  closure they already build). §11 *does* add `game.admins` to `config_gen`.
- A migration / batch job to strip dependency rows from servers saved under the
  old `addModWithDeps` behaviour — they stay until the operator removes them.
- A persisted explicit-vs-dependency flag on `server_mods` or `modpack_items`
  rows. §8's dedup (decision 10a) is a view-only render rule over the existing
  explicit rows; modpack snapshots stay a flat `{mod_guid, load_order}` list and
  the receiving server re-derives the dedup on display.
- RCON commands outside the documented vanilla surface + one raw passthrough —
  no mod-command catalogue, no `#say` "support" claims, no live server-message
  (`0x02`) stream in the UI.
- MCP tools for bans / raw RCON (a later sprint if wanted).
- Real-time player presence (join/leave events) — the tab polls `#players`.

## Verification
```bash
cd frontend && npm run build                                   # tsc -b && vite build — clean
cd backend && ./.venv/Scripts/python.exe -m pytest -q          # green, incl. new RCON + bans tests
```
Manual read-through on a running stack: Mods tab (load modpack; pre-flight only
on button; mod titles link to detail; caret expands dependencies; adding a mod
with deps adds one row); /servers roster columns line up and Start/Stop works
with the stop-other prompt; no port-collision box in the form; History tab
shows a chart; RCON tab lists the joined player with UID, Kick / Ban / Copy UID
work, the Bans section lists and removes bans, the raw box runs an arbitrary
command, and (if §11) Make admin writes the identity id.
