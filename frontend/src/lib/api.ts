export type Engine = {
  id: number;
  installed_build: string | null;
  installed_version: string | null;
  latest_build: string | null;
  latest_version: string | null;
  update_available: boolean;
  last_checked: string | null;
  last_updated_at: string | null;
};
export type User = { id: number; username: string; is_admin: boolean; is_active: boolean };
export type LoginResponse = { access_token: string; token_type: string; expires_in: number };
export type ServerMod = {
  id: number;
  mod_guid: string;
  mod_name: string | null;
  load_order: number;
  enabled: boolean;
  pinned_version: string | null;
  pinned_at_build: string | null;
  pinned_reason: string | null;
  pinned_at: string | null;
};
export type Server = {
  id: number;
  name: string;
  scenario_game_id: string | null;
  game_name: string | null;
  max_players: number;
  is_favourite: boolean;
  is_running: boolean;
  last_state: string | null;
  last_diagnosis: Record<string, unknown> | null;
  bind_port: number;
  a2s_port: number;
  rcon_port: number;
  mods: ServerMod[];
};
export type Mod = {
  guid: string;
  name: string | null;
  installed_version: string | null;
  latest_version: string | null;
  size: number | null;
  api_state: string;
  is_local: boolean;
  pinned_version: string | null;
  has_update: boolean;
  stale_pin: boolean;
};
export type ModpackItem = {
  mod_guid: string;
  load_order: number;
  mod_name: string | null;
};
export type Modpack = {
  id: number;
  name: string;
  description: string | null;
  items: ModpackItem[];
  created_at: string | null;
  updated_at: string | null;
};
export type Job = {
  id: number;
  kind: string;
  state: string;
  progress: number;
  current_step: string | null;
  error: string | null;
  created_at: string | null;
};
export type DetailServer = Server & {
  config_revision: number;
  pid: number | null;
  last_exit_code: number | null;
  last_started_at: string | null;
  last_stopped_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  bind_address: string;
  bind_port: number;
  public_address: string | null;
  public_port: number;
  a2s_address: string;
  a2s_port: number;
  rcon_enabled: boolean;
  rcon_address: string;
  rcon_port: number;
  rcon_permission: string;
  rcon_max_clients: number;
  visible: boolean;
  is_favourite: boolean;
  game_password: string | null;
  admin_password: string | null;
  rcon_password: string | null;
  game_properties: Record<string, unknown> | null;
  extra_config: Record<string, unknown> | null;
};
export type ConfigResponse = { server_id: number; config: Record<string, unknown>; path: string };
export type Preflight = {
  verdict: "green" | "warn" | "blocked";
  checks: Array<{ name: string; level: string; detail: string; fix?: string }>;
  resolved_mods: unknown[];
};
export type LogLine = { text: string; severity: string | null; is_spam: boolean; line_number?: number };
export type LogResponse = { lines: LogLine[]; truncated?: boolean; scanned_bytes?: number };
export type Player = {
  id?: string | number;
  name?: string;
  ip?: string;
  ping?: string | number;
  [key: string]: unknown;
};
export type AppSettings = {
  nightly_check_enabled: boolean;
  nightly_check_hour: number;
  log_spam_patterns: string[];
};
export type Stats = { current: Record<string, unknown>; history: Array<Record<string, unknown>> };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let accessToken: string | null = null;

export const setAccessToken = (token: string | null) => {
  accessToken = token;
};
export const getAccessToken = () => accessToken;

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    if (response.status === 401) window.dispatchEvent(new Event("auth:expired"));
    throw new ApiError(response.status, payload?.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

/** Like {@link api} but for endpoints that answer 204 / empty body (e.g. DELETE). */
export async function apiVoid(path: string, init: RequestInit = {}): Promise<void> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    if (response.status === 401) window.dispatchEvent(new Event("auth:expired"));
    throw new ApiError(response.status, payload?.detail ?? `Request failed (${response.status})`);
  }
}

export const apiClient = {
  login: (username: string, password: string) =>
    api<LoginResponse>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  me: () => api<User>("/api/auth/me"),
  health: () => api<{ status: string }>("/api/health"),
  engine: () => api<Engine>("/api/engine"),
  servers: () => api<Server[]>("/api/servers"),
  server: (id: string) => api<Server>(`/api/servers/${id}`),
  mods: () => api<Mod[]>("/api/mods"),
  jobs: () => api<Job[]>("/api/jobs"),
};

export function websocket(path: string) {
  if (!accessToken) throw new Error("No session token available");
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${protocol}//${window.location.host}${path}?token=${encodeURIComponent(accessToken)}`);
}