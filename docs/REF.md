# Reforger Manager — shared build reference (orchestrator notes)

Authoritative spec: `.sprints/1/PLAN.md` + `.sprints/1/STORIES.md` (sprint 1); `.sprints/2/PLAN.md` (sprint 2).
Homelab conventions: `Z:\CLAUDE.md`.

## HARD SAFETY BOUNDARY (non-negotiable — this is a live production NAS)
- You may freely CREATE and EDIT files under `Z:\reforger-manager\` only.
- DO NOT touch anything else on `Z:\` except reading. DO NOT edit `Z:\traefik\...`, `Z:\armaservermanager\...`, etc.
- DO NOT run: `docker build`, `docker compose up/down/start/stop/restart`, `docker rm/rmi`, `zfs *`,
  `rm`/`mv` outside scratchpad, `apt`, or anything over `ssh truenas` that changes state.
- Read-only `ssh truenas` (cat/ls/stat/`sudo docker inspect`/`sudo docker run --rm --entrypoint cat ...`) is fine.
- If you think a build/run is needed to verify, STOP and say so in your report — the orchestrator gates it.

## Verified environment facts (already confirmed live, do not re-verify)
- Old stack STOPPED as of 2026-09-02. Ports 2001/17777/19999 + 18090 are FREE. Old data retained on disk.
- Reforger server app id: **1874900**, anonymous-installable (`steamcmd +login anonymous +app_update 1874900 validate`). NO Steam account / Guard / 2FA anywhere.
- Installed engine build **24501482** (`public` branch), display version `1.8.0.10`.
  - installed buildid: `steamapps/appmanifest_1874900.acf` -> `"buildid" "24501482"`
  - latest buildid: `GET https://api.steamcmd.net/v1/info/1874900` -> `.data["1874900"].depots.branches.public.buildid` (plain HTTP, no writes)
  - fallback: `steamcmd +login anonymous +app_info_update 1 +app_info_print 1874900` (retry once on `{}`)
  - display version only from console.log `Creating game instance(...), version X built ...` at server start
- Base OS target: **Debian 13 (trixie)**. Old image was trixie too. `python3` NOT in old image.
- Binary flags present: `-addonsVerify -addonsRepair -addonsChecksum -addonsForceLocal -addonTempDir -addonDownloadDir -addonsDir -logStats -nothrow -logsDir -loadSessionSave`. NOT present: `-listScenarios`, `-autoreload`.
- Supervisor launch args: `-config <f> -profile <d> -addonDownloadDir <d> -addonTempDir <d> -logStats 30000 -nothrow -maxFPS 60 -logLevel normal`
- Mod download progress lines (parseable):
  `Addon Download started <id> - <name>` / `<name>: [====>____] 20% 1224/6040 MB` /
  `Required addons are ready to use.` (SIGTERM here for headless updater) /
  `NETWORK: Starting RPL server, listening on 0.0.0.0:2001`
- Workshop API base: `https://api.reforgermods.net/v2` — endpoints `/mods/{id}`, `/mods/{id}/versions`,
  `/mods/{id}/dependencies`, `/mods/{id}/scenarios`, `/mods?q=` (NOT `?search=`), `/rate-limits`.
  Rate limit: 60/min, burst 20, 20000/day, scoped by client IP => ONE container-wide limiter.
  **HTTP 404 => api_state=not_found** (deleted/blocked/private are indistinguishable — never claim a reason).
  versions[]: version, gameVersion, size, approved, published, createdAt, scenarioCount, dependencyCount
  scenarios[]: gameId (`{GUID}Missions/x.conf`), name, gameMode, playerCount  (camelCase -> snake_case cols)
  dependencies[]: id, name, version, published, private, workshopUrl
- Mod dirs on disk: `mods/reforger/addons/<Name_GUID>/` (22 of them). Per dir:
  - `meta` — read as `utf-8-sig`, JSON, everything under top-level `meta` object.
    version = `meta.versions[0].version`; size = `meta.versions[0].package.totalSize`.
    `meta.id` = the dir GUID. IGNORE `meta.versions[0].scenarios` and `.dependencies` (empty even when non-empty upstream).
    Present & useful: `deleted`, `unlisted`, `name`, `summary`, `tags`.
  - `addon.gproj` — ENFUSION `GameProject { ... }` key=value block, NOT JSON. Authoritative deps:
    `Dependencies { "58D0FB3206B6F859" "61B514B96692C049" }` (bare quoted GUIDs, space-separated, no names).
  - `resourceDatabase.rdb` — do NOT write an RDBC record parser. `strings` + regex `^Missions/.*\.conf$` only.
    Build gameId = `{` + meta.id + `}` + matched path.
  - `ServerData.json` — BOM JSON: `id`, `name`, `revision.version` (simplest version source).
  - `<file>_<version>_manifest.json` — version/size/sha512/fragments (integrity). `thumbnail.png` for tiles.
  - NAS has no `xxd`/`file`; use `od`/`strings`.
- RCON: native `rcon` block in config.json, default port 19999, perms admin/monitor, cmds `#players #kick #ban #say #restart #shutdown`. (Reforger RCON = BattlEye protocol.)
- A2S live query on 17777 for player count / map.

## Reference: existing entrypoint (adapt, do NOT copy verbatim — new paths)
```sh
#!/bin/sh
set -e
DATA_DIR="/home/steam/armaservermanager"
DET_UID="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || echo 0)"
DET_GID="$(stat -c '%g' "$DATA_DIR" 2>/dev/null || echo 0)"
TARGET_UID="${PUID:-$DET_UID}"; TARGET_GID="${PGID:-$DET_GID}"
[ "$TARGET_UID" = "0" ] && TARGET_UID=1000
[ "$TARGET_GID" = "0" ] && TARGET_GID=1000
MARKER="$DATA_DIR/.asm-owner"; WANT="${TARGET_UID}:${TARGET_GID}"
groupmod -o -g "$TARGET_GID" steam
usermod  -o -u "$TARGET_UID" -g "$TARGET_GID" steam
chown steam:steam /home/steam "$DATA_DIR" ... 2>/dev/null || true
if [ "$(cat "$MARKER" 2>/dev/null)" != "$WANT" ]; then chown -R steam:steam /home/steam; echo "$WANT" > "$MARKER"; fi
exec gosu steam "$@"
```
Old image: ENTRYPOINT `/usr/local/bin/docker-entrypoint.sh`, CMD `java -jar ./app.jar`, USER root, WORKDIR /home/steam.

## Runtime paths (bind mounts; SSD pool, NOT in Z: share)
`/mnt/Apps/docker/reforger-manager/{server,mods,profiles,configs,pg}`
- `server/` — Reforger install (moved from old `steam/servers/REFORGER` in Phase 5)
- `mods/`   — addon cache (moved from old `steam/mods/reforger` in Phase 5; engine expects `<mods>/reforger/addons/<Name_GUID>`)
- `profiles/` — per-server profile + logs
- `configs/`  — generated config.json per server definition
- `pg/`       — Postgres 17 data

## Routing / ports
- Keep `armaserver.badis.net`. Phase 5 repoints `Z:\traefik\dynamic\armaserver.yml` `:18080` -> `:18090`.
- New web port **18090**. Game: `2001/udp` + `17777/udp` + `19999/udp` (RCON). Container is `network_mode: host`.

## The 4 definitions migrated in Phase 5 (from archived REFORGER_*.json)
- #6 `badis | BattleGen High Command` — scenario `{45FCF596CD2409EB}Missions/BattleGen_eden.conf`,
  mods `615F07D39925670F` (BattleGen - High Command), `595F2BF2F44836FB` (RHS - Status Quo). Expect GREEN.
- #13 `badis server | reaper kingmaker` — scenario `{806FFA8093F22A71}Missions/REAPER_Kingmaker.conf`,
  mods `5EB139459EBF5C16`, `61B8FA7B3BF8656B`, `6576A4DF3F71360C`. maxPlayers 4. Expect GREEN.
- #7 `badis | CO-OP Conflict PVE [Friendly AI]` — scenario `{6DD58790690E9D29}Missions/ConflictPVERemixedVanilla2_US.conf`,
  mods `62E960A6A1BA0985` (CO-OP Conflict PVE), `595F2BF2F44836FB`. Expect PRE-FLIGHT FAIL:
  dep Ronin AI `6294F6D5EDD5CA66` latest `1.0.27` declares gameVersion `1.2.1.173` vs our `1.8.0.10`.
- #9 `badis | Conflict Kunar PVE RHS-WCS` — scenario `{43EF352F695C7676}Missions/Conflict_RHS_AutoAI_Kunar.conf`,
  mod `658756C5760E94DE` (404s on API). Expect PRE-FLIGHT FAIL: "unresolvable on Workshop" + list
  missing dep GUIDs from the local `addon.gproj`.
Common config.json shape (old): bindPort/publicPort 2001, a2s.port 17777, game.password "9999",
game.passwordAdmin "999999", gameProperties {serverMaxViewDistance 2500, battlEye true/false, ...}.

## Archive (Phase 0 done)
`Z:\_archive\armaservermanager-2026-09-02.zip` + `Z:\_archive\armaservermanager-db-2026-09-02.sql`.

## LIVE-VERIFIED ADDENDA (orchestrator, 2026-09-02, post-Phase-0)

### Workshop API response envelope
All responses wrap payload: `{"status":"success","data":{ ... }}`. `/versions` -> `data.versions[]`,
`data.count`. Version objects also carry `sizeFormatted`, `apiUrl`. 404 body is not JSON-guaranteed —
key on HTTP status. `/rate-limits` confirmed: `{plan:free, limit_per_minute:60, burst:20,
limit_per_day:20000, shared_by:"client_ip"}`. Reachable from both the Windows host and (expected) the container.
Ronin AI `6294F6D5EDD5CA66`: latest `1.0.27` gameVersion **`1.2.1.173`** (engine is `1.8.0.10`) — mismatch is real.
`658756C5760E94DE` -> **404** confirmed. `595F2BF2F44836FB`, `615F07D39925670F` -> 200.

### Local mod scanner facts (verified against real dirs)
- `mods/reforger/addons/` has **22 mod dirs + a `saves/` dir**. Scanner rule: **skip any dir without a `meta` file** (that's how `saves/` is excluded).
- `meta.versions[0].gameVersion` is `""` for **every** local mod — local meta gives you version+size only,
  NEVER gameVersion. gameVersion comes from the API `/versions` per-version `gameVersion` field.
- `meta` top-level keys seen: `id,name,description,type,createdAt,updatedAt,versions[],selectedRev,previews,
  summary,unlisted,tags,deleted,access,timeLastPlay,timeFirstDownload`. Use `unlisted`,`deleted` if present.
- `addon.gproj` `Dependencies { ... }` block can span **multiple lines**; GUIDs are space- and/or
  newline-separated quoted 16-hex strings. Parse the whole `{...}` region, then regex `"([0-9A-F]{16})"`.
- Some dependency GUIDs have **no local addon dir and are not on the Workshop as a mod** (e.g. Kingmaker
  deps include `58D0FB3206B6F859` which has no dir) — treat "dep GUID with neither local dir nor API record"
  as an *unresolved dependency*, not an error in the parser.
- `du` of full addons tree ~= 20 GB (matches the 19 GB figure). Biggest: RHS-ContentPack01 5.9G,
  REAPER_CORE 5.3G, REAPER_CORE_Pack2 3.9G, RHS-ContentPack02 2.3G, REAPER_Kingmaker 821M.
- `steam/mods/local/{ARMA3,DAYZ}` and `steam/mods/steamapps/` are EMPTY — not moved in Phase 5, correct.

### ⚠ Phase 5 test-#9 discrepancy to resolve before/at Phase 5
PLAN & STORIES say server 9's broken mod `658756C5760E94DE` should have its dependency GUIDs read from
"the local `addon.gproj`". **There is NO local addon dir for `658756C5760E94DE`** (never downloaded —
consistent with it being blocked). So the local-gproj fallback has nothing to read for this specific mod.
Honest pre-flight output for #9: "unresolvable on Workshop (deleted/blocked/private) AND not present
locally — dependencies cannot be enumerated." Still strictly better than the old "crashed outside the
manager". Flag this to the user at the Phase 3 or Phase 5 review; do not silently pretend gproj data exists.
(`MikesKunarInvadeAMPAnnex_65FF0A29BE6F7AA8` exists on disk with an EMPTY `addon.gproj`; unclear if related.)

### Reference config.json (old REFORGER_6.json — healthy server, shape to reproduce)
```json
{ "bindAddress":"", "bindPort":2001, "publicAddress":"", "publicPort":2001,
  "game": { "name":"badis | BattleGen High Command", "password":"9999", "passwordAdmin":"999999",
    "scenarioId":"{45FCF596CD2409EB}Missions/BattleGen_eden.conf", "maxPlayers":32, "visible":true,
    "supportedPlatforms":["PLATFORM_PC"],
    "gameProperties": { "serverMaxViewDistance":2500, "serverMinGrassDistance":50,
      "networkViewDistance":1000, "disableThirdPerson":false, "fastValidation":true, "battlEye":true,
      "VONDisableUI":true, "VONDisableDirectSpeechUI":true },
    "mods":[ {"modId":"615F07D39925670F","name":"BattleGen - High Command"},
             {"modId":"595F2BF2F44836FB","name":"RHS - Status Quo"} ] },
  "a2s": { "address":"0.0.0.0", "port":17777 } }
```
The generator must ALSO emit an `rcon` block (native): `"rcon": { "address":"0.0.0.0", "port":19999,
"password":"<gen>", "permission":"admin", "blacklist":[], "whitelist":[] }` (verify exact key names
against Bohemia server config docs during Phase 2).

## REAL FATAL-LOG FIXTURES (orchestrator, 2026-09-02) — for crash diagnosis + preflight
Real logs from the old stack's failed starts saved at `<scratchpad>/log-fixtures/`:
`REFORGER_7.log` (Ronin AI engine mismatch), `REFORGER_8.log` (deleted mod+deps),
`REFORGER_9.log` (blocked mod + missing deps), `REFORGER_13_tail.log` (healthy-ish run), `steamcmd_latest.log`.

### Exact engine phrases to pattern-match (verbatim from real logs)
| Pattern (regex-ish) | Meaning / plain-language cause |
|---|---|
| `Addon ([0-9A-F]{16}) \((.+?)\) - Addon is blocked\.` | Mod is **blocked** on the Workshop (engine knows this even though reforgermods API only 404s) |
| `Addon ([0-9A-F]{16}) - Addon was not found on workshop\.` | Mod/dep **deleted or never existed** on the Workshop |
| `Addon ([0-9A-F]{16}) \((.+?)\) - Addon has dependencies deleted from workshop\.` | Mod resolves but one/more of **its deps are gone** |
| `(\d+) addons are not downloadable! Cannot start until they are removed from server config\.` | Count of unresolvable addons; hard blocker |
| `Failed to fetch addon details from workshop API! Repeat later or try different mods\.` | Workshop API fetch failure (transient OR the above) |
| `ENGINE\s+\(E\): Addon loading failed \{([0-9A-F,]+)\}` | Full GUID set the engine tried to load and couldn't |
| `SCRIPT\s+\(E\): Can't compile "Game" script module!` | A mod's **scripts don't compile against this engine build** (server 7 / Ronin AI signature) |
| `SCRIPT\s+\(E\): .*(Can't find variable\|Can't find class\|Too many parameters\|Syntax error)` | Preceding script-compat errors — name the mod from the nearest `Addon loading failed {...}` |
| `ENGINE\s+\(E\): Cannot create game!` | Terminal: engine could not build the game world |
| `ENGINE\s+\(E\): Unable to initialize the game` | Terminal failure |
| `ENGINE\s+: Game destroyed\.` (with no `Starting RPL server` before it) | Process exited during init = failed start |
| success marker: `NETWORK\s+: Starting RPL server, listening on 0\.0\.0\.0:2001` (or `Game successfully created` + RPL) | server actually came up |
| display version: `Creating game instance\(.*?\), version (\S+) built` | scrape `1.8.0.10` |

### Download-progress phrases (verbatim) — for headless downloader
`BACKEND : Addon Download started <GUID> - <name>` / `BACKEND : Downloading <GUID> version <ver>` /
`BACKEND : <name>: [>___________________] 0% 0/0 MB` (also `[====>___] 20% 1224/6040 MB`) /
`BACKEND : Download speed 1.64 KB/s` / `BACKEND : Required addons are ready to use.`  <-- SIGTERM here.

### ⚠ Phase-5 test-#9 UPDATE — the missing dep GUIDs ARE recoverable
`REFORGER_9.log` names them: **`6512CC017515F9EB 65906C6513A8D3D4 64869009DD4637C4 64863EE1C8CF7512`**
(server 8's log overlaps: adds `6507450F3E03FE21`). So preflight's dependency-enumeration fallback
chain is: (1) API `/dependencies` when parent resolves, else (2) local `addon.gproj`, else
(3) **parse the server's most recent failed-start log** for `Addon <GUID> ...` lines, else (4) report
"dependencies unknown". For server 9 specifically, source (3) is what yields the GUIDs — there is no
local dir/gproj for `658756C5760E94DE`. This makes the PLAN's test-#9 expectation ("name the missing
dependency GUIDs") achievable; just not from gproj. Note the engine also distinguishes **blocked**
vs **not found** vs **deps deleted** — surface that exact wording, it's better than the API's bare 404.

### Old-stack log locations (read-only, for migration/import of history if wanted)
`/mnt/Apps/docker/armaservermanager/steam/logs/REFORGER_<n>.log` (per-def last run),
`/.../steam/logs/steamcmd/steamcmd_*.log`. NOT moved in Phase 5 (stay with archive) unless we choose to.

### steamcmd.net engine API — confirmed live shape
`GET https://api.steamcmd.net/v1/info/1874900` -> `.data["1874900"].depots.branches.public` =
`{ "buildid":"24501482", "timebuildupdated":1785535096, "timeupdated":1786620610 }`. Envelope also
has top-level `"status":"success"`. Linux server depot = `1874902`, shared content depot `1874901` (~10 GB).

## API ENVELOPE — CORRECTED (Phase 3a live testing, supersedes earlier "{status,data}" claim)
- `GET /mods/{id}`        -> `{"status", "mod": {...}}`            (key is **`mod`**)
- `GET /mods/{id}/versions|dependencies|scenarios` -> `{"status", "data": {"<listname>":[...], "count", "modId"}}`
- `GET /mods?q=<term>`    -> `{"status", "meta": {pagination}, "data": [...]}`  (list directly under `data`;
   term param `q`; **NO page-size param** — fixed 16/page, use `page` to paginate)
- `GET /rate-limits`      -> `{"authenticated", "rate_limit": {...}}`  (no wrapper)
- 404 body -> `{"error":{"code":"NOT_FOUND",...}}`  ; **key on HTTP status, not body**
- **HTTP 503 `UPSTREAM_UNAVAILABLE`** is common for valid-hex-but-unknown GUIDs (API proxies BI Workshop).
  Treat as TRANSIENT (retry, leave `api_state` unchanged) — it is NOT `not_found`.
- mod object fields: `id,name,summary,description,author,version(=latest),gameVersion(=latest),size,
  unlisted,private,obsolete,tags,imageUrl,previewImages,workshopUrl,dependencyCount,scenarioCount,
  dependencies[],scenarios[]`. versions[]/dependencies[]/scenarios[] fields per SA-3 report (all confirmed live).
- Phase 3a client already does full recursive camelCase->snake_case, so downstream code sees snake_case
  everywhere (`game_mode`, `player_count`, `game_version`, `game_id`).

## Phase 3a interfaces now available (import from `app.mods`)
- `scan_all(root=None) -> ScanResult(.mods: list[ScannedMod], .problems: list)`
- `workshop` singleton / `WorkshopClient`: `get_mod/get_versions/get_dependencies/get_scenarios/search(q,limit)/get_rate_limits/clear_cache/aclose`; raises `ModNotFound`, `WorkshopError`, `RateLimitExceeded`
- `resolve_dependencies(session, root_guids, *, client=None, use_api=True, max_depth=12) -> ResolvedTree`
  nodes `{guid,name?,via:api|gproj|unknown,state:ok|not_found|unresolved,depth}`; `.as_dict() -> {roots,nodes,edges}`
- `run_mod_sync(ctx)` (job kind `mod_sync`), `enrich_one(session, guid, *, client=None)`
- models: `mod_dependencies` has `source` in {`api`,`gproj`}, unique `(mod_guid, depends_on_guid)`;
  `mod_scenarios` matched on `game_id`. `Mod.api_state` in {`ok`,`not_found`,`unchecked`}.

## KNOWN OFFLINE-SCENARIO CAVEAT (accepted per PLAN, flag at Phase 5)
Offline scan builds `game_id = {mod_dir_GUID}Missions/x.conf`, but the real Workshop `gameId` uses the
`.conf` *resource's own* GUID (e.g. Kingmaker's real scenario id is `{806FFA8093F22A71}Missions/
REAPER_Kingmaker.conf`, NOT `{6576A4DF3F71360C}...`). => The API is the authoritative scenario source;
the offline list is a last-resort hint only and its `game_id` is generally NOT directly usable as a
config `scenarioId`. The 4 migrated servers' scenarioIds are taken verbatim from the old REFORGER_*.json
(they're in REF.md above / the archive), so this caveat doesn't block Phase 5.

## PHASE 3B INTEGRATION (2026-09-02)
- `Server.last_diagnosis` is persisted as JSON/JSONB in the initial schema and exposed by `ServerOut`.
  Supervisor exit events serialize it through `Diagnosis.as_dict()`.
- JWT-protected action routes are wired for library/server pins, update check/apply, verify/repair,
  preflight, RCON/player access, A2S stats/history, and filtered/downloadable logs. Mod update static
  paths precede `/{guid}`.
- `mod_download`, `verify_repair`, `mod_update_check`, and `mod_update_apply` registered factories
  read their persisted `Job.params` by job ID before executing. `engine_update` invokes
  `on_engine_updated()` once after SteamCMD; that hook owns `mark_engine_updated()` and the combined
  preflight/stale-pin report.
- `NightlyCheckScheduler` is lifecycle-managed and only started when `NIGHTLY_CHECK_ENABLED=true`.
  A2S and RCON replace wildcard/default bind addresses with `127.0.0.1` for the host-networked backend.
- Phase 5 still needs live server validation for headless download/verify output, RCON authentication
  and commands, A2S replies/history, real log-tail filtering, and the four migrated preflight cases.

## PHASE 3B ENGINE PREFLIGHT FALLBACK (2026-09-02)
- Verified fact: build `24501482` maps to display version `1.8.0.10`, confirmed from the real
  `Creating game instance(...), version 1.8.0.10 built ...` console-log line.
- Behavior: engine seed, refresh, and post-update persistence use that exact known-build fallback
  only when no current-build console-log display version is available. A changed unknown build clears
  the old display version, so preflight warns that compatibility is unavailable rather than treating
  stale data as valid. A normal console-log scrape remains authoritative and overwrites the fallback.
