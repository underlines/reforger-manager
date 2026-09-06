# Reforger Manager — Sprint 7 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-07 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: done, green

All ten stories implemented — S1–S5, S7–S10 plus the S6 integration pass. The
optional migration-bearing story **S10 was taken**. Backend suite green
(**319 passed**, sprint-6 baseline 296 → +23: S5 +3, S8 +13, S10 +7); frontend
`npm run build` clean (`tsc -b && vite build`). No Docker, no live stack, no
real RCON server.

## Report → fix mapping (R1–R9)

| Report | Fix |
| --- | --- |
| R1 — two near-identical "save" buttons on the Mods tab | S1: "Save as modpack" → **"Snapshot to modpack…"**, dialog reworded to say "last saved" set; disabled "Save mod set" gets `title="No unsaved changes"`. No behaviour change. |
| R2 — no way to load a modpack from the Mods tab | S1: new **"Load modpack…"** toolbar dialog (pack `<select>` + replace/append radios) → `POST /api/modpacks/{pack}/apply/{server}` verbatim, persists immediately, disabled while the working copy is dirty. |
| R3 — pre-flight runs on mount; button looks wrong | S7: pre-flight query `enabled:false` + no `refetchOnMount/WindowFocus/Reconnect`; runs only via `refetch()` (button + after save/pin/apply). "Pre-flight has not run yet" idle line. Button `ghost` → `outline`. |
| R4 — /servers roster columns misaligned, no inline start/stop | S2: one shared `.roster` grid (fixed column tracks, every row aligned) + per-row Start/Stop `<Button>` with the stop-the-other-first `window.confirm`; mobile collapse preserved. |
| R5 — per-server "Port collisions" warning is noise | S3: `portCollisions` IIFE + `allServers` query + render block deleted from `ServerForm.tsx`. No replacement. |
| R6 — History tab: scrolling per-sample list | S4: inline hand-built `<svg>` line chart (player count + faint ping line, min/max/now labels), degrades at 0/1 samples. No charting dependency. "Current A2S sample" `<pre>` kept. |
| R7 — Players tab empty while History shows the player (BUG) | S5: `parse_players` rewritten for the Reforger semicolon shape `<id> ; <uuid> ; <name>` (legacy space-separated kept as fallback); raw `#players` text threaded to `/players` and shown in a `<pre>` when nothing parses. New per-player `uid`. `_STATS_HISTORY` maxlen 60 → 240. |
| R8 — assigned list: link titles, expandable deps, stop storing dep rows | S7: mod titles link to `/mods/{guid}`; per-row caret lazily expands the dependency subtree (read-only, resolved deps linked); `addModWithDeps` → `addMod` adds only the explicit pick (config_gen already builds the closure at start); **dedup render rule** — an explicit row whose guid is in another explicit row's dependency closure is hidden from the top-level list and shown pick-styled (keeps enable + pin controls) under each covering parent, cycle-guarded, restored when no covering parent remains. View-only: working array / save payload / snapshot unchanged. |
| R9 — grow Players tab into a full RCON tab | S8 (backend) + S9 (UI) + S10 (persistent admin): see below. |

## What shipped

### Frontend

- **S1 — `ModsPanel.tsx`.** "Load modpack…" dialog (4th `<Dialog>`, mirrors the
  `/modpacks` "Apply to…" flow): pack picker, replace (default) / append radios,
  immediate apply, `applied N` + dropped-pin names on success, 409 → "stop the
  server first", empty-list body. Disabled while `dirty`. Button renames +
  disabled-button `title` per R1. No new backend surface.
- **S2 — `Servers.tsx` + `ServerRow.tsx` + `styles.css`.** `.roster` wrapper
  with one fixed `grid-template-columns` (`1.5rem minmax(0,1fr) 5rem 6rem 7rem`);
  every `.row` shares column edges. `ServerRow` takes a `runningServer?` prop;
  Start/Stop `useMutation` with `preventDefault()+stopPropagation()` (the row is
  a `<Link>`), stop-other `window.confirm`, invalidates `["servers"]` +
  `["server",id]`, inline `.error`. Mobile `@media (max-width:700px)` collapse
  re-expressed for `.roster` (hides the mods column).
- **S3 — `ServerForm.tsx`.** Port-collision computation, its `allServers` query,
  and its render block removed (40 lines). `Server` / `keepPreviousData` imports
  kept (still used elsewhere). Ports still save.
- **S4 — `HistoryPanel.tsx`.** "Rolling history" list replaced by one inline
  `<svg>` (`viewBox`, `w-full h-40`): amber player-count polyline, faint
  `currentColor` ping polyline on its own scale (omitted if all-zero),
  grid lines, `now/min/max` + `N samples · ~Ns` labels. 0 samples → existing
  `<Empty>`; 1 sample → dot + read-out, no polyline. `num()` guards `NaN`.
  `Badge` import dropped. No dependency added (`git diff frontend/package.json`
  empty).
- **S5 — `PlayersPanel.tsx`.** Query type → `{ players; raw? }`; IP + Ping
  columns replaced by one **Identity (UID)** column (`font-mono text-xs
  break-all`, `player.uid ?? "—"`); empty branch renders `raw` in a `<pre>` with
  a "paste into PLAN" caption. `Player.uid?: string | null` added to `api.ts`.
- **S7 — `ModsPanel.tsx` + `SortableModList.tsx`.** `SortableModList` gains
  optional `renderName` / `renderExpanded` props (both default to today's
  output — Modpacks page byte-identical). Assigned list: `<Link>` titles, a
  lifted `expandedRows` Set + `AssignedModExpanded` caret subtree (shared
  `["mod",guid,"deps"]` cache), `addMod` explicit-only, and the R8-point-4
  dedup via `useQueries` over `working` → `depEdges` → `coverageByParent` →
  `covered` → `topLevel = working.filter(!covered)`; `reorderVisible` splices
  the visible order back into the full `working` array. Pre-flight changes per
  R3 above.
- **S9 — `PlayersPanel.tsx` → `RconPanel.tsx`** (git rename) + `ServerDetail.tsx`
  (tab `"Players"` → `"RCON"`) + `api.ts` (`Ban`, `BansResponse`,
  `Server.game_admins?`). Four sections: **Players** (Name / playerId /
  Identity-UID+Copy / Status badges `Banned`+`Admin` / Kick·Ban…·Copy·Make-admin),
  **Bans** (`GET /bans?page=` table, 25/page prev-next, per-row Remove, an
  offline-ban `BanForm`), **Server control** (Restart / Shutdown + the
  scheduled-restart widget moved verbatim), **Raw command** (`<details>`,
  `POST /rcon?raw=1`; the `#say` box relocated here, relabelled
  "Broadcast (#say — best-effort, not a vanilla RCON command)"). One shared
  `BanForm` component. `git grep PlayersPanel` clean in code (one historical
  `.sprints/2/STORIES.md` prose line left per the no-`.sprints` rule).

### Backend

- **S5 — `rcon/client.py` + `api/servers.py` + `test_rcon_client.py`.**
  `parse_players` rewritten (`_REFORGER_ROW` / `_SKIP` regexes, legacy
  space-separated fallback with `uid=None`); new `@dataclass PlayersResult`;
  `RconClient.players()` returns it; `/players` route returns
  `{"players", "raw"}`. `_STATS_HISTORY` `maxlen` 60 → 240. `parse_players`
  stays a standalone importable pure function. `_validate_command` /
  `"#players"` untouched.
- **S8 — `rcon/client.py` + `api/servers.py` + new `schemas/rcon.py` +
  `test_rcon_client.py` + new `test_rcon_bans_api.py`.** `_validate_command`
  widened (tight regexes) for `#ban create <token> <secs> [reason]`,
  `#ban remove <token>`, `#ban list [page]`, `@logout` — additive, `#kick` /
  `#ban <digits>` / `#say` unchanged. New pure `parse_bans` (`BanID ; UID ;
  Duration`). `RconClient.bans/ban_create/ban_remove` + best-effort `@logout` on
  `__aexit__`/`close`. Routes (all behind the existing `_rcon` guard,
  `dependencies=authed`, refactored to `_run_rcon`/`_rcon_guard`/
  `_assert_rcon_permission` — `monitor` permission = `#players`/`@logout` only,
  enforced server-side): `GET /servers/{id}/bans?page=`,
  `POST /servers/{id}/bans` `{identifier,duration_seconds,reason?}` → `{echo}`,
  `DELETE /servers/{id}/bans/{identity_id}` → 204, and `POST /servers/{id}/rcon`
  gains `?raw=1` (skips the whitelist via `_sanitize_raw`, 512-char cap +
  control-char strip; still auth + active-server + permission gated). Multi-word
  player names must use `?raw=1` or an identityId (noted in the route docstring).
- **S10 — `models/server.py` + `servers/config_gen.py` + `schemas/server.py` +
  new migration + new `test_game_admins.py`.** `Server.game_admins:
  Mapped[list[str] | None]` (JSONVariant, nullable, same style as
  `game_properties`). `build_config` `game` dict emits
  `"admins": list(server.game_admins or [])`. `game_admins: list[str] | None`
  on `ServerBase` (Create + Out) and `ServerUpdate` (partial PATCH via
  `exclude_unset`). Migration
  **`0004_server_game_admins`** (`down_revision = 0003_mcp_tokens`; `alembic
  heads` = single head): `add_column` / `drop_column` with
  `sa.JSON().with_variant(postgresql.JSONB(), "postgresql")`. S9's `RconPanel`
  admin wiring verified against the real shape — no change needed.

## Data-model changes

**One migration** — `backend/app/migrations/versions/0004_server_game_admins.py`
adds the nullable `servers.game_admins` JSON column (`down_revision`
`0003_mcp_tokens`). `DB_MIGRATE_ON_STARTUP=create_all` (the default) also picks
the column up on a fresh DB via the model. Every other backend change is
API-behaviour only: `/players` gains `raw` + per-player `uid`; new
`/servers/{id}/bans` routes; `/servers/{id}/rcon?raw=1`.

## New API / FE surface

- `GET /api/servers/{id}/players` → `{players:[{id,name,uid,ip,ping,raw}], raw}`
  (was `{players:[…]}`).
- `GET /api/servers/{id}/bans?page=` → `{bans:[{ban_id,uid,duration,raw}], raw, page}`.
- `POST /api/servers/{id}/bans` `{identifier, duration_seconds, reason?}` → `{echo}`.
- `DELETE /api/servers/{id}/bans/{identity_id}` → 204.
- `POST /api/servers/{id}/rcon?raw=1` — whitelist-bypass passthrough (still
  auth + active-server + `rcon_permission`).
- `ServerCreate`/`ServerUpdate`/`ServerOut` gain `game_admins`.
- FE: `RconPanel` (was `PlayersPanel`), `Ban` / `BansResponse` types,
  `Player.uid?`, `Server.game_admins?`.

## Verification performed

- `cd backend && ./.venv/Scripts/python.exe -m pytest -q` → **319 passed,
  94 warnings in ~54s** (full suite, sqlite+aiosqlite). Run independently by the
  orchestrator after every story and once more at S6.
- `cd frontend && npm run build` → clean (`tsc -b && vite build`; pre-existing
  >500 kB chunk advisory only). Run independently after S2/S3/S4/S5/S7/S9/S10.
- `alembic heads` → single head `0004_server_game_admins`.
- `git grep PlayersPanel -- frontend backend` → empty.
- `git diff frontend/package.json backend/requirements.txt` → empty (no new deps).
- New / changed tests: `test_rcon_client.py` (rewritten player parser + ban-
  validation + `parse_bans` cases), new `test_rcon_bans_api.py` (stubbed-client
  route tests: happy paths, 400 RCON-disabled, 409 not-running, `monitor`
  rejection, `?raw=1`), new `test_game_admins.py` (7: `config_gen` emits
  `game.admins`; `game_admins` round-trips through POST/PATCH/GET).

## Not exercised

- Docker / live stack / real Workshop API / real RCON server — all stubbed.
- **The `#players` and `#ban list` parsers were built to the *documented*
  format** (`docs/rcon-reforger-spec.md` + the PLAN's web sources), not a live
  capture. A real reply is still worth pasting into
  PLAN → *RCON `#players` format* → *live capture*; S5 threads the raw text to a
  `<pre>` in the tab so a format drift is visible, not silent.
- Migration upgrade/downgrade not run by a test — no `alembic`/`command.upgrade`
  harness in `backend/tests/`; chain validity confirmed via `alembic heads` /
  `history` only.
- Browser-level feel of the roster grid, the history chart, the RCON tab, the
  dependency-expander / dedup interaction — all build-gated, not clicked.
- CI / deploy; nothing committed or pushed.

## Minor observations

- S9 left the moved-verbatim `moderate()` helper's `"ban"` branch as unreachable
  dead code (live-player bans now go through `BanForm`) — kept to honour "move
  verbatim"; a trivial follow-up cleanup.
- `game.admins` is exposed in `ServerOut` as `list[str] | None` (matching
  `game_properties`); the FE normalises with `?? []`.
- A hand-edited `extra_config.game.admins` still deep-merges over the generated
  `game.admins` (`config_gen`), as with every other `game.*` field.

## How this sprint was implemented

Orchestrator (Claude Sonnet 5) stayed a thin coordinator — PLAN + STORIES held
every fact; no story was implemented by the orchestrator and no re-research was
done. Three agent types were rotated per the sprint skill:

| Story | Agent | Notes |
| --- | --- | --- |
| S1 | native Claude subagent | pre-existing (done before this session) |
| S2 | opencode · glm (`z-ai/glm-5.3-flash`) | slow research phase (~5 min of CSS/media-query deliberation) then landed clean first try |
| S3 | opencode · deepseek (`deepseek/deepseek-v4-flash-0731`) | landed clean in one pass, ~1 min |
| S4 | opencode · glm | clean first try |
| S5 | native Claude subagent | RCON parser rewrite + raw passthrough |
| S7 | native Claude subagent | the trickiest FE story (dedup render rule); ~7.5 min, one large self-correcting revision |
| S8 | opencode · deepseek | green pytest (312) but the agent then stalled on post-run narration without emitting its exit; orchestrator verified the suite independently and accepted it |
| S9 | native Claude subagent | `PlayersPanel` → `RconPanel` rename + 4-section tab |
| S10 | native Claude subagent | the sprint's only migration (`0004_server_game_admins`) |
| S6 | orchestrator | full `pytest` + `npm run build` + consistency read + this file |

Rotation totals: opencode·glm ×2 (S2, S4), opencode·deepseek ×2 (S3, S8),
native Claude ×4 (S5, S7, S9, S10) — the chained and highest-risk stories on the
native agent by design. Dependency handling: S2/S3/S5 ran in parallel (disjoint
files); the RCON chain S5 → S8 → S9 → S10 ran strictly sequential (shared
`rcon/client.py` / `api/servers.py` / the renamed panel), each verified green
before the next started. S4 ran on glm in parallel with S5. Only one stall
(S8, post-success narration) — no restarts or reassignments needed. Nothing
committed or pushed.
