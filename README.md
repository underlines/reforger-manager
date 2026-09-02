# reforger-manager

Purpose-built **Arma Reforger** dedicated-server manager. Unlike general-purpose
multi-game managers (e.g. `arma-server-manager`, an Arma 3 / DayZ tool with
Reforger bolted on), this is a Reforger-only stack: a server-template system
(many saved definitions; **one runs at a time**), a real mod subsystem (library,
Workshop lookup, dependency resolution, version pinning, pre-flight validation),
engine build tracking, RCON/A2S, and a reactive web UI.

Design context: [.sprints/1/PLAN.md](.sprints/1/PLAN.md) (build) and
[.sprints/2/PLAN.md](.sprints/2/PLAN.md) (close the UI ↔ API gap, finish the
deferred features). **Both sprints are done**; Sprint 2 outcome is in
[.sprints/2/RESULTS.md](.sprints/2/RESULTS.md). Per-story detail:
`.sprints/<n>/STORIES.md`. Local dev / contributor guide: [CLAUDE.md](CLAUDE.md).

## Ports

| | |
|---|---|
| Web UI / API | `18090/tcp` (host; `WEB_PORT`). Front it with a reverse proxy for TLS if you expose it. |
| Game / query / RCON | `2001/udp` (game), `17777/udp` (A2S query), `19999/udp` (RCON) — used by spawned game servers only |

## Containers

| Container | Image | Network | Role |
|---|---|---|---|
| `reforger-manager` | built from `./Dockerfile` (Debian, multi-stage: Node SPA build + Python runtime) | `host` in prod, bridge in the local override | FastAPI API + process supervisor + built SPA; spawns Reforger as child processes |
| `reforger-manager-db` | `postgres:17` | isolated bridge, published on `127.0.0.1:5432` | Config / mod / job store (JSONB) |

Production uses `network_mode: host` on the manager: Reforger is NAT-sensitive
and spawned game servers must bind the host UDP ports directly. Where host
networking is not available (e.g. Docker Desktop), the local override runs the
manager on a bridge with only `18090` published — enough for everything except a
LAN-joinable game server. The manager reaches Postgres over the loopback-only
`127.0.0.1:5432` publish.

## Runtime paths

Bind-mounted host directories under `${DATA_DIR:-./data}` (set `DATA_DIR` in
`.env`; defaults to `./data` in the repo):

| Mount | In container | Contents |
|---|---|---|
| server | `/home/steam/data/server` | Reforger install via `steamcmd` (~10 GB) |
| mods | `/home/steam/data/mods` | Addon cache at `mods/reforger/addons/<Name_GUID>`; the supervisor launches the engine with `-addonDownloadDir …/mods/reforger` so it reads the cache in place instead of re-downloading |
| profiles | `/home/steam/data/profiles` | Per-definition profile + logs |
| configs | `/home/steam/data/configs` | Generated `config.json` per server definition |
| pg | (db container) `/var/lib/postgresql/data` | Postgres 17 data |

The entrypoint remaps the in-image `steam` uid/gid to the bind-mount owner
(`PUID`/`PGID`, else detected, else `1000`), drops privileges with `gosu`, and
runs under `tini`.

## No Steam account

Arma Reforger Server is Steam app **1874900**, installed and updated with
`steamcmd +login anonymous +app_update 1874900 validate`. There is **no Steam
account, no Steam Guard, no 2FA** anywhere in this project — do not add
credentials.

## Single-server rule

Many server definitions can be saved, but **only one runs at a time**. The
supervisor refuses to start a second definition while one is up, with a clear
message rather than a silent failure. Engine updates and verify/repair are also
blocked while a server runs.

## Engine-update behaviour

- **Build-id tracking.** Detection keys on the numeric Steam `buildid`, not the
  display version. Installed build is read from
  `server/steamapps/appmanifest_1874900.acf` (`"buildid"`). The available
  public-branch build comes from `GET https://api.steamcmd.net/v1/info/1874900`
  → `depots.branches.public.buildid` (plain HTTP, no steamcmd, no writes);
  fallback is `steamcmd +app_info_update 1 +app_info_print 1874900` (retry once
  on empty). Both persist to the `engine` singleton row. The display version
  (e.g. `1.8.0.10`) is best-effort scraped from the `console.log`
  `Creating game instance(...), version ...` line at server start — display only.
- **Blocked while running.** No engine update starts while a server is up; the
  UI offers "update after this server stops". The server binary is never
  pinnable — clients are force-updated by Steam and cannot join an older build,
  so its only update path is forward.
- **Post-update pre-flight sweep.** Completing an engine update writes the new
  build to `engine`, then automatically re-runs pre-flight across **every**
  server definition and re-validates every mod pin against the new build. The
  result is a plain report: which servers are still green, which mods now have a
  `gameVersion` mismatch, which pins went stale (held against a build that is no
  longer installed).

## Build & deploy

```bash
cp .env.example .env       # set real secrets; keep DATABASE_URL in sync with POSTGRES_*
docker compose up -d --build
docker compose logs -f reforger-manager
```

Compose files and `Dockerfile` are at the repo root. `docker-compose.override.yml`
is merged automatically and carries local-dev settings (bridge networking,
`./data` bind mounts). On a production host with working host networking, run the
base file only: `docker compose -f docker-compose.yml up -d`.

The `reforger-manager` image is multi-stage: a `node` stage runs `npm ci && npm
run build` for the SPA, then the runtime stage copies `frontend/dist` in and
serves it from FastAPI (`app/main.py` mounts `/assets` and falls back to
`index.html` for client-side routes — the frontend uses history-mode
`BrowserRouter`). There is no separate frontend container and no bind mount over
`/app`, so **frontend changes require an image rebuild**. Dev workflows:
[CLAUDE.md](CLAUDE.md), `frontend/README.md`. Live end-to-end test against a
running stack: `scripts/e2e_live.py` (`scripts/README.md`).

### API auth for manual calls

```bash
B=http://127.0.0.1:18090
TOKEN=$(curl -s -X POST $B/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<ADMIN_PASSWORD from .env>"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" $B/api/servers            # list definitions
curl -s -X POST -H "Authorization: Bearer $TOKEN" $B/api/servers/1/start
```

WebSockets (`/api/jobs/stream`, `/api/servers/{id}/console`, `/api/servers/{id}/stats`) take the JWT
as `?token=` since browsers can't set the header on a WS handshake.

### Config / env

`.env` (git/dockerignored, real secrets) carries Postgres creds, `JWT_SECRET`, `ADMIN_USERNAME` /
`ADMIN_PASSWORD`, ports and the in-container bind-mount paths — see `.env.example` for the full
list. Optional: `NIGHTLY_CHECK_ENABLED=true` (+ `NIGHTLY_CHECK_HOUR`, `NIGHTLY_CHECK_INTERVAL_SECONDS`)
turns on the nightly engine-check + mod-update-check + cache-scan cycle (default off).
`DB_MIGRATE_ON_STARTUP` is `create_all` (default) or `alembic`. `CORS_ORIGINS` is a JSON array —
keep `http://localhost:5173` for `npm run dev`.

### Pre-flight validation

`backend/app/servers/preflight.py` grades each definition `ok` / `warn` / `blocked` before start.
Two intentional refinements over a naive check:

- An `unlisted` / `private` / `obsolete` mod that is **already installed locally** warns instead of
  blocking.
- The engine-version check (`_version_compat`) only **blocks** on a newer declared engine, a
  different major, or ≥3 minors behind; it **warns** at 1–2 minors behind.

An unresolvable mod (Workshop 404 with no local dir) blocks; a phantom `.gproj` dependency (parent
resolves, dependency GUID does not) warns.

### Sprint 2 — delivered ([.sprints/2/RESULTS.md](.sprints/2/RESULTS.md))

All 19 code stories landed on `main`. The web UI is now a full surface over the API.

- **Server definitions** — create / edit / delete / **clone** in the UI
  (`POST` / `PATCH` / `DELETE /api/servers`, `POST /api/servers/{id}/clone`). Config tab is a
  hybrid form (structured common fields + collapsed Advanced with two validated raw-JSON
  editors), debounced live preview (`POST /api/servers/{id}/config/preview`, no persistence),
  "applies on next start" banner while running. **Favourites** (star toggle, sort first).
  **Scenario picker** (`POST /api/scenarios/resolve`, DB-first, Workshop only for uncached
  GUIDs) with a free-text override.
- **Mod-set editing** — searchable add-from-library with dependency preview, remove,
  enable/disable, `@dnd-kit` drag order, inline pin/unpin, one `PATCH` save, pre-flight
  re-runs. Per-server pins survive a mod-set replace unless the payload overrides them.
- **Mods** — "add by URL / 16-hex GUID" + inline Workshop search (`POST /api/mods/add`,
  `GET /api/mods/search?q=`). Mod detail `/mods/:guid`: summary, tags, version history
  (+ `gameVersion`), resolved dependency tree, `used_by`, pin/unpin, verify. Force
  re-download (`POST /api/mods/{guid}/download`) behind a free-space guard (NULL `Mod.size`
  ⇒ 409) shared with `updates/apply`.
- **Storage** — `GET /api/storage`: per-mod on-disk sizes, free/total, `orphans` and
  `kept_as_dependency` (orphan = local, unreferenced, and absent from the offline dependency
  closure of every assigned/packed mod). `DELETE /api/mods/{guid}/local` removes files, keeps
  the row.
- **Modpacks** — real page + CRUD API (`/api/modpacks`), drag order, apply-to-server
  (`replace` reports dropped pins, `append` never unpins), create-from-server, export /
  import (`on_conflict=rename|replace|error`).
- **Players & scheduled restart** — Players tab (name/id/IP/ping, auto-refresh), per-row
  Kick/Ban (`#kick` / `#ban`) behind a confirm. One cancellable in-memory restart schedule
  per server (`#say` at each `warn_at`, then `#restart`), reload-surviving countdown
  (`POST` / `GET` / `DELETE /api/servers/{id}/schedule-restart`).
- **Runtime settings** — `app_settings` singleton (Alembic `0002`). `GET` / `PATCH
  /api/settings` (nightly check enabled/hour, log-spam patterns; live), `POST
  /api/auth/password` (verify current, keep session). Console spam filter is DB-driven end to
  end (WebSocket `is_spam`).
- **Backup** — `GET /api/backup/export` (definitions + `server_mods` incl. pins and
  **cleartext passwords** + modpacks; no runtime state / `engine` / mod library).
  `POST /api/backup/import?dry_run=&on_conflict=skip|replace` returns a per-name plan; a
  running server is forced to `skip`.
- **CORS** — `cors_origins` default is now an explicit allow-list, not `["*"]` (`CORS_ORIGINS`
  in `.env.example`).

Only runtime deps added: `@dnd-kit/core` + `/sortable` + `/utilities`.

**Cut by design:** config-revision diff / rollback — the stored snapshot is the lossy
generated `config.json`; **clone** (duplicate, edit the copy, delete if bad) replaces it.

### Post-Sprint-2 follow-ups

- **Live E2E harness** — `scripts/e2e_live.py` drives a *running* stack over HTTP (steamcmd
  engine install → real server start → RCON / A2S / console-log / scheduled-restart → stop;
  real Workshop mod download → `/api/storage`). See `scripts/README.md`. Four bugs it
  surfaced, all fixed:
  1. Engine install aborted on a cold steamcmd appinfo cache — added `+app_info_update 1`
     before `+app_update`.
  2. First-ever force-download of any mod always 409'd — `enrich_one` never stored
     `Mod.size`, so the free-space guard saw NULL. Now taken from the Workshop object
     (fallback: newest version's size).
  3. Headless mod downloader rejected by the 1.8.0.10 engine (`scenarioId: ""`) then hung on
     a block-buffered stdout pipe — valid placeholder scenario; now follows
     `<profile>/logs/*/console.log` with timeouts.
  4. Downloaded addons landed in `<mods_dir>` not `<mods_dir>/reforger/addons` — fixed
     `-addonDownloadDir`; the `mod_download` job now runs a targeted `refresh_local_mods` on
     success so the row flips `is_local` immediately.
  Backend suite: 143 → 155.
- **Jobs page** — rows show relative "started / last update" times (full timestamp on hover);
  detail dialog gains "Last update"; the WS stream replay carries the timestamps.
- **Stale library rows** — `DELETE /api/mods/{guid}` deletes a mod's library row outright
  (+ files if local), guarded like `.../local`. `GET /api/storage` adds `unreferenced_entries`
  (rows with no files and no references — invisible to the disk-only `orphans` list),
  surfaced on the Storage page and mod detail as "Remove from library".
