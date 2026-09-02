# Reforger Manager — purpose-built Arma Reforger server manager

> Implementation phases live in [STORIES.md](STORIES.md).

## Context

`armaservermanager` (fugasjunior/arma-server-manager) is an Arma 3 / DayZ tool with Reforger
bolted on. Today's debugging session showed the cost of that: of 8 Reforger servers, 5 would not
start, and diagnosing why required SSH-ing into the NAS and grepping 300,000-line console logs,
because the UI only said *"crashed or was exited outside the manager"*.

The three distinct root causes we found were all knowable **before** launching:

| Server | Real cause | Detectable in advance? |
|---|---|---|
| CO-OP Conflict PVE (7) | dep mod *Ronin AI* script incompatible with engine 1.8.0.10 | yes — mod's `gameVersion` vs installed build |
| Conflict Everon PVE RHS (8) | mod + 5 deps **deleted** from Workshop | yes — Workshop lookup |
| Conflict Kunar PVE RHS-WCS (9) | mod **blocked** on Workshop + 4 missing deps | yes — Workshop lookup |

We are replacing it with a Reforger-only manager: a **server-template** system (many saved
definitions — configs, maps, mods) where **one runs at a time**, with a genuinely useful mod
subsystem and a reactive web UI.

### What is wrong with the current solution

1. **Steam credentials demanded unnecessarily — and the login is now actively broken.** The stack
   logs in as a real account (`login madit79` + Steam Guard) and that login is failing
   `Two-factor code mismatch`; that is the confirmed source of the permanent *"Incorrect Steam
   authorization"* error. App `1874900` needs **no login at all** beyond `login anonymous`, so the
   entire credential path — and the 2FA breakage with it — simply disappears.
2. **No mod library.** The Mods page shows `0–0 of 0` while 22 mod dirs (19 GB) sit on disk.
3. **Manual ID/GUID entry** for both mods and scenario IDs (`{806FFA8093F22A71}Missions/…conf`).
4. **Dependencies invisible.** The engine silently pulled Ronin AI, RHS packs, etc. — none shown in UI.
5. **No update checking, no version pinning.** An upstream mod update can silently break a server,
   with no way to hold a known-good version while the author fixes it.
6. **Engine updates are invisible and untracked.** Nothing records which build is installed or
   notices that a newer one exists — yet an engine bump is precisely what breaks mods built against
   the old one (server 7: *Ronin AI* against `1.8.0.10`). The update is also unavoidable: clients
   are force-updated by Steam and cannot join an older build, so the server must follow.
7. **Never uses `-addonsVerify` / `-addonsRepair`** (both confirmed present in the binary). This is why
   your kingmaker fix required manually deleting mod folders — the engine can self-repair, unprompted.
8. **All 13 servers hardcoded to port 2001/17777**, and RCON is not configured at all.
9. **Opaque failures** — no parsing of the fatal log lines that state the cause outright.
10. Heavy Java/Spring + MySQL stack for what is fundamentally process supervision.

## Verified technical findings

These were confirmed against the live NAS and the installed binary (`1.8.0.10`, build `24501482`):

- **Install needs no credentials** — `steamcmd +login anonymous +app_update 1874900 validate`.
  App `1874900` (Arma Reforger Server) is anonymous-installable; confirmed by the user against
  Bohemia/Steam documentation. No Steam account, no Steam Guard, no 2FA anywhere in this project.
- **Binary deps are trivial** — `ldd` shows only glibc/libstdc++. steamcmd itself needs `i386`
  foreign arch. Extra runtime packages: `libcurl4`, `libssl3`, `net-tools`.
- **Supported flags present in the binary**: `-addonsVerify`, `-addonsRepair`, `-addonsChecksum`,
  `-addonsForceLocal`, `-addonTempDir`, `-addonDownloadDir`, `-addonsDir`, `-logStats`, `-nothrow`,
  `-logsDir`, `-loadSessionSave`. (`-listScenarios` and `-autoreload` are **not** in this build —
  scenario discovery must come from the API/rdb, see below.)
- **Mod download is driven by the engine itself** and emits parseable progress:
  `Addon Download started <id> - <name>` → `<name>: [====>____] 20% 1224/6040 MB` →
  `Required addons are ready to use.` → `NETWORK: Starting RPL server, listening on 0.0.0.0:2001`.
  Launching with a synthetic config and killing it at "ready" is a reliable **headless updater**.
- **Local metadata already on disk** — **22** mod dirs under `mods/reforger/addons/`, named
  `Name_GUID`. Per dir (re-verified 2026-09-02, corrections in bold):
  - `meta` — UTF-8 **BOM** confirmed (`ef bb bf`, read `utf-8-sig`), and it is **JSON**. But
    everything is wrapped in a **single top-level `meta` object**, and two fields are not where
    assumed: **version is `meta.versions[0].version`**, **size is `meta.versions[0].package.totalSize`**
    (no `size` key). `deleted`, `unlisted`, `name`, `summary`, `tags` are present as expected.
    **`meta.versions[0].scenarios` and `.dependencies` are `[]` even for mods that have them** —
    meta is useless for both; do not read them from here.
  - `addon.gproj` — confirmed authoritative for dependencies. ENFUSION `GameProject { … }` key-value
    block, **not JSON**; deps are bare space-separated quoted GUIDs, no names:
    `Dependencies { "58D0FB3206B6F859" "61B514B96692C049" }`.
  - `resourceDatabase.rdb` — uncompressed **IFF container**, header `FORM`…`RDBC`. Records carry an
    8-byte GUID-ish field plus a **uint32-LE length-prefixed, NUL-terminated path**, so the original
    description was roughly right — but see the scoping decision under Feature #3.
  - **Also present, and missed by the first pass**: `ServerData.json` (BOM JSON: `id`, `name`,
    `revision.version` — the simplest version source), `<file>_<version>_manifest.json`
    (`version`/`size`/`sha512`/`fragments` — integrity data), `thumbnail.png`, `scenario_image_N.tif`,
    and split `data*.pak` payloads.
  - The NAS has no `xxd` and likely no `file`; use `od` / `strings` when inspecting.
- **Workshop metadata API** — `https://api.reforgermods.net/v2` (unofficial; Bohemia has no official
  public Workshop API). Re-validated live 2026-09-02 against RHS Status Quo `595F2BF2F44836FB`
  (exists, `0.16.5150`, `gameVersion 1.8.0.10`, 2 deps, 11 scenarios). Real endpoints:
  `/mods/{id}`, `/mods/{id}/versions`, `/mods/{id}/dependencies`, `/mods/{id}/scenarios`,
  `/mods?q=`, `/rate-limits`. `/v2` root 404s; docs at `reforgermods.net/arma-reforger-mods-api/v2/`.
  - `versions[]`: `version`, `gameVersion`, `size`, `approved`, `published`, `createdAt`,
    `scenarioCount`, `dependencyCount` — as assumed.
  - `scenarios[]`: `gameId` in the exact expected format, plus `name`, `gameMode`, `playerCount`
    — note **camelCase**, so the `mod_scenarios` columns `game_mode`/`player_count` are a mapping,
    not a passthrough.
  - `dependencies[]`: `id`, `name`, `version`, `published`, `private`, `workshopUrl`.
  - Rate limit confirmed via `/rate-limits`: 60/min, **burst 20**, 20 000/day, scoped
    `by client_ip` — so the whole container shares one budget.
  - **Search is `?q=`, not `?search=`.**
  - ⚠ **The state-flag model was wrong.** There is no `deleted` and no `blocked`. The real booleans
    are `unlisted`, `private`, `obsolete`. A deleted *or* blocked *or* private mod simply returns
    **HTTP 404** and vanishes from search, with **no reason code** — the API cannot tell you which of
    the three it is. See the data-model and pre-flight notes below.
- **RCON is native** — `rcon` block in config.json, default port 19999, `admin`/`monitor`
  permission, commands `#players`, `#kick`, `#ban`, `#say`, `#restart`, `#shutdown`.
- **Live stats** via A2S query on port 17777 (how the old UI got "0 / 32" and the map name).
- **`Apps/docker` is a single ZFS dataset** — re-verified: `zfs list -r Apps/docker` returns exactly
  one dataset, no child boundary anywhere under `armaservermanager`, **823 GB free**. Actual sizes:
  `steam/servers/REFORGER` **9.9 GB**, `steam/mods/reforger` **19 GB**, 29 GB total ⇒ the Phase 5
  `mv` is genuinely a rename, not a copy.
- **Cutover targets are clear** (verified 2026-09-02): port **`18090` is free**, and so are
  `2001`/`17777`/`19999` (no Reforger process is running; `assettoserver` and `ams2` exited months
  ago). The old stack binds only TCP `18080` (`armaservermanager`, host-net) and TCP `3306`
  (`armaservermanager-db`, `mysql:8.3`). `Z:\traefik/dynamic/armaserver.yml` points at
  `http://192.168.1.10:18080` — a genuine one-line change — keeping `sec-headers@file`, `gzip@file`,
  `crowdsec@docker` and the wildcard cert. `armaserver.badis.net` **is** present in `Z:\ddns/.env`
  `DOMAINS`, so no Cloudflare churn. All 13 `REFORGER_1..13.json` exist and are contiguous.
- **Engine build detection is solved, cheaply** (this replaces the earlier unverified guess):
  - Installed build reads straight from `appmanifest_1874900.acf` → `"buildid" "24501482"`
    (plus `TargetBuildID`, `BetaKey "public"`).
  - Available build: **`GET https://api.steamcmd.net/v1/info/1874900`** →
    `depots.branches.public.buildid` = `24501482` today (i.e. we are current), with `timeupdated`.
    Plain HTTP, no steamcmd, **no writes** — safe for a nightly job.
  - Fallback if that API is stale or down: `steamcmd +login anonymous +app_info_update 1
    +app_info_print 1874900` inside the container (retry once — the first call can return `{}`).
    It writes only to `Steam/appcache/*.vdf`, which here is the container's ephemeral overlay, not a
    bind mount, so the write is harmless.
  - **The human-readable version is *not* on disk in any manifest.** `1.8.0.10` exists only in
    derived artifacts that require a prior server run: the `console.log` line
    `INIT : Creating game instance(ArmaReforgerScripted), version 1.8.0.10 built …`, and a
    savegame's `meta-info.json` `m_sGameVersion`. (`VERSIONS.txt` in the install dir is the Steam
    Linux Runtime version — unrelated, do not use it.) ⇒ **detection keys on the numeric `buildid`**;
    the display version is a separate best-effort field scraped at startup.

**Why the old stack still uses a Steam account.** It did not have to. steamcmd logs show
`login madit79` + Steam Guard and the ACF `LastOwner` is a real SteamID64, so this install was made
**authenticated** — a choice, not a requirement, and one that has since rotted into the
`Two-factor code mismatch` failure behind the dashboard error. The new stack drops the login
entirely; nothing in the migration inherits the account, and the existing install is re-validated
anonymously in place.

## Architecture

Proven pattern borrowed from the existing image: root entrypoint remaps the `steam` uid/gid to the
bind-mount owner, then `gosu steam` drops privileges. Game servers run as **child processes** under
**host networking** (Reforger is sensitive to UDP/NAT).

```
Z:\reforger-manager/                 → /mnt/iceberg/docker/reforger-manager/
  docker-compose.yml
  .env
  Dockerfile
  backend/          FastAPI + SQLAlchemy 2.0 (async) + Alembic
    core/           config, auth, job queue
    steam/          steamcmd wrapper (anonymous), install/update/validate, engine build tracking
    mods/           workshop API client, local scanner, rdb parser, downloader
    servers/        config generator, process supervisor, preflight
    rcon/           BattlEye RCON client
    a2s/            live query client
    api/            REST + WebSocket routes
  frontend/         React + TS + Vite + TanStack Query + Tailwind + shadcn/ui
```

Runtime (SSD, **not** in the share) — `/mnt/Apps/docker/reforger-manager/`:

```
  server/     Reforger install (steamcmd, ~10 GB)   ← moved from old stack
  mods/       addon cache (~19 GB)                  ← moved from old stack
  profiles/   per-server-definition profile + logs
  configs/    generated config.json per definition
  pg/         Postgres data
```

Containers: `reforger-manager` (host network, API + supervisor + built SPA) and
`reforger-manager-db` (Postgres 17). Postgres replaces MySQL — JSONB suits the config storage.

**Routing:** keep `armaserver.badis.net` (already in `Z:\ddns/.env` `DOMAINS`, so no Cloudflare
churn). Update `Z:\traefik/dynamic/armaserver.yml` to point at the new port `18090`. Game ports
stay `2001/udp` + `17777/udp`, adding `19999/udp` for RCON — existing port-forward unaffected.

### Data model (core tables)

- `engine` — singleton row: `installed_version`, `installed_build`, `latest_version`, `latest_build`,
  `last_checked`, `last_updated_at`. The one source of truth for "which build are we on" and
  "is a newer one available"; every pre-flight game-version comparison reads `installed_build` from here.
- `mods` — guid PK, name, summary, installed_version, latest_version, latest_game_version, size,
  thumbnail, tags, last_checked,
  `is_unlisted`, `is_private`, `is_obsolete` (the flags the API actually returns) and
  `api_state` ∈ `ok | not_found | unchecked` + `api_checked_at` — because deleted, blocked and
  private are indistinguishable behind a bare 404, we record *"the Workshop will not resolve this"*
  as one honest state rather than pretending to know which,
  `pinned_version`, `pinned_at_build`, `pinned_reason`, `pinned_at` — the pin records *which engine
  build it was known good against*, so pre-flight can call it stale once the engine moves past it
- `mod_dependencies` — mod_guid → depends_on_guid
- `mod_scenarios` — mod_guid, game_id, name, game_mode, player_count
- `modpacks` / `modpack_items` — name, description; (modpack, mod, load_order)
- `servers` — name, scenario_game_id, config JSONB, ports, rcon settings, is_favourite
- `server_mods` — server_id, mod_guid, load_order, `pinned_version`, `pinned_at_build`,
  `pinned_reason`, `pinned_at` (per-server pin overrides the library-wide one)
- `server_config_revisions` — versioned snapshots for diff/rollback
- `jobs` — kind, state, progress %, current step, log tail, started/finished

## Feature set

### The ones you asked for
1. **Mod library** — every mod on disk, with thumbnail, version, size, tags, **which servers use it**,
   dependency tree, and an update badge. Sourced from disk, enriched from the API.
2. **Add mods without GUID hunting** — paste a Workshop URL *or* ID, or search the Workshop inline
   (`/v2/mods?q=`) and click Add.
3. **Scenario picker** — dropdown populated from the selected mods' scenarios. No more pasting
   `{GUID}Missions/x.conf`. API is the primary, authoritative source (it returns `gameId` directly).

   **Offline fallback, deliberately scoped down after inspecting real files:** do *not* build a full
   RDBC record-table parser. Extracting a per-record GUID means reverse-engineering an undocumented
   Enfusion layout — roughly a day of RE for a parser that breaks on the next format bump — and it is
   unnecessary. The scenario *paths* come out trivially with a `strings`+regex scan for
   `^Missions/.*\.conf$` (verified against 3 mods), and the full gameId is simply
   `{` + `meta.id` (= the mod's dir GUID) + `}` + that path. Cheap, good enough, no RE.
4. **Mod picker when creating/editing a server** — multi-select from the library with search and
   filters, showing size impact and dependency additions before you commit.
5. **Drag-and-drop load order** per server (`dnd-kit`), since Reforger load order is significant.
6. **Modpacks** — named reusable mod lists, apply/clone to any server, JSON import/export,
   "create modpack from this server".
7. **Update modes** — per server, and globally across all downloaded mods; each available as
   *check only* (fast, API-only) or *apply* (downloads); plus an optional scheduled nightly check
   that covers the engine build too (see #20).

### Additions I recommend
8. **Pre-flight check before start** — validates every mod *and resolved dependency*: resolves on
   Workshop, not unlisted/private/obsolete, `gameVersion` matches the installed engine, disk space
   sufficient. **Would have caught all three of today's failures without launching anything.**

   Two consequences of what the API actually returns:
   - A mod that 404s is reported as *"no longer resolvable on the Workshop — deleted, blocked or made
     private"*, not as a specific cause. Honest and still perfectly actionable; the old UI said
     nothing at all.
   - **Dependency enumeration must fall back to local disk.** When a *parent* mod 404s, the API
     cannot list its dependencies either — exactly server 9's case. The dependency GUIDs then come
     from the on-disk `addon.gproj`, which is why that file is the authoritative dependency source
     (the old `REFORGER_*.json` definitions list only top-level mods and no dependency GUIDs at all).
   It is also the **backstop for pinning**: any pin whose `pinned_at_build` is older than the
   installed build is flagged as *stale* — held at a version that was known good against an engine
   we are no longer running — with a one-click "unpin and take latest".
9. **Verify & repair** — one click running `-addonsVerify -addonsRepair`, optionally automatic
   before start. This replaces the manual "delete the mod folder and let it redownload" ritual.
10. **Mod version pinning** — hold a *mod* at a known-good version so an upstream release cannot
    break a working server while you wait for the author to fix it. This works because clients carry
    no mods: they download whatever version the server dictates, and Bohemia keeps old versions
    hosted (`/versions` exposes each with its `gameVersion`).

    Deliberately scoped as a **temporary hold, not a permanent state**. A pin protects against
    upstream *mod* changes; it does nothing against an *engine* update, and after one it becomes a
    liability — a mod frozen against the old build is exactly the Ronin AI failure. So every pin
    stores the build it was taken against, the UI shows its age, and pre-flight (#8) flags it stale
    once the engine moves.

    **The server binary is explicitly not pinnable.** Clients are force-updated by Steam and cannot
    join an older build, so holding the engine back only makes the server unjoinable. Its only
    update path is forward.
11. **Crash diagnosis** — match known fatal patterns (`Addon loading failed`, `Can't compile "Game"
    script module`, `Addon was not found on workshop`, `Addon is blocked`, `has dependencies deleted`)
    and show a plain-language cause instead of "crashed outside the manager".
12. **Log viewer that is actually readable** — WebSocket live tail, severity filter, and default
    suppression of known spam (today's log was 300,681 lines, overwhelmingly repeated
    `thermalProfileDefault.conf` errors). Full search; raw download.
13. **RCON console + player management** — live player list, kick/ban/say, scheduled restart with
    in-game warnings.
14. **Live server stats** — A2S player count/map + `-logStats` FPS, with a small history graph.
15. **Config revisions with diff and rollback** — every change versioned.
16. **Storage management** — per-mod disk usage, orphan detection (mods no longer referenced by any
    server), and a free-space guard before large downloads.
17. **Server definition clone/template** — duplicate an existing definition as a starting point.
18. **Backup/restore** — export all server definitions + modpacks as JSON.
19. **Proper auth** — hashed credentials in the DB, JWT sessions (not plaintext env vars).
20. **Engine update intelligence** — the server binary gets the same treatment the mods get, instead
    of being a bare "run steamcmd" button:
    - **Detection & badge.** A cheap check (installed `buildid` from the local appmanifest vs the
      public-branch `buildid` from steamcmd) runs on the same schedule as the nightly mod check and
      surfaces an update badge on the Dashboard, with the current build always visible.
    - **Blocked while running.** The single-server rule extends here: no engine update starts while
      a server is up; offer "update after this server stops".
    - **Post-update trigger** — the part that matters most. Completing an engine update writes the
      new build to `engine`, then automatically re-runs pre-flight across *every* server definition
      and re-validates every pin against the new build. The result is a plain report: which servers
      are still green, which mods now have a `gameVersion` mismatch, which pins went stale. This is
      server 7's failure caught the moment it becomes true, rather than at the next launch attempt.

## Verification

1. **Clean-install path**: build the image, start with empty volumes, confirm steamcmd installs the
   server anonymously with visible progress and no credentials.
1b. **Adopted-install path**: the migrated install was originally made *authenticated* (its
   `appmanifest_1874900.acf` carries a real `LastOwner` and `FullValidateAfterNextUpdate 1`). Confirm
   an anonymous `+app_update 1874900 validate` runs cleanly over it and does not force a full 9.9 GB
   redownload. If it does re-fetch everything, that is a nuisance, not a blocker — but find out
   before cutover, not during it.
2. **Import path**: after migration, the Mod Library lists all 22 existing mods with correct versions,
   sizes, and dependency edges — with no redownload. (The mod cache moves wholesale regardless of how
   many server definitions we migrate.)
3. **Regression against today's findings** — the sharpest test of whether this was worth building.
   Only **four** definitions are migrated (see Phase 5), chosen to cover both the happy path and the
   failure detection that justifies the build:
   - `badis | BattleGen High Command` (6) and `badis server | reaper kingmaker` (13) → pre-flight
     green, start, reach `Starting RPL server, listening on 0.0.0.0:2001`, appear in A2S, stop cleanly.
   - `CO-OP Conflict PVE` (7) → pre-flight flags *Ronin AI* game-version mismatch **before** launch.
     Re-confirmed live: mod `6294F6D5EDD5CA66` still resolves and is published, but its newest
     version `1.0.27` declares `gameVersion 1.2.1.173` and was last touched 2025-01-12 — abandoned
     ~20 months, against our `1.8.0.10`. Test valid exactly as written.
   - `Conflict Kunar PVE RHS-WCS` (9) → mod `658756C5760E94DE` still 404s on both `/mods/{id}` and
     `/dependencies` and is absent from search — still broken, not restored. Pre-flight must flag it
     unresolvable **and name the missing dependency GUIDs from the local `addon.gproj`**, since the
     API cannot enumerate them for a 404'd parent. That local fallback is what this test really
     exercises. (Server 8 adds no new detection path.)
4. **Mod update**: run check-only globally (expect RHS Status Quo `0.16.5150` = current), then force
   a re-download of one small mod and watch live progress.
4b. **Engine update detection**: confirm the installed build reads as `24501482` and that the
   available-build check returns a value (equal, if we are current). Simulate a bump by writing an
   older `installed_build` and confirm the Dashboard badge appears, the update is refused while a
   server runs, and completing it re-runs pre-flight across all four definitions.
4c. **Stale-pin backstop**: pin a mod at its current version, then lower `engine.installed_build`
   below the pin's `pinned_at_build`; pre-flight must flag the pin stale and offer unpin-and-update.
5. **Verify/repair**: corrupt a non-critical mod file in a scratch copy, confirm `-addonsVerify
   -addonsRepair` detects and repairs it without manual folder deletion.
6. **RCON/A2S**: with a server running, confirm the player list loads and `#say` reaches the game.
7. **Single-server rule**: attempt to start a second definition while one runs; expect a clear refusal.
8. **Traefik**: `https://armaserver.badis.net` serves the new UI over the existing wildcard TLS.
