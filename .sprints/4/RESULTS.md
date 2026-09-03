# Reforger Manager — Sprint 4 results

> Implementation of [PLAN.md](PLAN.md) / [STORIES.md](STORIES.md).
> Completed 2026-09-04 on the local dev checkout (`D:\GitHub\reforger-manager`).

## Status: complete

All nine stories (S1–S9) implemented, plus a gap-closure pass on the tool
table. Backend suite green (**282 passed**, baseline 215 → +67 for this
feature); frontend `npm run build` green.

## What shipped

- **Token backend** — `McpToken` model + migration `0003_mcp_tokens` (both
  `create_all` and `alembic` paths); token REST (`GET`/`POST /api/mcp/tokens`
  → 201 with the raw `rfm_...` token returned exactly once, `DELETE
  /{id}` soft-revoke). Only the sha256 hash is stored.
- **Settings "MCP Tokens" card** (`McpTokensCard.tsx`) — token list (label,
  created, last used, revoked badge), label-only create dialog with the
  one-time token + copy button, revoke per row.
- **`app/mcp/` package** —
  `auth.py` (ASGI middleware: Bearer `rfm_...` → sha256 lookup → refuse
  revoked/expired → stamp `last_used_at` → mint an app JWT into a
  `ContextVar`),
  `api.py` (shared `httpx.AsyncClient` over `ASGITransport` against the real
  app; `ApiError(status_code, detail)` surfaced verbatim),
  `tools.py` (declarative `ToolSpec` table),
  `composites.py` (`wait_for_job` / `tail_log` / `server_overview`).
  Mounted in-process at `/mcp` next to the router block, with `"mcp"` added to
  the SPA catch-all's excluded prefixes (fact #1).
- **Tool surface** — 55-row `ToolSpec` table (**24 reads + 31 confirm-gated
  mutations**) + the 3 composites. Mutating tools take `confirm: bool = false`
  and never touch the API without it; job-returning tools return
  `JobEnqueuedOut` verbatim and point at `wait_for_job` in their description.
- **Tests** — protocol-level via the SDK `Client` over the same
  `ASGITransport` convention: token REST, middleware auth (missing/garbage/
  revoked/expired), read tools, confirm-gated mutations, composites, and an
  e2e arc (token → `server_overview` → create → start → `tail_log` → stop →
  delete → revoke → 401).
- **Docs** — README "Connect an agent" section (token creation, `claude mcp
  add`, Codex `config.toml`, proxy non-buffering note).

## Deviations from PLAN

- **`mcp==2.1.1`** — the SDK renamed `FastMCP` → `MCPServer` and its HTTP
  transport runs on a separate `httpx2` client; pinned in `requirements.txt`.
- **Mounting quirks** — `/mcp` is registered as a plain Starlette `Route`
  (a `Mount` compiles to `<path>/{path}` and never matches the bare `/mcp`
  URL on Starlette 0.48); main.py's lifespan enters the session manager
  (`mcp_server.session_manager.run()`), since Starlette does not propagate
  lifespan to routes; the SDK's DNS-rebinding protection is disabled via
  `host="0.0.0.0"` (it only allows `Host: localhost`, which no proxied
  deployment hits) — auth stays in our middleware.
- **Tool table grew beyond the estimate** — PLAN §3 said "~45 rows": 55
  shipped. `create_server` was missing from the S4/S5 tables and was added
  during S7; 8 more rows (server update/clone, restart-schedule arm+cancel,
  RCON command, server mods-update-check, mod pin/unpin) were added in a
  gap-closure pass to honour PLAN §4's 1:1 decision.
- **Re-revoke is an idempotent 204** (S2 asked to pick one; chosen and tested).
- **UI create dialog is label-only** — `expires_at` is settable via the API
  (`POST /api/mcp/tokens`), not from the UI.

## Deferred / known surface notes

- No tool rows for: mod-library verify/download/pin, modpack
  create-from-server/export/import, and the file download/archive endpoints —
  all absent from PLAN §4's table, which scoped the 1:1 mirror. Easy table
  rows if agents ask.
- WS streams (console tail, live stats, job progress) remain webui-only, per
  fact #8; the composites replace them for agents.
