# reforger-manager

reforger-manager is a self-hosted manager for **Arma Reforger** dedicated
servers: a single Docker stack (FastAPI + process supervisor, React web UI,
Postgres) that installs the engine, stores many server definitions, and runs one
of them as a supervised game-server process with live console, RCON and A2S. It
ships a complete mod subsystem — a local library synced with the Steam Workshop,
transitive dependency resolution, per-server version pinning, and pre-flight
validation — plus engine build-id tracking and a scheduled-restart system.

It exists because the general-purpose multi-game panels (Pterodactyl, LinuxGSM,
`arma-server-manager` and similar) treat Reforger as an afterthought bolted onto
an Arma 3 / DayZ workflow. They cover "spawn a process with these flags" and
leave the genuinely awkward parts as manual work: the anonymous-only Steam
install, the Workshop mod dependency graph, build-id-based update detection, mod
pins that silently go stale after an engine update, and the constraint that only
one server can bind the host's UDP ports at a time. Here each of those is a
first-class feature with both a UI and an API, and the tool enforces Reforger's
real limits instead of pretending they aren't there.

## Features

- **Server definitions** — save many server configs (network, RCON, game
  settings, ordered mod set); create / edit / clone / delete, favourites, and a
  hybrid structured + raw-JSON config editor with debounced live preview. Only
  one definition runs at a time.
- **Supervised game server** — start / stop a Reforger process as a managed
  child; live console log with DB-driven spam filtering, player list over RCON,
  session stats over A2S.
- **Mod library + Workshop** — add by URL or 16-hex GUID, inline Workshop
  search, cached metadata, version history with `gameVersion`, resolved
  dependency trees, `used_by` back-references.
- **Dependency resolution & pinning** — transitive dependency closure per
  server; inline per-server version pins that survive mod-set edits and are
  re-validated against the installed engine build.
- **Pre-flight validation** — every definition graded `ok` / `warn` / `blocked`
  before start, distinguishing unresolvable mods, stale pins, engine-version
  mismatches and phantom `.gproj` dependencies.
- **Engine build tracking** — build-id-based update detection via the public
  Steam API / steamcmd; updates blocked while a server runs; a post-update sweep
  re-runs pre-flight and re-checks every pin across all definitions.
- **Modpacks** — reusable named mod lists with drag order; apply-to-server
  (replace / append), create-from-server, JSON export / import.
- **Storage management** — per-mod on-disk sizes, free / total, orphan and
  unreferenced-row detection, delete local files while keeping the library entry.
- **Players & scheduled restart** — players tab with kick / ban; one cancellable
  in-memory restart schedule per server with `#say` warnings and a
  reload-surviving countdown.
- **Profile file browser** — per-server "Files" tab: list / view / whole-file
  edit (JSON-validated) / upload / mkdir / rename / recursive delete / download
  (incl. zip); read-only while that server runs.
- **Backup** — export every definition, mod set, pin and modpack as one JSON
  document; dry-run import with a per-name conflict plan.
- **Runtime settings** — nightly engine-check toggle / hour, console spam
  patterns, admin password change — all applied live.
- **Agent interface (MCP)** — the whole admin surface as MCP tools over HTTP,
  mirroring the REST API 1:1, with a `confirm` gate on every state-changing tool.
- **Anonymous Steam** — engine install / update via `steamcmd +login anonymous`;
  no Steam account, Steam Guard or 2FA anywhere.

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

## Profile files

A "Files" tab on each server's detail page browses and edits that server
definition's profile directory (`PROFILES_DIR/{id}/`) — list, view, whole-file
text edit (with JSON parse-validation when the file ends in `.json`), upload,
new folder, rename, recursive delete, per-file download, and "download all as
.zip". It deliberately does **not** reach `SERVER_DIR` or `MODS_DIR` (engine /
addon cache, steamcmd-owned), does not write `CONFIGS_DIR/{id}.json`
(regenerated from the DB on every start — shown read-only), and hides `logs/`
(use the Console/Log tab) and `addons_tmp/` (engine scratch).

Reads and browsing are always allowed. Write, delete, mkdir, rename and upload
return HTTP 409 while that server definition is the one currently running —
stop it first. There is no automatic backup or version history on overwrite; a
bad edit is only recoverable from NAS-side ZFS snapshots of the bind mount.

## Connect an agent

The manager exposes its full admin surface as MCP tools over **streamable HTTP**
at `https://<host>/mcp`; point any MCP client there with an
`Authorization: Bearer <token>` header. The tools mirror the REST API 1:1, plus
three read-only composites (`server_overview`, `tail_log`, `wait_for_job`), and
every state-changing tool requires an explicit `confirm` flag.

Create a token in the web UI under **Settings → MCP Tokens**. The raw `rfm_…`
value is shown once and only its hash is stored; a token is full admin, so treat
it like the web UI password and revoke it from the same place.

**Reverse-proxy note.** `/mcp` responses are streamed (SSE) — a proxy in front
of the manager must not buffer them (nginx: `proxy_buffering off` for the
location, or send `X-Accel-Buffering: no`).

## Deploying

`docker-compose.yml` is the deployment file: `network_mode: host` (Reforger needs
the host UDP ports), `build: .`, and every environment-specific value is a
`${VARIABLE}` with a dev default. Supply real values three equivalent ways — a
`.env` file next to the compose (auto-loaded), the shell environment, or a
Portainer stack's *Environment variables*.

```bash
cp .env.example .env         # set at least the three REQUIRED vars below
docker compose -f docker-compose.yml up -d      # production shape (host networking)
```

### Variables

| Variable | Default | Notes |
|---|---|---|
| `DATA_DIR` | `./data` | Absolute host path for bind mounts — Reforger install (~10 GB), addon cache (tens of GB), Postgres. **Always set this on a server**; the `./data` default would fill the stack's working dir. |
| `POSTGRES_PASSWORD` | — **required** | deploy refuses to start if unset |
| `JWT_SECRET` | — **required** | rotating it invalidates all sessions |
| `ADMIN_PASSWORD` | — **required** | used once to create the `admin` user on an empty DB |
| `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_VERSION` | `reforger` / `reforger_manager` / `17` | |
| `DATABASE_URL` | built from the `POSTGRES_*` vars @ `127.0.0.1:5432` | set only if the DB is elsewhere |
| `ADMIN_USERNAME` | `admin` | ignored after first boot |
| `TZ` | `UTC` | |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | JSON array; add your served origin |
| `DB_MIGRATE_ON_STARTUP` | `create_all` | or `alembic` |
| `ENGINE_CHECK_ON_STARTUP` / `NIGHTLY_CHECK_ENABLED` / `NIGHTLY_CHECK_HOUR` / `JWT_EXPIRE_HOURS` | `true` / `false` / `3` / `12` | |
| `COMPOSE_PROJECT_NAME` / `RESTART_POLICY` / `DB_BIND_ADDR` / `DB_PORT` | `reforger-manager` / `unless-stopped` / `127.0.0.1` / `5432` | rarely changed |

TLS termination and routing are out of scope for this compose — put a reverse
proxy in front and point it at `http://<host>:18090`.

### CI / auto-deploy

`.github/workflows/ci.yml` runs `pytest` + `npm run build` on every push and PR.
Continuous deployment is left to your infrastructure — for example a Portainer
git-stack that rebuilds the image on a webhook POSTed from a GitHub Actions job
on `main`. The build then happens on the deploy host (`build: .`), so it takes a
few minutes.

### Image internals

Multi-stage: a `node` stage runs `npm ci && npm run build`, then the runtime
stage copies `frontend/dist` in and FastAPI serves it (`app/main.py` mounts
`/assets`, falls back to `index.html` for client-side routes). No separate
frontend container, no bind mount over `/app` — **frontend changes need an image
rebuild**. Dev workflows: [CLAUDE.md](CLAUDE.md), `frontend/README.md`. Live
end-to-end test against a running stack: `scripts/e2e_live.py`
(`scripts/README.md`).

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

### Pre-flight validation

`backend/app/servers/preflight.py` grades each definition `ok` / `warn` / `blocked` before start.
Two intentional refinements over a naive check:

- An `unlisted` / `private` / `obsolete` mod that is **already installed locally** warns instead of
  blocking.
- The engine-version check (`_version_compat`) only **blocks** on a newer declared engine, a
  different major, or ≥3 minors behind; it **warns** at 1–2 minors behind.

An unresolvable mod (Workshop 404 with no local dir) blocks; a phantom `.gproj` dependency (parent
resolves, dependency GUID does not) warns.

## Development

[CLAUDE.md](CLAUDE.md) is the contributor guide: stack layout, how to run the
backend tests (`pytest` on sqlite — no database needed) and the Vite dev server,
and the project's gotchas. Non-trivial changes are planned as numbered sprints
under `.sprints/<n>/` — a scoped `PLAN.md`, a codebase-detailed `STORIES.md`
split into delegable units, and a `RESULTS.md` written when the work lands.
