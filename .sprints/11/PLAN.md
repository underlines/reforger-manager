# Reforger Manager — Sprint 11 plan

Diagnostic trustworthiness: make the tools that are meant to catch a broken
server actually catch it.

## Context

On 2026-09-17 server 32 (`BADIS | custom`, WCS Everon Conflict) refused every
client join for roughly two hours. The server itself was healthy the whole time —
60 FPS, registered with the backend, BattlEye connected, zero script exceptions.
Every join ran the same way:

```
RPL : ServerImpl event: connected (identity=0x00000000)
DEFAULT: BattlEye Server: 'Player #0 underlines - BE GUID: ...'
RPL : ServerImpl event: disconnected (..., slots=0/12), group=4, reason=3
BACKEND (E): [RestApi] LobbyApiS2S/RoomsAcceptPlayerS2S 400 apiCode="InvalidSessionTicket"
```

The cause was a single wrong character sequence in one column:
`servers.scenario_game_id` held
`{6148289172F86E7A}Missions/WCS_Everon_Conflict_NvS_Layout1.conf`, where
`6148289172F86E7A` is **WCS_Everon's own mod GUID**, not the scenario's resource
GUID. The true id — confirmed against a joinable third-party server running the
identical mission and against the live Workshop API — is
`{02778C5E44407C03}Missions/WCS_Everon_Conflict_NvS_Layout1.conf`.

The write bug behind it is already fixed (`76895ac`, "preserve enriched scenario
data across local mod rescans"): `_upsert_local()` was wiping API-enriched
`mod_scenarios` rows and replacing them with `scanner.parse_scenarios()`'s
offline guess, `"{" + mod_guid + "}" + path`.

**This sprint is about why it took two hours to find.** The manager ran a
pre-flight before every one of those starts and never mentioned the scenario. It
did emit `verdict: "blocked"` — for a reason that is wrong on every modded server
in the fleet, which is exactly why the verdict was ignored, by the supervisor and
by the operator. Four other pieces of friction each cost real time on the same
day. All five are recorded below with the evidence that produced them.

## Guiding principle

**A warning that fires when nothing is wrong is worse than no warning at all.**
Pre-flight's job is not to enumerate everything it noticed; it is to be trusted.
Every `blocked` this sprint leaves in place must be a condition that genuinely
prevents a working server, and every condition that genuinely prevents a working
server must be a `blocked`. Noise reduction comes *before* new checks — adding a
scenario check to a report nobody reads would have saved nobody.

## Decisions locked

| # | Decision |
|---|---|
| 1 | Fix pre-flight's false positives (F1) **before** adding the scenario check (F2). A new check inherits the credibility of the report it lands in. |
| 2 | `blocked` keeps meaning "this will not work". Engine-builtin GUIDs and older-than-engine addon versions are not that, and stop being `blocked`. |
| 3 | `start_server` surfaces blocking pre-flight findings in its response instead of silently proceeding. It still starts — the supervisor is not made to refuse — but the caller can no longer miss them. |
| 4 | Scenario validation warns, never blocks. A scenario id the manager cannot verify is usually an un-enriched library, not a broken server; blocking would reintroduce the very noise F1 removes. |
| 6 | Mod downloads batch into one engine invocation. The job signature already takes a list; only the API and MCP surfaces are single-GUID. |
| 7 | The "no downloads while a server runs" guard stays exactly as it is. Investigating whether it can be relaxed is a research spike with no guaranteed payoff and is cut from this sprint (see Non-goals). |

## Code-level facts the implementation must respect

1. **`ENGINE_BUILTIN_GUIDS` already exists and pre-flight does not use it.**
   `backend/app/mods/resolve.py:40-43`:

   ```python
   ENGINE_BUILTIN_GUIDS = frozenset({
       "58D0FB3206B6F859",  # data/ArmaReforger.gproj
       "5614BBCCBB55ED1C",  # core/core.gproj
   })
   ```

   `downloader.py:409`, `sync.py:212,224` and `config_gen.py:184,231` all filter
   against it. `backend/app/servers/preflight.py` never imports it. Consequence,
   observed on server 32 verbatim:

   > `58D0FB3206B6F859 is not resolvable on the Workshop (deleted, blocked, or private).` — level `blocked`

   `58D0FB3206B6F859` is the vanilla `data/ArmaReforger.gproj`, present in every
   install and visible in every engine log's "Available addons" block. It is
   reachable as a dependency of nearly every addon (WCS_Core, WCS_Armaments,
   RHS, …), so `_verdict()` (`preflight.py:344-346`) returns `blocked` for
   **every modded server in the fleet, permanently**.

2. **`_version_compat` blocks on a declared game version that is merely older.**
   `preflight.py:322-338` returns `"blocked"` when the addon's declared
   `game_version` differs by major or by "several minors". This fired on Stun
   Grenade (`59EAA899751805DF`, declared `1.3.0.130`, engine `1.8.0.13`) and the
   mod was removed from server 32 on the strength of it — while the reference
   server `69.67.175.17:2003` runs that exact mod and version against the same
   engine build without issue. Reforger's declared game version is a
   built-against marker, not a compatibility gate.

3. **The supervisor does not consult pre-flight's verdict.** Server 32 started
   successfully on every attempt while `preflight()` reported `verdict:
   "blocked"`. This is defensible behaviour given facts 1 and 2 — but the result
   is a report with no consumer.

4. **Pre-flight has no scenario check of any kind.** `preflight()`
   (`preflight.py:58-224`) reads `ServerMod` rows and resolves the dependency
   tree; `server.scenario_game_id` is never read. A scenario id that matches no
   `mod_scenarios` row, or that was synthesised from a mod GUID, passes silently.

5. **`mod_scenarios` rows distinguish verified from guessed data — but only by
   implication.** `backend/app/models/mod.py:101-116`: `game_id`, `name`,
   `game_mode`, `player_count`. API enrichment (`sync.py:212-253`,
   `_upsert_api_scenarios`) sets `name`/`game_mode`/`player_count`; the offline
   scanner path sets `game_id` only, leaving the rest NULL. So
   **`name IS NOT NULL` is the current proxy for "this id came from the
   Workshop"**. Server 32's six WCS_Everon rows all had `name = NULL`. The
   scanner's own docstring (`scanner.py:24-31`) is explicit that its id is a
   guess:

   > the API is authoritative for scenarios and returns the true per-scenario
   > resource GUID, which is often *not* the mod GUID; this offline id is a
   > best-effort fallback.

   Consider making that implication explicit with a `source` column rather than
   relying on `name IS NOT NULL` — see F2.

6. **The Workshop API returns the correct ids and the manager already calls it.**
   `WorkshopClient.get_scenarios()` (`workshop.py:307-311`) hits
   `GET /mods/{id}/scenarios`. Live response for `6148289172F86E7A`, abridged:

   ```json
   {"name":"[WCS] New Everon - North/South #1",
    "gameId":"{02778C5E44407C03}Missions/WCS_Everon_Conflict_NvS_Layout1.conf",
    "gameMode":"#AR-Scenario_GameMode_Campaign","playerCount":128}
   ```

   Six scenarios, six distinct resource GUIDs, none equal to the mod GUID.

7. **`tail_log` reads the supervisor's aggregate capture, not the engine's
   session log.** `backend/app/mods/logview.py:109-111` hardcodes
   `profile_dir / "logs" / "console.log"`; `supervisor.py:203-204` sets
   `log_path = log_dir / "console.log"` as the pipe target. The engine
   *separately* writes its own authoritative log to
   `PROFILES_DIR/{id}/logs/logs_<UTC timestamp>/console.log`. Observed on server
   32: the aggregate sat at 853 lines / 122 855 bytes while the live session file
   was 129 779 bytes and still growing, and the aggregate contained a torn line
   boundary — a truncated fragment `1C4B48772550E0F/addon.gproj' guid: ...`
   immediately after an unrelated line from an earlier run. Reading the aggregate
   led to a wrong conclusion that the server process had died; `docker exec ps`
   and the manager's own `is_running` then disagreed with it.

8. **`run_mod_download` already accepts a list.** `downloader.py:372-380`:

   ```python
   async def run_mod_download(ctx, guids: list[str], versions: dict[str, str] | None = None)
   ```

   `_build_config(guids, versions)` (`:165`), `_run_engine(ctx, guids, versions)`
   (`:243`) and the progress parser (`:133-135`) are all list-shaped throughout.
   Only the API route and the `download_mod` MCP tool are single-GUID. Measured
   cost of that: 34 downloads on 2026-09-17, each spawning a headless engine
   (~25 s of init plus script compilation) to fetch payloads as small as 688 KB —
   roughly 17 minutes wall-clock, the overwhelming majority of it engine startup.

9. **The running-server download guard is a single line.**
   `downloader.py:243-245`:

   ```python
   async def _run_engine(ctx, guids, versions):
       if supervisor.is_running():
           raise ModDownloadError("cannot download addons while a server is running")
   ```

   The downloader runs a *separate* engine process with its own temp profile and
   its own `-addonDownloadDir` (`:260` writes a temp config). It forced three
   stop → download → start cycles on 2026-09-17, i.e. three outages, purely to
   add mods to a running server.

10. **Mutation responses return the full mod list.** `update_server` returns the
    complete `ServerSchema` including every `ServerMod`. On a 78-mod server that
    is ~900 lines of mostly `"pinned_reason": null` per call, for a single-field
    patch. Eight such calls were made on 2026-09-17. `list_mods` exceeded the MCP
    response ceiling twice on a 259-row library and had to be spilled to a file.

11. **`get_server_config` already exists** (`mcp/tools.py`, backed by
    `config_gen.build_config`) and returns the exact generated `config.json`
    without writing it. The incident was solved by diffing two of these by hand
    via `docker exec` + Python; no tool composes them.

12. **Tests have no `conftest.py`.** Each file is self-contained. For
    pre-flight work copy the harness from an existing route test; for the
    scanner/sync-shaped work copy `backend/tests/test_refresh_local_mods.py`
    (fixture at `:38-47`), which is the pattern
    `test_mod_sync_scenario_preservation.py` already follows.

## Feature set

### F1 — Make `blocked` mean blocked (`backend/app/servers/preflight.py`)

Highest value in the sprint: it is the precondition for F2 being read at all.

- Import `ENGINE_BUILTIN_GUIDS` from `..mods.resolve` and skip those GUIDs
  entirely in the `for guid, node in nodes.items()` loop (`:110`) — no Workshop
  lookup, no availability check, no compatibility check, no `resolved_mods`
  entry. They are the base game (F-fact 1).
- Downgrade `_version_compat`'s older-than-engine result from `blocked` to
  `warn` (F-fact 2). Keep `blocked` for the genuinely fatal direction: an addon
  declaring a game version **newer** than the installed engine. The existing
  `warn` copy ("Older-version addons usually run on a newer engine, but verify
  against a live start", `:181`) already says the right thing — it should simply
  be the only outcome in that direction.
- Leave the unlisted/private/obsolete logic as is: it already correctly
  distinguishes present-locally (`warn`, `:151-156`) from absent (`blocked`).

Expected outcome, verifiable against the fleet: servers 13/22/24/32 all return
`verdict: "green"` or `"warn"`, and any future `blocked` is worth acting on.

### F2 — Scenario validation

**F2a — a scenario check in pre-flight.** Read `server.scenario_game_id`, and
resolve it against `mod_scenarios` rows belonging to the server's enabled mods:

- Matches a row with `name IS NOT NULL` (API-verified) → `green`, no check
  emitted.
- Matches a row with `name IS NULL` (offline guess) → `warn`: *"This scenario id
  came from a local scan, not the Workshop, and may use the mod's GUID instead of
  the scenario's own resource GUID. Run a mod sync to verify it."*
- Matches **no** row for any enabled mod, but the path portion matches a row
  under a different GUID → `warn`, naming the verified id as the likely intended
  one. **This is the exact server-32 shape and the message must name the
  correction**, e.g. *"Server is set to `{6148289172F86E7A}Missions/….conf`; the
  Workshop lists that mission as `{02778C5E44407C03}Missions/….conf`."*
- Matches nothing at all → `warn`: the scenario's mod may be disabled or absent.

Never `blocked` (Decision 4): an un-enriched library is common and benign, and a
server whose scenario genuinely cannot resolve fails loudly at start anyway.

**F2b — repair the stored ids.** `76895ac` stops corruption; it does not fix rows
already wrong. Add a small repair pass, run as part of `mod_sync` (not a separate
job): after enrichment, for every `Server` whose `scenario_game_id` has no
API-verified match but whose path portion matches exactly one verified
`mod_scenarios` row, log the discrepancy at WARNING with both ids. **Do not
rewrite the column automatically** — silently changing which mission a server
boots is worse than the warning. Surface it in F2a instead and let the admin
click.

Optional, worth costing during implementation: add a `source` column
(`"api" | "scan"`) to `mod_scenarios` so F2a tests a fact rather than inferring
one from `name IS NOT NULL` (F-fact 5). It is a one-column migration and makes
the check honest; skip it if the sprint is tight, since the proxy is currently
exact.

### F3 — Batch mod downloads

- New API route and MCP tool taking `guids: list[str]` (plus the existing
  optional per-GUID `versions` map), calling `run_mod_download` **once**. The job
  layer needs no change at all (F-fact 8) — this is API and MCP plumbing over an
  already-list-shaped core.
- Keep the single-GUID route as a thin wrapper so existing callers and the
  frontend keep working.
- Progress reporting already aggregates across GUIDs (`ModDownloadProgress.percent`,
  `:133-135`), so the job's progress bar is correct for a batch without changes.

Expected saving on the 2026-09-17 workload: 34 engine spawns → 2, roughly 17
minutes → under 2.

The running-server guard (F-fact 9) is left exactly as it is — see Non-goals.
Batching does not need it: it only changes how many engine spawns one call to
the download route costs, not when downloads are allowed.

### F4 — `tail_log` reads the live session log

`backend/app/mods/logview.py`, `resolve` (`:99-111`):

- Always prefer the newest `logs/logs_*/console.log` by directory mtime when a
  session directory exists — it is the engine's own authoritative log, live or
  historical. Fall back to the aggregate `logs/console.log` only when no
  session directory exists at all (a server that has never started). No
  merging of the two: once a session log exists it is strictly the useful one.
- Return which file was chosen in the response metadata, so a caller can tell the
  live session log from the fallback.
- Keep `resolve_safe_path`-equivalent ancestry checks: the resolved path must
  remain under the server's profile directory.

This removes both observed failure modes from F-fact 7 — stale content, and torn
lines from a previous run being read as current.

### F5 — Trim mutation and listing responses

- `update_server` (and the other server mutations) omit the `mods` array by
  default, returning `mod_count` instead; add `?include=mods` for callers that
  want it. `get_server` keeps its current shape — fetching a server is when you
  legitimately want its mods.
- `list_mods` gains a `fields` or `compact` parameter that drops `required_by`,
  `versions` and `thumbnail`. Default the MCP tool to the compact shape; the
  frontend asks for what it needs.

Pure ergonomics, no behaviour change, and it makes every future incident cheaper
to work through.

## Data-model changes

None required. One optional column if F2's `source` refinement is taken:

| Table | Column | Type | Default |
|---|---|---|---|
| `mod_scenarios` | `source` | str(16), nullable | `NULL` (backfill `'api'` where `name IS NOT NULL`) |

## Testing

Backend (`backend/tests/`, self-contained harness per F-fact 12):

1. Pre-flight skips `ENGINE_BUILTIN_GUIDS` entirely — a server whose dependency
   tree includes `58D0FB3206B6F859` emits no check naming it and does not return
   `blocked` on its account.
2. An addon declaring an **older** game version than the engine yields `warn`,
   not `blocked`; one declaring a **newer** version still yields `blocked`.
3. A server whose only findings are warnings returns `verdict: "warn"` — i.e.
   regression-guard that F1 did not simply delete the verdict.
4. Scenario check: API-verified id → no check; `name IS NULL` id → `warn`;
   wrong-GUID-same-path → `warn` **naming the verified id**; unmatched → `warn`.
   The third case seeds exactly the server-32 shape from F-fact 6.
5. Scenario check never returns `blocked` for any of the above.
6. Batch download: one `run_mod_download` call for N guids; the single-GUID
   wrapper still resolves to the same job shape.
7. `tail_log` prefers the newest `logs_*/console.log`, falls back to the
   aggregate when no session dir exists, and refuses a path escaping the profile
   dir.
8. `update_server` omits `mods` by default and includes it with `include=mods`.

Cover exactly these. Frontend is untested; `npm run build` is the only gate.

Manual matrix on the live fleet:

1. Pre-flight on servers 13, 22, 24 and 32 returns no `blocked` — this is the
   headline check for the whole sprint.
2. Temporarily point a scratch server at a mod-GUID-prefixed scenario id;
   pre-flight names the correct resource GUID.
3. Batch-download several mods in one call; one engine spawn in the job log.
4. `tail_log` on a running server returns lines that are still arriving, and on
   a server restart picks up the new session's log, not the previous one's.

## Cut from this sprint / Non-goals

- **Making the supervisor refuse to start on `blocked`.** Tempting once the
  verdict is trustworthy, and still wrong for this sprint: the fleet has to run
  clean for a while first, and an operator locked out of their own server by a
  false positive is a worse failure than the one being fixed. Decision 3 surfaces
  the findings; enforcing them is a later call made with evidence.
- **Auto-correcting `scenario_game_id`.** F2b deliberately warns instead.
  Silently changing which mission a server boots is exactly the class of
  invisible action that caused this incident.
- **Parsing `resourceDatabase.rdb` properly to recover true resource GUIDs
  offline.** The scanner's docstring (`scanner.py:24-31`) already rejects this as
  undocumented-format reverse engineering for no benefit, and the API is
  authoritative. Still true.
- **`diff_server_configs(a, b)`.** Solved the server-32 incident by hand via
  `docker exec` + a hand-written flattener; useful in the moment, but not worth
  a permanent tool surface for how rarely two servers need diffing.
- **A general "compare against a remote server" feature.** The reference-server
  comparison that cracked this incident used a third-party listing site; that is
  research, not a product surface.
- **Investigating the running-server download guard (F-fact 9).** Whether a
  concurrent downloader can safely write into the shared addon cache while a
  server reads from it is a real question, but it is a research spike with no
  guaranteed payoff — the guard stays exactly as it is (Decision 7). Revisit
  only if stop→download→restart cycles become a recurring, measured cost.
- **Retrofitting pre-flight onto scheduled restarts.** Separate concern.
- **Production TrueNAS / SSH.** Out of scope, as always.

## Verification

```bash
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm run build
docker compose up -d --build   # then the manual matrix above at :18090
```
