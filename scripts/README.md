# scripts/

Operational / test helpers that are **not** part of the shipped image
(`.dockerignore` keeps this directory out of the build context).

## `e2e_live.py` — live-stack end-to-end smoke test

Drives a **running** Reforger Manager stack over HTTP. Unlike the pytest suite
(`backend/tests/`, sqlite, no network), this actually installs the Reforger
engine with `steamcmd`, launches the game server as a child process, and
exercises RCON / A2S stats / console-log parsing / the scheduled-restart timer
against it. Nothing has to *connect* to the game server — the manager reaches it
over the container loopback — so the local bridge-networking
`docker-compose.override.yml` is enough.

### Phases

| phase    | what it covers | needs the engine? |
|----------|----------------|-------------------|
| `api`    | server CRUD, draft-config preview, **pin preservation across a mod-set replace**, clone, favourites, modpack CRUD + apply/from-server/export/import, storage, scenarios, settings, backup export + dry-run import | no |
| `engine` | `POST /api/engine/update` → steamcmd `+app_info_update 1 +app_update 1874900 validate`; polls the job to `succeeded`; asserts `installed_build == latest_build`. ~10 GB on a cold box; a warm install just re-verifies (~2 min). | installs it |
| `server` | create a vanilla definition, `POST /start` for real, wait for A2S, then `/stats`, RCON `#say` / `#kick`, `/players`, `/log` (+ `severity` / `hide_spam` / `q` filters), scheduled-restart arm/read/cancel **and a real 40 s fire** with `#say` warnings, then `/stop` and a clean-exit (not "crashed") assertion | yes |
| `mods`   | add a mod by GUID and by Workshop URL, mod detail (versions/deps/tags), `POST /mods/{guid}/download` → real download job → `is_local` + `/api/storage` per-mod bytes + orphan listing, `POST /mods/updates/check` | yes (for the download) |

### Run it

Inside the manager container (has `python3`, reaches the API on loopback):

```bash
docker cp scripts/e2e_live.py reforger-manager:/tmp/e2e_live.py
docker exec -e RM_ADMIN_PASSWORD='<admin pw from .env>' \
  reforger-manager python3 -u /tmp/e2e_live.py --phases api,engine,server,mods
```

Or from any host that can reach the published port:

```bash
python3 scripts/e2e_live.py --base http://localhost:18090/api --password '<pw>'
```

Stdlib only — no `httpx` / `requests`. Exit code is 0 only when every assertion
in every selected phase passed. All definitions/modpacks it creates are named
`e2e-*` and deleted on exit (`--keep` to leave them).

### Useful flags

```
--phases api,engine,server,mods   subset / order of phases (default: all)
--base URL                        API base (default http://127.0.0.1:18090/api)
--password / RM_ADMIN_PASSWORD    admin password (also reads ADMIN_PASSWORD)
--engine-timeout 3600             seconds to wait for the steamcmd job
--boot-timeout 300                seconds to wait for the game server's first A2S reply
--small-mod GUID                  tiny mod for the download test (default: "Where Am I")
--dep-mod GUID                    mod with deps+scenarios (default: RHS - Status Quo)
--scenario '{GUID}Missions/x.conf'  vanilla scenario for the test definition
--keep                            don't delete the e2e-* definitions/modpacks
```

### Notes / gotchas

- **Fresh Postgres**: `data/pg/.gitkeep` makes `postgres:17` refuse to initialise
  an empty data dir (`initdb: directory ... not empty`). Remove it before the
  first `docker compose up`, restore it afterwards (`git checkout data/pg/.gitkeep`).
- The `engine` phase needs `+app_info_update 1` in the steamcmd call or app
  `1874900` fails with *"Missing configuration"* on a cold appinfo cache — that
  argument is in `backend/app/steam/steamcmd.py` and covered by
  `backend/tests/test_steamcmd_args.py`.
- `#restart` reloads the running scenario **in-process** (stable pid); the
  supervisor's schedule fires once and does not re-exec the binary.
