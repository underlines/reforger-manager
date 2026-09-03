# Reforger Manager — Sprint 4 implementation stories

> Context, decisions and the full feature spec live in [PLAN.md](PLAN.md).
> `fact #n` = PLAN.md *Code-level facts*; `§n` = its *Feature set* number.
> Every path reference was verified against the tree at sprint start — if a line
> number has drifted, trust the symbol name.

## Shape of the sprint

Nine stories, four phases, built bottom-up: dependency → token backend → MCP
skeleton → tools → tests → UI/docs. The whole MCP surface is one new backend package
(`app/mcp/`), one new router, one model, one migration, one frontend card.

| Phase | Stories | Theme |
|---|---|---|
| 0 | S1–S2 | Dependency pin · `mcp_tokens` model + REST |
| 1 | S3–S4 | `app/mcp/` skeleton (auth + adapter + mount) · read tools |
| 2 | S5–S6 | Mutation tools (+confirm) · composite tools |
| 3 | S7–S9 | Protocol tests · Settings card · docs |

**Parallelism.** S1 blocks S3+. S2 blocks S3 (middleware validates tokens) and S8.
S3 blocks S4/S5/S6. S4→S5 sequential (same file/table). S6 after S5. S7 after S6;
S8 anytime after S2; S9 last.

**Verification for every story**: `cd backend && python -m pytest`
(frontend stories: `cd frontend && npm run build`).

---

## Shared reference (read before starting any story)

| Thing | Where | Notes |
|---|---|---|
| Router registration | `app/main.py:283-295` `_API = "/api"` block | Add the tokens router + `app.mount("/mcp", ...)` **here** (fact #1). |
| SPA catch-all | `app/main.py:308` `_spa`, prefix check `:310` | Add `"mcp"` to the excluded tuple (fact #1). Mount must precede this route's registration — it does if placed with the routers. |
| JWT mint | `app/core/security.py:46` `create_access_token(subject)` | `sub=settings.admin_username` (fact #3); `get_current_user` needs an active `User` row. |
| Session pattern outside FastAPI DI | `app/main.py:124` `_bootstrap_admin`, `api/jobs.py` `SessionLocal()` | MCP middleware/composites open + commit + close their own `SessionLocal()` — never a request-scoped session. |
| Endpoint inventory | Router module docstrings (fact #5) | `servers.py:1-17`, `jobs.py:1-7`, `engine.py`, `mods.py`, `modpacks.py`, `server_files.py`, `backup.py`, `settings.py`, `storage.py`, `scenarios.py`. |
| Model + migration precedent | `models/app_settings.py`, `migrations/versions/0002_app_settings.py` | Copy the pattern for `McpToken` + `0003`. Export from `models/__init__.py` (fact #4). |
| Schema precedent | `schemas/job.py` (`JobOut`, `JobEnqueuedOut`) | `schemas/mcp.py` follows. `TERMINAL_JOB_STATES` from `models/base.py`. |
| Test conventions | `tests/test_scenarios_routes.py`, `tests/test_settings_routes.py` | `unittest.IsolatedAsyncioTestCase`; throwaway `FastAPI()` or the real `app` with `dependency_overrides[get_session]`; `httpx.AsyncClient(transport=httpx.ASGITransport(app=...))`; sqlite in-memory. This is also the adapter's production mechanism (fact #2). |
| Router boilerplate | `api/settings.py` | `APIRouter`, `authed = [Depends(get_current_user)]`, `response_model`, commit pattern. |
| Frontend card precedent | `components/settings/NightlyCard.tsx`, `SessionCard.tsx` | Card list + dialog + Button variants; wire into `pages/Settings.tsx`. UI kit: `components/ui.tsx`. |
| JSON transport | `src/lib/api.ts:137` `api<T>()`, `:135` `getAccessToken()` | The card uses `api<T>()`; no multipart anywhere. |

---

## Phase 0 — Dependency + token backend

### S1 — Pin the MCP SDK · fact #6

**Deliver.**
1. Create a scratch venv; `pip install "fastapi==0.118.0" "uvicorn[standard]==0.34.0" "mcp[cli]"`; `pip check` + `python -c "import mcp, fastapi"`.
2. On success: pin the resolved `mcp` version in `backend/requirements.txt` (comment: SDK pinned for its Starlette/anyio range). On failure: try the latest `mcp` that co-installs; if none, stop and report — do not downgrade fastapi.

**Done when.** Fresh `pip install -r requirements.txt` succeeds with `pip check` clean.

**Watch out.** `mcp[cli]` extras (typer, rich) are unneeded in the image — plain `mcp` if the pin resolves cleanly without them.

### S2 — `McpToken` model, migration 0003, token REST · §1, §2, facts #4 #7 #8

**Deliver.**
1. `app/models/mcp_token.py`: `id`, `label: str`, `token_hash: str` (unique, sha256 hex), `last_used_at`, `expires_at`, `revoked_at` (nullable datetimes), `TimestampMixin`. Export in `models/__init__.py`.
2. `migrations/versions/0003_mcp_tokens.py` copying `0002`'s shape (both `create_all` and `alembic` startup paths must produce it).
3. `app/schemas/mcp.py`: `McpTokenCreate {label: str, expires_at: datetime | None}`, `McpTokenOut {id, label, created_at, last_used_at, expires_at, revoked_at}`, `McpTokenCreatedOut` = `McpTokenOut` + `token: str`.
4. New `app/api/mcp_tokens.py` (`GET ""`, `POST ""` → 201, `DELETE "/{id}"` soft-revoke; `authed` like `api/settings.py`); register in `main.py`.
5. Token mint: `secrets.token_urlsafe(32)` prefixed `rfm_` (fact #7); store `hashlib.sha256(token.encode()).hexdigest()` only; response carries the raw token exactly once. Reject `expires_at` in the past (422).
6. `tests/test_mcp_tokens_routes.py`: 201 + token once, list omits hash, revoke then re-revoke (idempotent 204 or 404 — pick and test one), 401s without auth.

**Done when.** `pytest` green; `alembic upgrade head` on a scratch sqlite DB creates `mcp_tokens`.

**Watch out.** Do not add a `McpToken.user_id` — single-admin model (PLAN Non-goals).

---

## Phase 1 — MCP skeleton

### S3 — `app/mcp/` package: auth middleware + API adapter + mount · §3, facts #1 #2 #3

**Deliver.**
1. `app/mcp/auth.py` — ASGI middleware around the MCP app: parse `Authorization: Bearer rfm_...` (401 otherwise); sha256 lookup in a fresh `SessionLocal()`; refuse expired (`expires_at` past) or revoked (`revoked_at` set) — 401; update `last_used_at`; mint `create_access_token(settings.admin_username)` into a `contextvars.ContextVar`; close the session before dispatch (no DB handle held during the call).
2. `app/mcp/api.py` — module-level lazy `httpx.AsyncClient(transport=httpx.ASGITransport(app=<the FastAPI app>), base_url="http://api")`; `async def call(method, path, *, json=None, timeout_s=60) -> dict` reads the ContextVar for the Bearer header, decodes JSON, raises `ApiError(status_code, detail)` on non-2xx. Resolve the app by import inside the function (avoid import cycle `main ↔ mcp`).
3. `app/mcp/__init__.py` — `mcp = FastMCP("reforger-manager")`; register one smoke tool `ping()` returning `call("GET", "/api/health")`; `def mcp_app()` returns the streamable-HTTP ASGI app wrapped in the auth middleware.
4. `main.py`: import + `app.mount("/mcp", mcp_app())` beside the `include_router` block; add `"mcp"` to `_spa`'s excluded prefixes (fact #1).
5. `tests/test_mcp_auth.py` — drive the mounted app over ASGITransport: no/garbage/revoked/expired token → 401; valid token → MCP initialize succeeds (SDK `Client`), `ping` returns the health body.

**Done when.** `pytest` green; `GET /mcp` on the running app no longer returns `index.html`; a `POST /mcp` MCP initialize round-trips with a valid token.

**Watch out.** `FastMCP.streamable_http_app()` may expect its own sub-path — mount so the client URL is exactly `https://<host>/mcp` and verify with a real initialize call, not just a 200. Keep `SessionLocal` usage short-lived (Shared reference row 4).

### S4 — Read tool table · §3, §4

**Deliver.**
1. `app/mcp/tools.py` — `@dataclass(frozen=True) ToolSpec {name, method, path, description, arg_model?, timeout_s}` plus a factory that registers each spec as a FastMCP tool: typed args from `arg_model` (Pydantic) or path params, `confirm=False` default not injected yet (S5). Mirrors read-only endpoints: servers (list/get/config/config_preview/preflight/stats/players/restart-schedule-get), mods (list/detail/dependencies/search), modpacks (list/get), engine (status/check), jobs (list/get), files (list/content), backup export, settings get, storage view, scenarios resolve. Descriptions: agent-oriented, from the routers' docstrings (fact #5) — not raw path strings.
2. Wire the factory into `__init__.py`.
3. `tests/test_mcp_tools_read.py` — protocol-level via SDK `Client`: `list_tools` includes the expected names; `list_servers` and `get_server` (created via the API fixture) round-trip; an API 404 (unknown id) surfaces as a deterministic tool error carrying status + detail.

**Done when.** `pytest` green; `list_tools` output is self-describing enough for an agent to pick correctly (peer-review the descriptions).

**Watch out.** Job- and 202-endpoints (scan/check/update) are *not* read tools — they land in S5 with confirm. `stats` can 502/504 when the server is down — description must say so.

---

## Phase 2 — Mutations + composites

### S5 — Mutation tools with `confirm` · §3, §4

**Deliver.**
1. Extend `ToolSpec` with `confirm: bool = False`; the factory adds a trailing `confirm: bool = False` arg and returns a 400-style tool error (`ApiError(400, "...")`) unless true — the API call is never made without it.
2. Confirm-gated rows (PLAN §4 starred list): server delete/stop/start? (start = yes, confirm — it boots a LAN-facing process), mods_update_apply, scan, add, mod delete, modpack create/update/delete/apply, engine update, jobs cancel/prune/delete, file write/delete/mkdir/rename/upload, backup import, settings patch. Body-bearing tools take a Pydantic `arg_model` mirroring the API schema (e.g. `ServerCreate`, `ServerUpdate`, `RconCommandIn` — reuse `app/schemas/*` models directly where possible).
3. `tests/test_mcp_tools_mutations.py` — for three representative tools (stop_server, delete_server, patch_settings): `confirm=False` → error, **and** the API state unchanged (assert via the API); `confirm=True` → the API call happened; job-returning tools return `JobEnqueuedOut` with `job_id`.

**Done when.** `pytest` green; every mutating tool's description states its side effect and, for job tools, points at `wait_for_job`.

**Watch out.** `start_server` does not take `confirm` in the API — the gate is purely at the MCP layer; never bypass it by calling the API another way from tools.

### S6 — Composite tools · §3 (composites), facts #7 #8

**Deliver.**
1. `app/mcp/composites.py` — three FastMCP tools over `api.call` only:
   - `wait_for_job(job_id: int, timeout_s: int = 60, poll_s: float = 1.0)` — poll `GET /api/jobs/{job_id}` until `state` in `TERMINAL_JOB_STATES`, return the final `JobOut` (includes `result`/`error`/`log_tail`); on timeout return the current non-terminal state with a `timed_out: true` marker — never raise.
   - `tail_log(server_id: int, lines: int = 100, hide_spam: bool = True)` — `GET /api/servers/{id}/log` and return the last `lines` (clamp 1..1000).
   - `server_overview()` — servers list + running status (which id the supervisor runs) + engine row + 5 most recent jobs, one dict.
2. Register alongside the table in `__init__.py`.
3. `tests/test_mcp_composites.py` — `wait_for_job` against an enqueued fake job (monkeypatch the registered handler or use a job-kind the test env enqueues instantly); timeout path returns `timed_out`; `tail_log` slicing on a written fixture log; `server_overview` shape.

**Done when.** `pytest` green.

**Watch out.** Composites open their own `SessionLocal` only if they need one at all — prefer routing through `api.call` for everything (that is the design); the supervisor status in `server_overview` comes from `GET /api/servers` + `supervisor.active_server_id` via a read API, or `supervisor` directly — pick the API-first option unless no endpoint carries it (then direct import is fine, read-only).

---

## Phase 3 — Tests, UI, docs

### S7 — Full protocol-level regression pass

**Deliver.** One `tests/test_mcp_end_to_end.py` (or extend S3's file): fresh token via the REST route → SDK `Client` over ASGITransport → `server_overview` → create server via MCP → `start` (confirm) → `tail_log` → RCON-less stats tolerated when down → `stop` (confirm) → `delete` (confirm) → revoke token → next call 401. Uses the same sqlite/`dependency_overrides` fixtures as the rest of the suite; the supervisor is monkeypatched/mocked so no engine binary is needed (see `tests/test_schedule_restart.py` for supervisor mocking).

**Done when.** Full `pytest` green; no test touches the network or spawns a process.

**Watch out.** Keep the supervisor mock narrow — assert on the API calls the tools make, not on supervisor internals.

### S8 — Frontend "MCP Tokens" card · §5

**Deliver.** `frontend/src/components/settings/McpTokensCard.tsx` (precedent `NightlyCard.tsx`): token table (label, created, last used, revoked badge), create Dialog (label input → `POST /api/mcp/tokens` → one-time token Dialog with copy-to-clipboard), revoke Button per row with the app's existing confirm pattern. Wire into `pages/Settings.tsx`. `api<T>()` only — no raw fetch needed.

**Done when.** `npm run build` clean; manual pass: create → token shown once → listed (no hash) → revoke → badge.

**Watch out.** Token value must live in component state only after the 201 — never refetchable. Follow the local `errText` convention for error text.

### S9 — Docs · §6

**Deliver.**
1. `README.md` — "Connect an agent" section: create a token in Settings → `claude mcp add --transport http reforger-manager https://<host>/mcp --header "Authorization: Bearer rfm_..."`; equivalent Codex `config.toml` `[mcp_servers.reforger-manager]` block; note that reverse proxies must not buffer `/mcp`; pointer to PLAN's tool naming.
2. `.sprints/4/RESULTS.md` — brief: what shipped, deviations from PLAN, anything deferred.

**Done when.** The README steps work verbatim against a running stack for at least the `claude` CLI.

**Watch out.** Token examples in docs must use `rfm_<redacted>` placeholders — never a real token.
