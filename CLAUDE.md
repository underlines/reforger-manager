# CLAUDE.md — reforger-manager

## What this is

Arma Reforger dedicated-server manager: FastAPI backend + process supervisor
(spawns the Reforger server as a child process), React/Vite SPA, Postgres 17
store (config/mods/jobs). Product context: [README.md](README.md). Sprint
plans/history: `.sprints/`.

## Stack / layout

- `backend/` — FastAPI app under `app/`, pytest suite under `tests/`,
  `requirements.txt`. Async SQLAlchemy; all config is env-only
  (`app/core/config.py`).
- `frontend/` — React 19 + Vite + Tailwind SPA. `npm run build` output
  (`frontend/dist`) is baked into the image and served by FastAPI.
- `Dockerfile` — multi-stage: a `node` stage builds the SPA, the Debian runtime
  stage runs the Python app as an unprivileged `steam` user under `tini`.
- `docker-compose.yml` — `reforger-manager` (app) + `reforger-manager-db`
  (`postgres:17`). `docker-compose.override.yml` is merged automatically and
  holds local-dev settings (bridge networking, `./data` bind mounts).
- `docker/entrypoint.sh` — uid/gid remap + privilege drop.
- `scripts/e2e_live.py` — live end-to-end smoke test against a running stack.

## Run the stack

Docker must be running (`docker info` succeeds).

```bash
cp .env.example .env      # then set real POSTGRES_PASSWORD / JWT_SECRET /
                          # ADMIN_PASSWORD; keep DATABASE_URL in sync
docker compose up -d --build
docker compose logs -f reforger-manager
```

- Web UI / API: `http://localhost:18090` — login `admin` / `ADMIN_PASSWORD`.
- First run auto-creates the schema, the `engine` row and the admin user. The
  Reforger engine binary is **not** installed until an engine install is
  triggered from the UI/API (a multi-GB `steamcmd` download into the server dir).
- Stop: `docker compose down` (keep data) / `docker compose down -v`.

The override runs the manager on the default bridge with only `18090` published.
That fully exercises the manager (UI, API, DB, mod library + Workshop lookup,
pre-flight, engine-build tracking, `steamcmd`) but does **not** publish the
engine's UDP ports. A LAN-joinable game server needs `network_mode: host` (base
file only: `docker compose -f docker-compose.yml up -d`) on a host where Docker
host networking works.

## Frontend dev (hot reload)

`node_modules` is not in the image. The Vite dev server proxies `/api` + WS to
`127.0.0.1:18090` — run the backend first (Docker or bare).

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

A frontend change reaches the container only via `docker compose build`.

## Backend dev (bare)

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # .venv/Scripts on Windows
.venv/bin/python -m pytest                                          # sqlite+aiosqlite, no DB needed
.venv/bin/python -m uvicorn app.main:app --reload --app-dir . --port 18090
```

A bare run needs `SERVER_DIR` / `MODS_DIR` / `PROFILES_DIR` / `CONFIGS_DIR`
pointed at real local dirs — the `.env` defaults are in-container paths
(`/home/steam/data/...`).

## Tests

- Backend: `cd backend && python -m pytest` — `sqlite+aiosqlite`, no Postgres.
- Frontend: `npm run build` (`tsc -b && vite build`) is the only check; no JS
  test harness.
- `scripts/e2e_live.py` drives a running stack (engine install → server start →
  RCON/A2S/log/scheduled-restart → stop; mod download + storage). See
  `scripts/README.md`.

## Config

Read from the environment / `.env` by `app/core/config.py`; `.env.example` is the
full list. Notable: `DB_MIGRATE_ON_STARTUP` (`create_all` default, or
`alembic`), `NIGHTLY_CHECK_ENABLED`, `CORS_ORIGINS` (JSON array — keep
`http://localhost:5173` for `npm run dev`).

`docker-compose.yml` parameterizes every environment-specific value as a
`${VAR}` (dev defaults; `:?` on the three required secrets). It is also the
deploy file — Portainer git-stack + `.github/workflows/ci.yml` webhook; see
README "Deploying". `docker-compose.override.yml` is dev-only and Portainer
does not merge it.

## Disk space

The engine install and the addon cache are large (tens of GB combined) and grow
with use. Compose bind-mounts them to `${DATA_DIR:-./data}` (`data/server/`,
`data/mods/`, `data/pg/`); point `DATA_DIR` at a roomy path so they don't land
inside Docker's VM disk. Reclaim:

```bash
docker compose down
rm -rf data/server/* data/server/.[!.]*      # engine install — re-downloaded on next install
rm -rf data/mods/*   data/mods/.[!.]*        # addon cache
rm -rf data/profiles/* data/configs/*        # per-server logs + generated configs
rm -rf data/pg/* data/pg/.[!.]*              # wipe DB — schema + admin recreated on next start
docker system prune -af --volumes && docker builder prune -af
```

Keep every `data/*/.gitkeep`.

## Implementing a sprint (`.sprints/N/`)

- Read `PLAN.md` + `STORIES.md` first; trust their facts.
- Map story dependencies: independent → parallel, dependent → sequential.
- Verify each unit of work (`pytest` + `npm run build`) before moving on.

## Gotchas

- **Docker daemon must be running** — `docker` commands fail until it is.
- **Postgres won't initialise into a non-empty data dir** — `postgres:17`
  refuses if `data/pg/` contains anything, including the tracked `.gitkeep`. On a
  clean checkout: `rm data/pg/.gitkeep` before the first `up`, restore it after
  (`git checkout data/pg/.gitkeep`). Only the first init checks.
- **Bind-mount I/O is slow on non-Linux hosts** (Docker Desktop's VM boundary) —
  most visible on the large addon cache; fine for dev.
- **Entrypoint chown** — `docker/entrypoint.sh` runs a one-time
  `chown -R steam:steam /home/steam` (marker `data/.rm-owner`); a harmless no-op
  on bind mounts that don't support it. Reappears if `data/` is wiped.
- **No Steam account** — the Reforger server (Steam app `1874900`) installs via
  `steamcmd +login anonymous`. Never add credentials.
- **Single-server rule** — the supervisor runs at most one server definition at a
  time, by design; engine update / verify are blocked while one runs.
- **Never commit `data/` or `.env`** (both gitignored).
- **Profile files are editable via the UI Files tab** (`PROFILES_DIR/{id}/`),
  but `CONFIGS_DIR/{id}.json` is regenerated from the DB by `supervisor.start`
  on every server start — a hand-edit there is silently clobbered, which is why
  the Files tab shows it read-only.
