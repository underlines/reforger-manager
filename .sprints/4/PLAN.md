# Reforger Manager — Sprint 4: MCP administration interface

## Context

Admins want to administrate the game server from their CLI agents (Claude Code, Codex)
over HTTPS instead of the webui. Hard requirement: **webui and MCP must never diverge** —
no second logic path. Decision (reviewed against alternatives): **MCP is a thin adapter over
our own REST API**. The REST surface is already task-shaped — `start`/`stop`/`sync`/
`verify`/`schedule-restart`/`config/preview` are server-side orchestrations — so 1:1
verb exposure is legitimate here; the "design intent-tools, never expose CRUD" advice
targets raw CRUD backends and does not apply. The only additions are three composite
read-only tools where an agent's UX would genuinely break (job polling, log tail,
overview), none of which contain business logic.

```
Claude Code / Codex ──HTTPS──▶ /mcp (FastMCP, in-process) ──httpx ASGITransport──▶ FastAPI /api/*
                                                                                       (same code path as the webui)
```

Everything an MCP tool does therefore runs the real request path: DI sessions, job
manager, supervisor state, `_apply_mods` pin reconciliation, 409s, single-server rule.

## Decisions locked for this sprint

| Decision | Choice |
|---|---|
| Tool surface | **1:1 mirror of the REST verbs** via one declarative tool table + 3 composite read tools. No OpenAPI→MCP generation; no hand-composed intent tools. |
| Transport | Streamable HTTP, `FastMCP.streamable_http_app()` mounted **in-process** at `/mcp` in `main.py` (supervisor, broadcaster, job manager, `_STATS_HISTORY` are loop-local — a separate process would desync). |
| Auth | New `mcp_tokens` table. Client sends `Authorization: Bearer rfm_<token>`. ASGI middleware validates (sha256, not revoked, not expired) → mints an app JWT via existing `create_access_token` → forwarded on internal API calls, so `get_current_user` runs the normal path. Revocable per token (the webui login flow is untouched). |
| Dangerous ops | Explicit `confirm: bool = false` tool argument; the tool 400s unless true. Applies to: stop server, delete server, engine update, mod update apply, verify/repair, backup import, job prune/cancel/delete, file write/delete/rename/mkdir/upload. |
| Job-returning tools | Return `JobEnqueuedOut` verbatim + document the `wait_for_job(job_id)` idiom in each such tool's description. |
| Settings / backup | Included with `confirm` (full parity with the webui, per review). |
| Frontend | Settings page → "MCP Tokens" card: list / create (label) / revoke; raw token shown exactly once. |
| Excluded from the surface | `auth/login`, `health`, every WS route (`ws.py`), token-management routes themselves. |
| New env settings | None. Disabling MCP = revoke all tokens. |

## Code-level facts the implementation must respect

1. **SPA catch-all** — `main.py:308` `@app.get("/{full_path:path}")` serves `index.html`
   for anything not starting with `api`/`docs`/`openapi`. Mount `/mcp` with the
   `include_router` block (mounts/routes match in registration order → the mount wins),
   **and** add `"mcp"` to the excluded prefixes in `_spa` as defense in depth. A bare
   `GET /mcp` returning `index.html` is the failure mode.
2. **httpx is already a dependency** (`httpx==0.28.1`, engine-build lookup + Workshop).
   `httpx.ASGITransport(app=app)` is the adapter mechanism — and already this repo's test
   convention (`tests/test_scenarios_routes.py`), so tests and production use the same trick.
3. **JWT minting exists** — `core/security.py:46` `create_access_token(subject)`.
   `get_current_user` requires an active `User` row, so mint with
   `sub=settings.admin_username` (the single-admin model from Sprint 2 is unchanged).
4. **Migration numbering** — `migrations/versions/` has `0001`, `0002` → new table is
   `0003_mcp_tokens`. Both startup paths (`create_all` default and `alembic`) must work;
   `models/__init__.py` re-exports every mapped class so the metadata registers.
5. **Router docstrings are the endpoint inventory** (`servers.py:1-17`, `jobs.py:1-7`,
   `engine.py`, `mods.py`, `modpacks.py`, `server_files.py`, `backup.py`, `settings.py`,
   `storage.py`, `scenarios.py`). Derive the tool table from them; trust symbol names
   over line numbers.
6. **MCP SDK dependency risk** — `mcp` pulls a Starlette/anyio range. Verify it
   co-installs with `fastapi==0.118.0` before committing; pin the exact version in
   `requirements.txt`. `pip install --dry-run` or a fresh venv `pip check` is enough.
7. **Token format** — `secrets.token_urlsafe(32)` prefixed `rfm_` (identifiable,
   greppable, high-entropy). Store only `sha256(token)`; show the raw token once in the
   201 response and the UI dialog.
8. **WS has no MCP mapping** — console tail, live stats, job progress streams are
   webui-only; `tail_log` / `server_overview` / `wait_for_job` composites replace them.

## Feature set

### 1. `McpToken` model + migration

`backend/app/models/mcp_token.py` — `id`, `label` (str), `token_hash` (sha256 hex,
unique), `last_used_at: datetime | None`, `expires_at: datetime | None`,
`revoked_at: datetime | None`, + `TimestampMixin` (created/updated). Export from
`models/__init__.py` (fact #4). `schemas/mcp.py`: `McpTokenCreate {label,
expires_at?}`, `McpTokenOut` (never the hash), `McpTokenCreatedOut` (includes the
raw token, once).

### 2. Token management REST — `backend/app/api/mcp_tokens.py`

| Method & path | Purpose |
|---|---|
| `GET /api/mcp/tokens` | List (label, timestamps, revoked flag) |
| `POST /api/mcp/tokens` | Create → **201**, body `{label, expires_at?}`, response carries the raw token |
| `DELETE /api/mcp/tokens/{id}` | Revoke (soft: set `revoked_at`) |

`authed` like every other router; registered in `main.py`. This is the only route group
the webui needs for the feature.

### 3. MCP server — new package `backend/app/mcp/`

- **`auth.py`** — Starlette ASGI middleware wrapping the MCP app: extract Bearer token
  (401 if absent/malformed), sha256-lookup, refuse revoked/expired (401), update
  `last_used_at` (best-effort, own `SessionLocal`), mint JWT via `create_access_token`,
  stash it in a `contextvars.ContextVar` for the current tool call. No DB session is
  held across requests.
- **`api.py`** — one shared `httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
  base_url="http://api")`; `async def call(method, path, json=None, timeout=...)` sets
  `Authorization: Bearer <minted JWT>` from the context var, executes against the real
  app, returns the decoded body; non-2xx raises a typed `ApiError(status_code, detail)`
  the tool layer surfaces verbatim (deterministic errors, HTTP semantics preserved).
- **`tools.py`** — a declarative `ToolSpec` table (`name, method, path, description,
  confirm=False, timeout_s`), one row per mirrored endpoint, registered on
  `FastMCP("reforger-manager")` by a small factory that builds typed tool functions
  from the spec (path params + JSON body as args, `confirm` injected where
  `confirm=True`). Descriptions are hand-written, agent-oriented; job-returning tools
  document `wait_for_job`. ~45 rows, one file.
- **`composites.py`** — read-only orchestration over the adapter, no business logic:
  `wait_for_job(job_id, timeout_s=60)` (polls `GET /api/jobs/{id}` until a terminal
  state; returns the final `JobOut`), `tail_log(server_id, lines=100, hide_spam=true)`
  (slices `GET /api/servers/{id}/log`), `server_overview()` (servers + supervisor
  status + engine build + recent jobs in one call).
- **`__init__.py`** — `mcp = FastMCP("reforger-manager")`, registers the table +
  composites, exposes `streamable_http_app()` pre-wrapped in the auth middleware.

`main.py`: `app.mount("/mcp", mcp_app)` next to the `include_router` block + add
`"mcp"` to the `_spa` excluded prefixes (fact #1).

### 4. Tool surface (derived from router docstrings at implementation)

| Group | Tools (1:1) |
|---|---|
| Servers | list, get, create, update, delete*, start, stop*, config, config_preview, preflight, clone, schedule_restart (arm/get/cancel), stats, rcon (players + command), mods_update_check, mods_update_apply*, pin, unpin |
| Mods | list (filters), detail, dependencies, scan*, add, search, delete* |
| Modpacks | list, create, get, update, delete*, apply-to-server* |
| Engine | status, check, update* |
| Jobs | list, get, cancel*, prune*, delete* |
| Files | list, read content, write*, delete*, mkdir*, rename*, upload* |
| Backup | export, import* (dry_run default true) |
| Settings / storage / scenarios | get + patch* / storage view / resolve |

`*` = `confirm` arg required. Composites on top: `wait_for_job`, `tail_log`,
`server_overview`.

### 5. Frontend — "MCP Tokens" card

`frontend/src/components/settings/McpTokensCard.tsx` (precedent: `NightlyCard.tsx`,
`SessionCard.tsx`; UI kit from `components/ui.tsx`): token list (label, created,
last used, revoked badge), create dialog (label → token shown once with copy button),
revoke button. Wired into `pages/Settings.tsx` next to the existing cards.

### 6. Docs

`README.md`: "Connect an agent" section — token creation via the webui, then
`claude mcp add --transport http reforger-manager https://<host>/mcp --header
"Authorization: Bearer rfm_..."` and the equivalent Codex `config.toml` entry; a note
that reverse proxies must not buffer `/mcp` responses (streaming).

## Data-model changes

One new table (`mcp_tokens`, migration `0003`). No existing table changes.

## Testing

- Token REST: create→201 + raw token once, list omits hash, revoke→subsequent 401.
- Middleware: valid / missing / garbage / revoked / expired tokens; `last_used_at` set.
- Protocol-level, in-process (SDK `Client` against the mounted app, same
  `httpx.ASGITransport` convention as the existing route tests): `list_tools` returns
  the expected names; a read tool returns the API body; a `confirm` tool 400s with
  `confirm=false` and hits the API with `confirm=true`; `wait_for_job` blocks on an
  enqueued fake job and returns its terminal state.
- Adapter error mapping: an API 409/404 surfaces as a deterministic tool error.
- The suite needs no network and no Postgres (sqlite + `dependency_overrides` precedent).

## Cut from this sprint

- **OAuth2 / OAuth flows** — static Bearer token is sufficient for a single-admin
  homelab; the token is revocable and expirable, which covers the real risk.
- **MCP resources / prompts primitives** — tools only; resources (read-only context)
  add surface for little gain when `server_overview` exists. Revisit if agents ask.
- **stdio / SSE transports** — Streamable HTTP only.
- **Rate limiting / per-token scopes** — every token is full admin (matches the webui's
  own model). Revisit only with multi-user support.
- **Serving `/mcp` through the webui's CORS config** — MCP clients are not browsers.

## Non-goals (explicitly out of scope)

- Any change to the webui login/JWT flow or multi-user support.
- Exposing `auth/login`, `health`, or WS routes as tools.
- A second implementation of any orchestration (supervisor, preflight, mods, files).

## Verification

- `cd backend && python -m pytest`
- `cd frontend && npm run build`
- Manual: create a token in the new Settings card, `claude mcp add` it, then from the
  agent: `server_overview`, edit a definition, `start_server`, read the log, run an RCON
  command, `stop_server` (confirm), `wait_for_job` on an engine check — and confirm the
  webui shows identical state throughout.
