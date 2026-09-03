# Reforger Manager — Sprint 3: server profile file manager

## Context

Deploying the **JLH Conflict PvE Framework** modpack (RHS + WCS NATO/RU + MrSaint JLH Config)
surfaced a real gap: everything about a server definition's *identity* — mods, scenario, network,
RCON — is already reachable through the API and the Sprint-2 UI, but the moment a mod like JLH
generates its own runtime config/persistence data on first boot, the operator is stuck SSH-ing the
NAS and hand-editing files, in flat violation of the homelab rule that `Z:\`/the NAS filesystem
stays untouched by hand. There is currently **no route of any kind** that reaches into
`PROFILES_DIR/{server_id}/` except the single hardcoded `logs/console.log` (`mods/logview.py`) —
no list, no read, no write.

This isn't JLH-specific. Research into how Reforger mods generally persist their own settings
found no single convention — only that they all write flat files directly under the server's
**profile directory** (`$profile:` in Enfusion script), with per-mod filenames and layouts:

- `PersistentXP.json` — a single JSON file at the profile root.
- `GMPersistentLoadouts/` — a subfolder of JSON files.
- `gmp_save_<map>.json` — one JSON file per map (GM Persistence).
- UUID-keyed session saves plus a `meta-info.json` index — the engine's own built-in
  Persistence System, governed by the `persistence` block in `config.json` (already reachable
  today via `extra_config`, per `config_gen.py`).

Sources: [BI Server Config wiki](https://community.bistudio.com/wiki/Arma_Reforger:Server_Config)
(blocked to automated fetches — same note already in `config_gen.py`'s docstring),
[loafhosts persistence guide](https://loafhosts.com/guides/arma-reforger-persistence-and-save-games),
Workshop listings for `PersistentXP`, `GM Persistence`, `GM Persistent Loadouts`,
`Autoload-PersistenceGM`.

Nothing is guaranteed to be JSON, nothing is guaranteed to live at a predictable filename, and nesting
depth varies per mod. **A generic, path-safe filesystem CRUD API scoped to one server's profile
directory — with a light JSON-editing convenience layered on top — is therefore the right shape**,
not a per-mod integration or a bespoke JLH feature.

### Guiding principle

*Treat `PROFILES_DIR/{server_id}/` as a small sandboxed filesystem the operator can browse and edit
entirely through the API/UI, safely by construction — never touching the engine/steamcmd-owned
trees (`SERVER_DIR`, `MODS_DIR`) or the DB-generated `config.json` this app already owns.*

## Decisions locked for this sprint

| Decision | Choice |
|---|---|
| Edit granularity | **Whole-file text edit**, with JSON pretty-print + parse-validation gate when the file is JSON (mirrors the existing `extra_config` raw-JSON textarea in `ServerForm.tsx`). No key-path/tree JSON editor. |
| Scope | `PROFILES_DIR/{id}/` — full read + write. The generated `config.json` is shown **read-only**, reusing the existing `GET /api/servers/{id}/config`; no new write path to `CONFIGS_DIR`. |
| Running-server safety | Reads/browsing **always** allowed. `write / delete / mkdir / rename / upload` return **409** while that definition is the one running — same precedent as delete-definition and modpack-apply. |
| Folder management | Full CRUD on directories too: create, delete (recursive, UI-confirmed), rename/move — not just files. |
| Upload | Yes — multipart upload into any folder in the tree. |
| Bulk export | Yes — one-click streamed `.zip` of the whole profile directory. |
| Auto-backup on overwrite (`.bak`, versioning) | **Declined** — cut. See *Cut from this sprint*. |
| Symlinks | Never followed, never created. Listed as their own type; every mutating/read op on one is refused (400). |
| Oversized / binary files | Download-only, never loaded into the text editor. Threshold is a setting (`FILES_MAX_EDIT_BYTES`, default 2 MiB). |
| `logs/` and `addons_tmp/` subfolders | **Excluded from the browser.** `logs/` already has a dedicated Console/Log tab (`mods/logview.py`, `GET /{id}/log`); `addons_tmp/` is engine scratch space recreated every start. Flagged here as an explicit, overridable default rather than a hard requirement. |
| `SERVER_DIR` / `MODS_DIR` | Never exposed by this feature — non-goal, see below. |

## Code-level facts the implementation must respect

1. **`profile_dir` may not exist yet.** `settings.profile_dir(server_id)` (`core/config.py:105`) is
   a computed path; it's only `mkdir`'d lazily by `supervisor.start` (`servers/supervisor.py:167`).
   A definition that has never been started has no profile directory at all — `GET .../files`
   must return an empty listing in that case, not 404.
2. **The path-safety pattern already exists once** — `mods/logview.py`'s `_is_relative_to()` +
   `.resolve(strict=False)` triple-check (profiles_root ⊃ profile_dir ⊃ candidate) is exactly the
   defense this feature needs, generalized from one hardcoded filename to an arbitrary relative
   path. Extract it into a shared helper rather than re-deriving it.
3. **The running-server check is `supervisor.active_server_id == server_id and
   supervisor.is_running()`** — the same condition `stop_server` (`api/servers.py:481`) and the
   409s on delete/modpack-apply already use. Reuse it verbatim for the write-side 409s here.
4. **No new DB tables.** This is pure filesystem I/O against an existing bind mount
   (`/mnt/Apps/docker/reforger-manager/profiles/` on the NAS) — no FK relationship, no ORM model.
5. **`GET /api/servers/{id}/config`** (`api/servers.py:284`) already regenerates and returns the
   live `config.json` dict without touching disk unless `?write=true`. Reused as-is for the
   read-only reference panel — no backend change needed for that piece.
6. **The JSON-textarea-with-validation pattern to copy already exists** in
   `frontend/src/components/server/ServerForm.tsx` (~lines 843–854, plus the `canon`/parse helpers
   above it) for `extra_config` / `game_properties`. The file editor's JSON gate is the same
   pattern, generalized to "any file whose extension is `.json`" instead of two hardcoded fields.
7. **`lib/api.ts`'s `api<T>()` helper assumes a JSON request/response body.** File content
   read/write can go through it (text payloads, JSON-encoded on the wire). Upload (multipart) and
   download/archive (binary blob response) cannot — they need raw `fetch()` calls that bypass that
   helper, same as the existing log-download button already does for `GET /{id}/log?download=true`.
8. **The existing log-download route** (`api/servers.py`, `read_log_download` /
   `Response(payload.content, media_type=..., headers={"Content-Disposition": ...})`) is the
   precedent for this feature's `/download` and `/archive` routes — same `Response` shape, same
   attachment-header convention.

## Feature set

### 1. Path-safety module — `backend/app/servers/files.py`

- `resolve_safe_path(server_id, rel_path) -> Path`: rejects absolute input paths and `..`
  segments before resolution (defense in depth), then resolves and re-checks ancestry under
  `profiles_root ⊃ profile_dir` (generalizing `logview._is_relative_to`). Raises a typed error the
  API layer turns into 400.
- Symlinks are never transparently followed: any path component that is a symlink, or whose
  resolved target escapes the profile dir, is rejected outright rather than silently contained.
- `@dataclass FileEntry { name, rel_path, type: "file" | "dir" | "symlink", size, mtime, is_text,
  is_json }` — `is_text` / `is_json` are best-effort sniffs (UTF-8 decodability; `json.loads` for
  `.json`-suffixed files) used by the API/UI to decide editable-vs-download-only.
- `logs/` and `addons_tmp/` are filtered out of listings at this layer (see decision above).

### 2. Backend API — new router `backend/app/api/server_files.py`

Mounted at `/api/servers/{server_id}/files`, `dependencies=authed`, registered in `main.py`
alongside the other routers:

| Method & path | Purpose |
|---|---|
| `GET /` | List one directory's entries (`?path=` relative dir, default root). Non-recursive — the UI does breadcrumb navigation rather than fetching a full recursive tree, so a profile dir with thousands of persistence files stays a cheap call. |
| `GET /content` | `?path=` a file. Returns `{path, content, is_json, size}` for text/JSON files under `FILES_MAX_EDIT_BYTES`; for anything binary or over the cap, returns a `{path, size, editable: false}` marker instead of erroring, so the UI can offer download in its place. |
| `PUT /content` | `?path=`, body `{content: str}`. Creates the file if missing, else overwrites. Server-side re-validates JSON for `.json` paths (never trust the client-side gate alone). 409 while running. |
| `DELETE /` | `?path=` file or directory (recursive for a directory — the UI confirms first for anything with children). 409 while running. |
| `POST /mkdir` | Body `{path: str}`, creates intermediate directories as needed. 409 while running. |
| `POST /rename` | Body `{path: str, new_path: str}`. Refuses if the destination already exists. 409 while running. |
| `POST /upload` | Multipart, `?path=` target directory. One or more files; each checked against `FILES_MAX_UPLOAD_BYTES` and the same path-safety resolver (filename itself can't smuggle `..`). 409 while running. |
| `GET /download` | `?path=` a file. Raw byte stream with `Content-Disposition: attachment` — works for any file regardless of size/type, mirroring the existing log-download route. |
| `GET /archive` | Streamed `.zip` of the entire profile directory. Symlinks are skipped (not followed) while walking. |

### 3. Settings additions

`core/config.py` / `.env.example`:

- `files_max_edit_bytes: int = 2_097_152` — files at or under this size are eligible for the
  text/JSON editor; larger ones are download-only.
- `files_max_upload_bytes: int = 50_000_000` — uploads over this are rejected with 413.

### 4. Frontend — new "Files" tab

- `frontend/src/pages/ServerDetail.tsx`: add `"Files"` to the existing `tabs` tuple alongside
  Config/Mods/Console/Players/History.
- New `frontend/src/components/server/FilesPanel.tsx`:
  - Breadcrumb + single-level directory listing (name, type icon, size, modified time), with
    **New Folder**, **Upload**, and **Download all as .zip** controls above the list.
  - Per-row actions: **Open** (files where `is_text` is true), **Download**, **Rename**,
    **Delete** — folders get Delete (recursive, confirmed) and Rename too.
  - Edit view: a `Dialog` (reusing `components/ui.tsx`) with a textarea, Save/Cancel. Save is
    disabled with an inline error when `is_json` is true and the current text fails
    `JSON.parse` — the same gate `ServerForm.tsx` already implements for `extra_config`. While
    this definition is the one running, the same *"stop the server to make changes"*
    banner/disabled-state used elsewhere in the app applies to every mutating control.
  - A collapsed **"Generated config.json (read-only)"** panel at the top of the tab, sourced
    from the existing `GET /api/servers/{id}/config` — no new endpoint for that piece.

## Data-model changes

None. This is pure filesystem I/O against the existing `PROFILES_DIR/{server_id}/` bind mount —
no new tables, no new columns, no migration.

## Testing

- **Path-safety**: `../../etc/passwd`, absolute paths, a symlink pointing outside the profile dir,
  a symlink pointing *inside* it (still refused — "never follow" is unconditional), embedded null
  bytes, and a path resolved against the wrong `server_id` (must never reach another server's
  profile dir).
- **Running-server 409s**: `write / mkdir / rename / delete / upload` all refused while
  `supervisor.active_server_id` is this server and it's running; `GET /`, `/content`, `/download`,
  `/archive` all still succeed.
- **JSON gate is server-side, not just client-side**: `PUT /content` on a `.json` path with
  invalid JSON is rejected even if some future/alternate client skips its own check.
- **Size-cap fallback**: a file over `files_max_edit_bytes` comes back from `GET /content` as the
  non-editable marker, not as content — and is still reachable via `/download`.
- **Missing profile dir**: `GET /` for a definition that has never started returns an empty list,
  not 404.
- **Archive correctness**: write a small tree of files/folders (including one symlink), hit
  `/archive`, unzip in memory, assert the symlink was skipped and everything else round-trips.
- **Upload**: happy-path multipart write, an oversized file rejected with 413, and a crafted
  filename containing `../` rejected by the same path-safety resolver used everywhere else.

## Cut from this sprint

- **Auto-backup / `.bak` history on overwrite** — declined. A bad hand-edit here is unrecoverable
  from the UI itself (though ZFS snapshots/replication on the NAS side, per `CLAUDE.md`, still
  cover the underlying bind mount). Revisit if this bites someone in practice.
- **JSON key-path tree editor** (structured get/set/delete of individual nested keys via a
  JSON-Pointer-style API) — whole-file text edit covers the same ground with much less API/UI
  surface for files this size. Revisit only if profile JSON files turn out large/deeply nested
  enough that hand-editing raw text becomes genuinely painful.
- **Full recursive tree in one response** — deferred in favor of per-directory (breadcrumb)
  listing, so a profile dir with thousands of persistence save files never produces a slow or huge
  list response.

## Non-goals (explicitly out of scope for Sprint 3)

- Editing `SERVER_DIR` (the steamcmd engine install) or `MODS_DIR` (the addon cache) — both are
  huge, engine/steamcmd-owned binary trees; never exposed by this feature.
- Editing `CONFIGS_DIR/{id}.json` directly — `supervisor.start` regenerates and overwrites it from
  the DB on every server start (`config_gen.write_config`), so a direct hand-edit here would be
  silently clobbered on next start. Stays a read-only reference view sourced from the existing
  `GET /api/servers/{id}/config`.
- Multi-user permissions or per-file ACLs — the single-admin auth model from Sprint 2 is unchanged.
- Live file-watching or a real-time diff of a mod's save file while the server is running.

## Verification

- `cd backend && python -m pytest`
- `cd frontend && npm run build`
- Manual, using the JLH Conflict PvE Framework stack as the live test case: create the definition
  via the existing `POST /api/servers` (mods + scenario ID from the serverpack doc), start it once,
  then use the new Files tab to open whatever JLH/MrSaint actually write under
  `PROFILES_DIR/{id}/`, edit as needed, restart, and confirm QRF/RECAP factions per the serverpack
  doc's own §9 functional-test checklist — all without ever touching the NAS filesystem by hand.
