# armaservermanager — archived stack (2026-09-02)

Snapshot of the **old** `fugasjunior/armaservermanager` stack taken immediately before it was
stopped and replaced by `reforger-manager`. Old stack stopped at 2026-09-02 ~02:00 local
(`docker compose stop`; containers left in `Exited` state, `restart: unless-stopped`).

## What replaced it
`Z:\reforger-manager\` — a Reforger-only manager. See `.sprints/<n>/PLAN.md` / `STORIES.md`.

## Identity / routing (unchanged across the migration)
| Item | Value |
|---|---|
| Subdomain | `armaserver.badis.net` (in `Z:\ddns\.env` DOMAINS; Traefik wildcard cert) |
| Old web port | `18080` (host networking, Spring Boot `SERVER_PORT`) |
| New web port | `18090` |
| Game ports | `2001/udp` (bind+public), `17777/udp` (A2S) — unchanged; new stack adds `19999/udp` RCON |
| Traefik dynamic config | `Z:\traefik\dynamic\armaserver.yml` — router `armaservermanager`, service `armaservermanager-svc`, middlewares `sec-headers@file,gzip@file,crowdsec@docker`. Phase 5 repoints the loadBalancer URL `:18080` -> `:18090`. |

## Old stack facts
| Item | Value |
|---|---|
| Image | `fugasjunior/armaservermanager:latest` + `mysql:8.3` (`armaservermanager-db`) |
| Web auth user | `admeen` (AUTH_USERNAME; password in `armaservermanager.env`) |
| DB | name `armaservermanager_db`, user `armaservermanager`, host `3306`, JDBC `jdbc:mysql://localhost:3306/armaservermanager_db` |
| Secrets in env | `JWT_SECRET`, `DATABASE_ENCRYPTION_SECRET` (AES-256 for Steam pw at rest), `STEAM_API_KEY` (Steam Web API key `98C5...`, NOT an account login) |
| TZ | `Europe/Zurich` |
| Compose networks | `armaservermanager-net` (bridge); manager itself `network_mode: host` |

### Storage (TrueNAS, SSD pool `/mnt/Apps/docker/armaservermanager/`)
| Path | Size | Fate |
|---|---|---|
| `steam/servers/REFORGER/` (13x `REFORGER_*.json` + 9.9 GB install) | 9.9 GB | Phase 5: `mv` install -> `reforger-manager/server`; the 13 JSON configs archived here |
| `steam/mods/reforger/` | 19 GB | Phase 5: `mv` -> `reforger-manager/mods` |
| `steam/mods/local/`, `steam/mods/steamapps/` | - | NOT moved; stay with this archive |
| `mysql/` | - | Dumped to `armaservermanager-db-2026-09-02.sql`; volume removed only after cutover verified |
| Compose + `.env` | - | `Z:\armaservermanager\` (archived here as `docker-compose.yml` + `armaservermanager.env`) |

## Archive contents
- `docker-compose.yml` — old stack compose (from `Z:\armaservermanager\`)
- `armaservermanager.env` — old stack `.env` verbatim (secrets included)
- `traefik-dynamic-armaserver.yml` — `Z:\traefik\dynamic\armaserver.yml` as of pre-cutover
- `armaservermanager-db-2026-09-02.sql` — full mysqldump (`--single-transaction --routines --triggers`)
- `REFORGER_configs/REFORGER_1..13.json` — all 13 server definitions

## Rollback (if the new stack has to be abandoned)
1. `cd /mnt/iceberg/docker/armaservermanager && sudo docker compose start`
2. Revert `Z:\traefik\dynamic\armaserver.yml` loadBalancer URL back to `:18080` (Traefik hot-reloads).
3. If Phase 5 `mv` already ran: move `reforger-manager/server` and `reforger-manager/mods` back to
   `steam/servers/REFORGER` / `steam/mods/reforger`.
4. DB is intact unless the `mysql/` volume was removed; if so, restore from the `.sql` dump.

Only **4 of 13** definitions are migrated to the new stack (6, 7, 9, 13). The other 9 live on only
in this archive.
