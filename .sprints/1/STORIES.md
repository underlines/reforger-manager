# Reforger Manager — implementation phases

> Context, architecture, and feature set live in [PLAN.md](PLAN.md).

> **Approval gate — see `Z:\CLAUDE.md`.** This is a production homelab. Every state-changing step
> below (`docker compose stop`, `mysqldump`, the Phase 5 `mv`, repointing Traefik, and any removal of
> containers/images/volumes) requires **explicit, step-by-step user confirmation immediately before
> it runs**. Approval for one step never carries to the next. Everything else — reading configs,
> `docker ps`, `zfs list`, scanning mod dirs — is free.

## Phase 0 — Archive the old stack *(nothing deleted yet)*

- **Take a manual ZFS snapshot of `Apps/docker` first.** Dailies run at 08:00 with 14 retained, so
  depending on the hour the newest rollback point can be ~17 h old and predate all of this work.
- `docker compose stop` the old stack (**ask first** — it is `restart: unless-stopped` and currently
  `Up (healthy)`; it holds `18080` until stopped); `mysqldump` the DB (`armaservermanager-db`,
  `mysql:8.3`, host `3306`).
- Build `Z:\_archive\armaservermanager-2026-09-02.zip` containing: **all 13** `REFORGER_*.json`
  configs, `docker-compose.yml`, `.env`, `traefik/dynamic/armaserver.yml`, the SQL dump, and a
  `README.md` recording subdomain (`armaserver.badis.net`), web port `18080`, game ports
  `2001`/`17777`, storage paths, auth user, and the DB/JWT/encryption settings.
- Large game data is **retained on disk** for Phase 5 import; removed only after cutover verifies.

## Phase 1 — Container base

- Dockerfile: Debian trixie, `dpkg --add-architecture i386`, `lib32gcc-s1 libcurl4 libssl3
  net-tools ca-certificates gosu python3`, steamcmd into `/home/steam/steamcmd`.
- Entrypoint modelled on the existing one (uid/gid remap + marker file + `gosu steam`).
- steamcmd wrapper installs/updates app `1874900` with `+login anonymous`, streaming progress as a
  job. **No Steam account, no Steam Guard, no credential storage anywhere** — the old stack's
  `login madit79` + 2FA path is not carried over, and its failure mode disappears with it.
  This path must work from empty, so a future clean install needs no manual steps.
- **Engine build tracking** — installed `buildid` parsed from `steamapps/appmanifest_1874900.acf`
  (confirmed: `"buildid" "24501482"`); available build from `GET
  https://api.steamcmd.net/v1/info/1874900` → `depots.branches.public.buildid` (confirmed, matches,
  no steamcmd and no writes). steamcmd `+app_info_update 1 +app_info_print 1874900` is the fallback
  only — retry once on empty output. Persist both to the `engine` singleton, keyed on **buildid**,
  not on the version string. Scrape the display version `1.8.0.10` from the `console.log`
  `Creating game instance(…), version …` line at server startup; display only. Update never runs
  while a server is up.

## Phase 2 — Backend core

- FastAPI + async SQLAlchemy + Alembic; job queue with progress events over WebSocket.
- Config generator: DB → Reforger `config.json` (including the `rcon` block).
- Process supervisor: **enforces a single running server**, graceful SIGTERM, crash detection,
  log tailing. Launch args: `-config -profile -addonDownloadDir -addonTempDir -logStats 30000
  -nothrow -maxFPS 60 -logLevel normal`.

## Phase 3 — Mod subsystem

- Workshop API client (`/mods/{id}`, `/versions`, `/dependencies`, `/scenarios`, `/mods?q=`) with
  aggressive local caching and rate-limit backoff — 60/min, **burst 20**, 20k/day, budget shared
  per client IP, so one container-wide limiter. Map camelCase (`gameMode`, `playerCount`) to columns.
  Treat **HTTP 404 as `api_state = not_found`** — deleted/blocked/private are indistinguishable, so
  never claim a specific reason.
- Local scanner over the 22 mod dirs in `mods/reforger/addons/` (`Name_GUID`):
  - `meta` — `utf-8-sig` JSON, everything under a top-level `meta` object; version at
    `meta.versions[0].version`, size at `meta.versions[0].package.totalSize`. Ignore its
    `scenarios`/`dependencies` arrays — they are empty even when the mod has both.
  - `addon.gproj` — ENFUSION key-value block (not JSON); `Dependencies { "GUID" "GUID" }` is the
    authoritative dependency source.
  - `resourceDatabase.rdb` — **`strings`+regex scan for `^Missions/.*\.conf$` only**, no RDBC record
    parser. Build the gameId as `{meta.id}` + path. (Full record parsing needs undocumented-format RE
    for no benefit.)
  - Also read `ServerData.json` (`revision.version`) and use `thumbnail.png` for the library tile.
- Headless downloader/updater: synthetic config → run engine → parse download progress → SIGTERM at
  `Required addons are ready to use.`
- Verify/repair job; update checking.
- **Dependency resolution with a local fallback**: API `/dependencies` when the mod resolves; when it
  404s, fall back to the on-disk `addon.gproj` GUID list — otherwise a blocked parent (server 9)
  yields no dependency information at all. The old `REFORGER_*.json` files carry only top-level mods.
- **Mod pinning** (mods only — the binary is never pinned): pin/unpin at library and per-server level,
  storing `pinned_at_build` + reason. Pre-flight compares each mod's `gameVersion` against
  `engine.installed_build` and flags pins taken against an older build as **stale**, with
  unpin-and-update as the offered fix.
- **Post-engine-update trigger**: on a completed engine update, write the new build to `engine`, then
  re-run pre-flight across all server definitions and re-validate every pin; emit a summary report.

## Phase 4 — Frontend

- Pages: Dashboard, Servers, Server detail (Config / Mods / Console / Players / History),
  Mod Library, Modpacks, Jobs, Settings.
- Dashboard shows the installed engine build always, plus an **update badge** when a newer public
  build exists; mod rows show pin state and a stale-pin warning.
- Live console and job progress over WebSocket; drag-and-drop load order; light/dark.

## Phase 5 — Migration & cutover

- `mv` the old `steam/servers/REFORGER` (9.9 GB) → `reforger-manager/server` and `steam/mods/reforger`
  (19 GB) → `reforger-manager/mods` — **ask before running**; verified instant, same ZFS dataset
  (`Apps/docker`, no child boundary, 823 GB free). Confirm a same-day snapshot exists first.
  Note `steam/mods/` also holds `local/` and `steamapps/`, which are *not* moved — they stay with the
  Phase 0 archive.
- Scan the mod dirs into the library and enrich from the API — the full cache moves regardless of how
  many definitions we migrate.
- **Migrate 4 of the 13 definitions**, not all. Two healthy, two broken, so both the happy path and
  the failure detection get real coverage:
  - `badis | BattleGen High Command` (6) and `badis server | reaper kingmaker` (13) — expect green.
  - `CO-OP Conflict PVE` (7) — expect a *Ronin AI* (`6294F6D5EDD5CA66`) mismatch: latest `1.0.27`
    declares `gameVersion 1.2.1.173`. Re-confirmed still broken 2026-09-02.
  - `Conflict Kunar PVE RHS-WCS` (9) — mod `658756C5760E94DE` 404s on the API; expect
    "unresolvable on Workshop" **plus dependency GUIDs read from the local `addon.gproj`**.
    Re-confirmed still broken 2026-09-02.
- Hand-create these four rather than building a 13-file importer; a bulk `REFORGER_*.json` importer
  stays a nice-to-have, off the critical path. The remaining 9 live on in the Phase 0 archive.
- Run pre-flight on all four and confirm the expected verdicts above.
- Repoint Traefik: one-line change in `Z:\traefik/dynamic/armaserver.yml`,
  `http://192.168.1.10:18080` → `:18090` (**ask first**). Router rule, middlewares
  (`sec-headers@file`, `gzip@file`, `crowdsec@docker`) and the wildcard cert are unchanged.
- Verify `https://armaserver.badis.net`, then — **only after the user confirms cutover succeeded,
  as a separate approval** — remove the old containers, image, and MySQL volume.
- **Update the homelab docs** (required by `Z:\CLAUDE.md`, and missing from the original plan):
  - swap the `armaservermanager/` row for a `reforger-manager/` row in CLAUDE.md's *Deployed Apps*
    table, keeping the `armaserver.badis.net` URL;
  - write `Z:\reforger-manager\README.md` with the app-specific detail (ports, paths, RCON, the
    single-server rule, engine-update behaviour), per the per-app README convention.
