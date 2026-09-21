import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { cn } from "../../lib/utils";
import {
  api,
  apiVoid,
  type AppSettings,
  type ConfigResponse,
  type DetailServer,
  type Server,
} from "../../lib/api";
import { Button, Dialog, Input } from "../ui";

/* ------------------------------------------------------------------ *
 * game_properties: the eight keys the structured form knows about.
 * They MERGE over the engine defaults (config_gen.DEFAULT_GAME_PROPERTIES),
 * so a key is only written back when the operator explicitly overrides it.
 * ------------------------------------------------------------------ */
const GP_NUM_KEYS = [
  "serverMaxViewDistance",
  "serverMinGrassDistance",
  "networkViewDistance",
] as const;
const GP_BOOL_KEYS = [
  "disableThirdPerson",
  "fastValidation",
  "battlEye",
  "VONDisableUI",
  "VONDisableDirectSpeechUI",
] as const;
const GP_DEFAULTS: Record<string, number | boolean> = {
  serverMaxViewDistance: 2500,
  serverMinGrassDistance: 50,
  networkViewDistance: 1000,
  disableThirdPerson: false,
  fastValidation: true,
  battlEye: true,
  VONDisableUI: true,
  VONDisableDirectSpeechUI: true,
};
const ALL_GP_KEYS: readonly string[] = [...GP_NUM_KEYS, ...GP_BOOL_KEYS];

/* ------------------------------ helpers ------------------------------ */

type ParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

function parseJsonObject(text: string): ParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Invalid JSON" };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: "Must be a JSON object" };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** Order-independent stringify, for "did this object actually change?" checks. */
function canon(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canon).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canon(record[key])}`)
    .join(",")}}`;
}

const isPositiveInt = (raw: string) => /^\d+$/.test(raw.trim()) && Number(raw.trim()) > 0;
const errText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

/* ------------------------------ form state ------------------------------ */

type FormState = {
  name: string;
  game_name: string;
  scenario_game_id: string;
  game_password: string;
  admin_password: string;
  max_players: string;
  visible: boolean;
  is_favourite: boolean;
  rcon_enabled: boolean;
  rcon_port: string;
  rcon_password: string;
  rcon_permission: string;
  rcon_max_clients: string;
  bind_address: string;
  bind_port: string;
  public_address: string;
  public_port: string;
  a2s_address: string;
  a2s_port: string;
  rcon_address: string;
  persistence_enabled: boolean;
  auto_save_interval: string;
  save_retention: string;
  load_session_save: boolean;
  keep_session_save: boolean;
  hive_id: string;
  advancedGpText: string;
  extraConfigText: string;
};

const CREATE_DEFAULTS: FormState = {
  name: "",
  game_name: "",
  scenario_game_id: "",
  game_password: "",
  admin_password: "",
  max_players: "32",
  visible: true,
  is_favourite: false,
  rcon_enabled: true,
  rcon_port: "19999",
  rcon_password: "",
  rcon_permission: "admin",
  rcon_max_clients: "16",
  bind_address: "",
  bind_port: "2001",
  public_address: "",
  public_port: "2001",
  a2s_address: "0.0.0.0",
  a2s_port: "17777",
  rcon_address: "0.0.0.0",
  persistence_enabled: true,
  auto_save_interval: "10",
  save_retention: "10",
  load_session_save: true,
  keep_session_save: false,
  hive_id: "0",
  advancedGpText: "",
  extraConfigText: "",
};

function initFromServer(server: DetailServer): FormState {
  const gp: Record<string, unknown> = server.game_properties ?? {};
  const extraGp: Record<string, unknown> = { ...gp };
  for (const key of ALL_GP_KEYS) delete extraGp[key];
  return {
    name: server.name,
    game_name: server.game_name ?? "",
    scenario_game_id: server.scenario_game_id ?? "",
    game_password: server.game_password ?? "",
    admin_password: server.admin_password ?? "",
    max_players: String(server.max_players),
    visible: server.visible,
    is_favourite: server.is_favourite,
    rcon_enabled: server.rcon_enabled,
    rcon_port: String(server.rcon_port),
    rcon_password: server.rcon_password ?? "",
    rcon_permission: server.rcon_permission,
    rcon_max_clients: String(server.rcon_max_clients),
    bind_address: server.bind_address,
    bind_port: String(server.bind_port),
    public_address: server.public_address ?? "",
    public_port: String(server.public_port),
    a2s_address: server.a2s_address,
    a2s_port: String(server.a2s_port),
    rcon_address: server.rcon_address,
    persistence_enabled: server.persistence_enabled,
    auto_save_interval: String(server.auto_save_interval),
    save_retention: String(server.save_retention),
    load_session_save: server.load_session_save,
    keep_session_save: server.keep_session_save,
    hive_id: String(server.hive_id),
    advancedGpText: Object.keys(extraGp).length ? JSON.stringify(extraGp, null, 2) : "",
    extraConfigText:
      server.extra_config && Object.keys(server.extra_config).length
        ? JSON.stringify(server.extra_config, null, 2)
        : "",
  };
}

type GpState = {
  set: Record<string, boolean>;
  num: Record<string, string>;
  bool: Record<string, boolean>;
};

function initGp(server: DetailServer | null): GpState {
  const gp: Record<string, unknown> = server?.game_properties ?? {};
  const set: Record<string, boolean> = {};
  const num: Record<string, string> = {};
  const bool: Record<string, boolean> = {};
  for (const key of GP_NUM_KEYS) {
    set[key] = key in gp;
    num[key] = String((gp[key] as number | undefined) ?? (GP_DEFAULTS[key] as number));
  }
  for (const key of GP_BOOL_KEYS) {
    set[key] = key in gp;
    bool[key] = (gp[key] as boolean | undefined) ?? (GP_DEFAULTS[key] as boolean);
  }
  return { set, num, bool };
}

/* ------------------------------ field bits ------------------------------ */

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1 text-xs text-stone-300">
      <span>{label}</span>
      {children}
    </label>
  );
}

function CheckField({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={cn(
        "flex items-center gap-2 text-xs",
        disabled ? "text-stone-600" : "text-stone-300",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <label className="block space-y-1 text-xs text-stone-300">
      <span>{label}</span>
      <select
        className="h-10 w-full border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}

type ResolvedScenario = {
  game_id: string;
  name: string | null;
  game_mode: string | null;
  player_count: number | null;
  mod_guid: string;
  mod_name: string | null;
  source: "db" | "api";
};
type ScenarioResolveResponse = {
  scenarios: ResolvedScenario[];
  failed: Array<{ guid: string; reason: string }>;
};

/**
 * Scenario picker (S6). Two sources feed the dropdown: the operator-configured
 * default scenarios (Settings → Default Scenarios, GET /api/settings — always
 * available, even in create mode with no mods yet) and the definition's CURRENT
 * mod GUIDs (read from the react-query `["server", id]` cache the page already
 * fetched — the component's own signature stays `{value, onChange}`), resolved
 * via POST /api/scenarios/resolve, grouped one `<optgroup>` per mod (plus one
 * for the defaults) so scenarios from different mods don't run together. The
 * dropdown and the free-text box both just write `value` — picking an option
 * overwrites whatever is in the text box, and the dropdown stays enabled (and
 * shows the matching entry highlighted) even when the current value came from
 * free text. With neither source available, just the free-text input renders.
 */
export function ScenarioField({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const { id = "" } = useParams();
  const queryClient = useQueryClient();
  const server = id
    ? queryClient.getQueryData<DetailServer>(["server", id])
    : undefined;
  const modGuids = (server?.mods ?? []).map((m) => m.mod_guid);

  // Fetch once per distinct guid set, not on every keystroke: the query is
  // keyed on the sorted guid list, so free-text typing never re-requests.
  const sortedGuids = useMemo(() => [...modGuids].sort(), [modGuids]);
  const resolve = useQuery({
    queryKey: ["scenarios-resolve", sortedGuids.join(",")],
    queryFn: () =>
      api<ScenarioResolveResponse>("/api/scenarios/resolve", {
        method: "POST",
        body: JSON.stringify({ guids: sortedGuids }),
      }),
    enabled: sortedGuids.length > 0,
    placeholderData: keepPreviousData,
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api<AppSettings>("/api/settings"),
  });

  const scenarios = resolve.data?.scenarios ?? [];
  const defaults = settings.data?.default_scenarios ?? [];
  const failedCount = resolve.data?.failed.length ?? 0;

  // One group per mod (in first-seen order) so scenarios from different mods
  // are visually separated instead of dumped into one flat list.
  const modScenarioGroups = useMemo(() => {
    const order: string[] = [];
    const byMod = new Map<string, { modName: string | null; scenarios: ResolvedScenario[] }>();
    for (const scenario of scenarios) {
      if (!byMod.has(scenario.mod_guid)) {
        order.push(scenario.mod_guid);
        byMod.set(scenario.mod_guid, { modName: scenario.mod_name, scenarios: [] });
      }
      byMod.get(scenario.mod_guid)!.scenarios.push(scenario);
    }
    return order.map((modGuid) => ({ modGuid, ...byMod.get(modGuid)! }));
  }, [scenarios]);
  const loading = settings.isLoading || (sortedGuids.length > 0 && resolve.isLoading);
  const hasOptions = defaults.length > 0 || scenarios.length > 0;

  if (!hasOptions && !loading) {
    return (
      <label className="block space-y-1 text-xs text-stone-300">
        <span>Scenario id</span>
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="{ECC61978EDCC2B5A}Missions/23_Campaign.conf"
        />
        <span className="block text-[10px] text-stone-500">
          Paste a scenario id. Add mods, or configure default scenarios in Settings, to
          pick from a list.
        </span>
      </label>
    );
  }

  return (
    <div className="grid gap-2">
      <div className="grid gap-3">
        <label className="block space-y-1 text-xs text-stone-300">
          <span>Scenario</span>
          <select
            className="h-10 w-full border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
            value={value}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">{loading ? "Loading scenarios..." : "Select a scenario..."}</option>
            {defaults.length > 0 && (
              <optgroup label="Official scenarios">
                {defaults.map((scenario) => (
                  <option key={`default:${scenario.game_id}`} value={scenario.game_id}>
                    {scenario.name}
                  </option>
                ))}
              </optgroup>
            )}
            {modScenarioGroups.map((group) => (
              <optgroup key={group.modGuid} label={group.modName ?? group.modGuid}>
                {group.scenarios.map((scenario) => (
                  <option
                    key={`${scenario.mod_guid}:${scenario.game_id}`}
                    value={scenario.game_id}
                  >
                    {scenario.name ?? scenario.game_id}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <label className="block space-y-1 text-xs text-stone-300">
          <span>Scenario id (or paste a custom one)</span>
          <Input
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder="{ECC61978EDCC2B5A}Missions/23_Campaign.conf"
          />
        </label>
      </div>
      {failedCount > 0 && (
        <span className="text-[10px] text-stone-500">
          couldn&apos;t fetch scenarios for {failedCount} mod(s)
        </span>
      )}
    </div>
  );
}

/* ------------------------------ component ------------------------------ */

type ServerFormProps =
  | { mode: "create"; onCancel?: () => void }
  | { mode: "edit"; server: DetailServer };

export function ServerForm(props: ServerFormProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const serverId = props.mode === "edit" ? props.server.id : null;

  const [form, setForm] = useState<FormState>(() =>
    props.mode === "edit" ? initFromServer(props.server) : { ...CREATE_DEFAULTS },
  );
  const [gp, setGp] = useState<GpState>(() =>
    initGp(props.mode === "edit" ? props.server : null),
  );

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const gpParse = parseJsonObject(form.advancedGpText);
  const extraParse = parseJsonObject(form.extraConfigText);

  const mergedGameProperties = (): Record<string, unknown> | null => {
    const known: Record<string, unknown> = {};
    for (const key of GP_NUM_KEYS) if (gp.set[key]) known[key] = Number(gp.num[key]);
    for (const key of GP_BOOL_KEYS) if (gp.set[key]) known[key] = gp.bool[key];
    const extra = gpParse.ok ? gpParse.value : {};
    const merged = { ...extra, ...known };
    return Object.keys(merged).length ? merged : null;
  };
  const mergedExtraConfig = (): Record<string, unknown> | null => {
    const value = extraParse.ok ? extraParse.value : {};
    return Object.keys(value).length ? value : null;
  };

  const nameOk = form.name.trim().length > 0;
  const rconMax = Number(form.rcon_max_clients.trim());
  const autoSave = Number(form.auto_save_interval.trim());
  const saveRetention = Number(form.save_retention.trim());
  const hiveId = Number(form.hive_id.trim());
  const numbersOk =
    [form.max_players, form.bind_port, form.public_port, form.a2s_port, form.rcon_port].every(
      isPositiveInt,
    ) &&
    /^\d+$/.test(form.rcon_max_clients.trim()) &&
    rconMax >= 1 &&
    rconMax <= 16 &&
    /^\d+$/.test(form.auto_save_interval.trim()) &&
    autoSave >= 0 &&
    autoSave <= 60 &&
    /^\d+$/.test(form.save_retention.trim()) &&
    saveRetention >= 1 &&
    saveRetention <= 128 &&
    /^\d+$/.test(form.hive_id.trim()) &&
    hiveId >= 0 &&
    hiveId <= 16383;
  const jsonOk = gpParse.ok && extraParse.ok;

  const fullBody = () => ({
    name: form.name.trim(),
    game_name: form.game_name.trim() || null,
    scenario_game_id: form.scenario_game_id.trim() || null,
    game_password: form.game_password || null,
    admin_password: form.admin_password || null,
    max_players: Number(form.max_players.trim()),
    visible: form.visible,
    is_favourite: form.is_favourite,
    game_properties: mergedGameProperties(),
    extra_config: mergedExtraConfig(),
    bind_address: form.bind_address.trim(),
    bind_port: Number(form.bind_port.trim()),
    public_address: form.public_address.trim() || null,
    public_port: Number(form.public_port.trim()),
    a2s_address: form.a2s_address.trim(),
    a2s_port: Number(form.a2s_port.trim()),
    rcon_enabled: form.rcon_enabled,
    rcon_address: form.rcon_address.trim(),
    rcon_port: Number(form.rcon_port.trim()),
    rcon_password: form.rcon_password || null,
    rcon_permission: form.rcon_permission,
    rcon_max_clients: Number(form.rcon_max_clients.trim()),
    persistence_enabled: form.persistence_enabled,
    auto_save_interval: Number(form.auto_save_interval.trim()),
    save_retention: Number(form.save_retention.trim()),
    load_session_save: form.load_session_save,
    keep_session_save: form.keep_session_save,
    hive_id: Number(form.hive_id.trim()),
  });

  const buildPatch = (): Record<string, unknown> => {
    if (props.mode !== "edit") return {};
    const server = props.server;
    const patch: Record<string, unknown> = {};
    const orNull = (value: string | null | undefined) => value ?? "";

    if (form.name.trim() !== server.name) patch.name = form.name.trim();
    if (form.bind_address.trim() !== server.bind_address)
      patch.bind_address = form.bind_address.trim();
    if (form.a2s_address.trim() !== server.a2s_address) patch.a2s_address = form.a2s_address.trim();
    if (form.rcon_address.trim() !== server.rcon_address)
      patch.rcon_address = form.rcon_address.trim();
    if (form.rcon_permission !== server.rcon_permission)
      patch.rcon_permission = form.rcon_permission;

    if (form.game_name.trim() !== orNull(server.game_name))
      patch.game_name = form.game_name.trim() || null;
    if (form.scenario_game_id.trim() !== orNull(server.scenario_game_id))
      patch.scenario_game_id = form.scenario_game_id.trim() || null;
    if (form.public_address.trim() !== orNull(server.public_address))
      patch.public_address = form.public_address.trim() || null;
    if (form.game_password !== orNull(server.game_password))
      patch.game_password = form.game_password || null;
    if (form.admin_password !== orNull(server.admin_password))
      patch.admin_password = form.admin_password || null;
    if (form.rcon_password !== orNull(server.rcon_password))
      patch.rcon_password = form.rcon_password || null;

    const numericFields: Array<[string, string, number]> = [
      ["max_players", form.max_players, server.max_players],
      ["bind_port", form.bind_port, server.bind_port],
      ["public_port", form.public_port, server.public_port],
      ["a2s_port", form.a2s_port, server.a2s_port],
      ["rcon_port", form.rcon_port, server.rcon_port],
      ["rcon_max_clients", form.rcon_max_clients, server.rcon_max_clients],
      ["auto_save_interval", form.auto_save_interval, server.auto_save_interval],
      ["save_retention", form.save_retention, server.save_retention],
      ["hive_id", form.hive_id, server.hive_id],
    ];
    for (const [key, raw, current] of numericFields) {
      const parsed = Number(raw.trim());
      if (/^\d+$/.test(raw.trim()) && parsed !== current) patch[key] = parsed;
    }

    if (form.visible !== server.visible) patch.visible = form.visible;
    if (form.is_favourite !== server.is_favourite) patch.is_favourite = form.is_favourite;
    if (form.rcon_enabled !== server.rcon_enabled) patch.rcon_enabled = form.rcon_enabled;
    if (form.persistence_enabled !== server.persistence_enabled)
      patch.persistence_enabled = form.persistence_enabled;
    if (form.load_session_save !== server.load_session_save)
      patch.load_session_save = form.load_session_save;
    if (form.keep_session_save !== server.keep_session_save)
      patch.keep_session_save = form.keep_session_save;

    const gameProperties = mergedGameProperties();
    if (canon(gameProperties) !== canon(server.game_properties ?? null))
      patch.game_properties = gameProperties;
    const extraConfig = mergedExtraConfig();
    if (canon(extraConfig) !== canon(server.extra_config ?? null))
      patch.extra_config = extraConfig;

    return patch;
  };

  const hasChanges = props.mode === "create" || Object.keys(buildPatch()).length > 0;
  const canSubmit = nameOk && numbersOk && jsonOk && !saving && hasChanges;

  /* ---------------------------- live preview ---------------------------- */

  const previewBody =
    props.mode === "edit" && nameOk && numbersOk && jsonOk ? JSON.stringify(fullBody()) : "";
  const [debouncedBody, setDebouncedBody] = useState("");
  useEffect(() => {
    if (!previewBody) return;
    const timer = setTimeout(() => setDebouncedBody(previewBody), 500);
    return () => clearTimeout(timer);
  }, [previewBody]);

  const preview = useQuery({
    queryKey: ["config-preview", serverId, debouncedBody],
    queryFn: () =>
      api<ConfigResponse>(`/api/servers/${serverId}/config/preview`, {
        method: "POST",
        body: debouncedBody,
      }),
    enabled: props.mode === "edit" && debouncedBody !== "",
    placeholderData: keepPreviousData,
  });

  /* ------------------------------ actions ------------------------------ */

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      if (props.mode === "create") {
        const created = await api<Server>("/api/servers", {
          method: "POST",
          body: JSON.stringify(fullBody()),
        });
        await queryClient.invalidateQueries({ queryKey: ["servers"] });
        navigate(`/servers/${created.id}`);
      } else {
        await api<Server>(`/api/servers/${serverId}`, {
          method: "PATCH",
          body: JSON.stringify(buildPatch()),
        });
        await queryClient.invalidateQueries({ queryKey: ["server", String(serverId)] });
        await queryClient.invalidateQueries({ queryKey: ["servers"] });
        setSaved(true);
      }
    } catch (error) {
      setSaveError(errText(error, "Save failed"));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await apiVoid(`/api/servers/${serverId}`, { method: "DELETE" });
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      navigate("/servers");
    } catch (error) {
      setDeleteError(errText(error, "Delete failed"));
      setConfirmDelete(false);
    } finally {
      setDeleting(false);
    }
  };

  const running = props.mode === "edit" && props.server.is_running;

  /* ------------------------------ render ------------------------------ */

  return (
    <>
      <form
        onSubmit={onSubmit}
        className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]"
      >
        <div className="grid gap-5">
          {running && (
            <div className="border border-amber-700 bg-amber-950/50 px-3 py-2 text-xs text-amber-300">
              This server is running &mdash; changes apply on next start.
            </div>
          )}
          {saveError && <p className="error">{saveError}</p>}
          {saved && (
            <p className="text-xs text-emerald-400">
              Saved.{running ? " Changes apply on next start." : ""}
            </p>
          )}

          <section className="grid gap-3">
            <p className="metric-label">Identity</p>
            <Field label="Name">
              <Input value={form.name} onChange={(event) => set("name", event.target.value)} required />
            </Field>
            <Field label="Game name (server browser title)">
              <Input
                value={form.game_name}
                onChange={(event) => set("game_name", event.target.value)}
                placeholder="Defaults to the definition name"
              />
            </Field>
            <ScenarioField
              value={form.scenario_game_id}
              onChange={(value) => set("scenario_game_id", value)}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Game password">
                <Input
                  value={form.game_password}
                  onChange={(event) => set("game_password", event.target.value)}
                />
              </Field>
              <Field label="Admin password">
                <Input
                  value={form.admin_password}
                  onChange={(event) => set("admin_password", event.target.value)}
                />
              </Field>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Max players">
                <Input
                  type="number"
                  value={form.max_players}
                  onChange={(event) => set("max_players", event.target.value)}
                />
              </Field>
            </div>
            <div className="flex flex-wrap gap-4">
              <CheckField
                label="Visible in server browser"
                checked={form.visible}
                onChange={(value) => set("visible", value)}
              />
              <CheckField
                label="Favourite"
                checked={form.is_favourite}
                onChange={(value) => set("is_favourite", value)}
              />
            </div>
          </section>

          <section className="grid gap-3">
            <p className="metric-label">RCON</p>
            <CheckField
              label="RCON enabled"
              checked={form.rcon_enabled}
              onChange={(value) => set("rcon_enabled", value)}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="RCON port">
                <Input
                  type="number"
                  value={form.rcon_port}
                  onChange={(event) => set("rcon_port", event.target.value)}
                />
              </Field>
              <Field label="RCON password">
                <Input
                  value={form.rcon_password}
                  onChange={(event) => set("rcon_password", event.target.value)}
                />
              </Field>
            </div>
            <SelectField
              label="RCON permission"
              value={form.rcon_permission}
              onChange={(value) => set("rcon_permission", value)}
              options={["admin", "monitor"]}
            />
          </section>

          <section className="grid gap-3">
            <p className="metric-label">Game properties</p>
            <p className="text-[10px] text-stone-500">
              Unchecked keys inherit the engine default and are not written to the config.
            </p>
            {GP_NUM_KEYS.map((key) => (
              <div key={key} className="grid grid-cols-[1fr_auto] items-center gap-3">
                <span className="text-xs text-stone-300">{key}</span>
                <div className="flex items-center gap-3">
                  {gp.set[key] ? (
                    <Input
                      type="number"
                      className="h-8 w-28"
                      value={gp.num[key]}
                      onChange={(event) =>
                        setGp((current) => ({
                          ...current,
                          num: { ...current.num, [key]: event.target.value },
                        }))
                      }
                    />
                  ) : (
                    <span className="text-[10px] text-stone-500">
                      inherits {String(GP_DEFAULTS[key])}
                    </span>
                  )}
                  <CheckField
                    label="override"
                    checked={gp.set[key]}
                    onChange={() =>
                      setGp((current) => ({
                        ...current,
                        set: { ...current.set, [key]: !current.set[key] },
                      }))
                    }
                  />
                </div>
              </div>
            ))}
            {GP_BOOL_KEYS.map((key) => (
              <div key={key} className="grid grid-cols-[1fr_auto] items-center gap-3">
                <span className="text-xs text-stone-300">{key}</span>
                <div className="flex items-center gap-3">
                  {gp.set[key] ? (
                    <input
                      type="checkbox"
                      checked={gp.bool[key]}
                      onChange={(event) =>
                        setGp((current) => ({
                          ...current,
                          bool: { ...current.bool, [key]: event.target.checked },
                        }))
                      }
                    />
                  ) : (
                    <span className="text-[10px] text-stone-500">
                      inherits {String(GP_DEFAULTS[key])}
                    </span>
                  )}
                  <CheckField
                    label="override"
                    checked={gp.set[key]}
                    onChange={() =>
                      setGp((current) => ({
                        ...current,
                        set: { ...current.set, [key]: !current.set[key] },
                      }))
                    }
                  />
                </div>
              </div>
            ))}
          </section>

          <section className="grid gap-3">
            <p className="metric-label">Persistence</p>
            <CheckField
              label="Enable save/load persistence"
              checked={form.persistence_enabled}
              onChange={(value) => set("persistence_enabled", value)}
            />
            <div
              className={cn(
                "grid gap-3 sm:grid-cols-2",
                !form.persistence_enabled && "opacity-50",
              )}
            >
              <Field label="Autosave interval (minutes)">
                <Input
                  type="number"
                  min={0}
                  max={60}
                  disabled={!form.persistence_enabled}
                  value={form.auto_save_interval}
                  onChange={(event) => set("auto_save_interval", event.target.value)}
                />
                <span className="block text-[10px] text-stone-500">0 disables autosave</span>
              </Field>
              <Field label="Save points to keep">
                <Input
                  type="number"
                  min={1}
                  max={128}
                  disabled={!form.persistence_enabled}
                  value={form.save_retention}
                  onChange={(event) => set("save_retention", event.target.value)}
                />
              </Field>
              <CheckField
                label="Load latest save on server start"
                checked={form.load_session_save}
                onChange={(value) => set("load_session_save", value)}
                disabled={!form.persistence_enabled}
              />
              <CheckField
                label="Keep saves after mission ends"
                checked={form.keep_session_save}
                onChange={(value) => set("keep_session_save", value)}
                disabled={!form.persistence_enabled}
              />
              <Field label="Hive ID">
                <Input
                  type="number"
                  min={0}
                  max={16383}
                  disabled={!form.persistence_enabled}
                  value={form.hive_id}
                  onChange={(event) => set("hive_id", event.target.value)}
                />
                <span className="block text-[10px] text-stone-500">
                  Only needed when multiple servers share one persistence database
                </span>
              </Field>
            </div>
          </section>

          <details className="border border-stone-800 p-3">
            <summary className="cursor-pointer text-xs font-bold uppercase tracking-wide text-stone-300">
              Advanced
            </summary>
            <div className="mt-3 grid gap-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="bind_address">
                  <Input
                    value={form.bind_address}
                    onChange={(event) => set("bind_address", event.target.value)}
                    placeholder="empty = default interface (same as public_address)"
                  />
                </Field>
                <Field label="bind_port">
                  <Input
                    type="number"
                    value={form.bind_port}
                    onChange={(event) => set("bind_port", event.target.value)}
                  />
                </Field>
                <Field label="public_address">
                  <Input
                    value={form.public_address}
                    onChange={(event) => set("public_address", event.target.value)}
                  />
                </Field>
                <Field label="public_port">
                  <Input
                    type="number"
                    value={form.public_port}
                    onChange={(event) => set("public_port", event.target.value)}
                  />
                </Field>
                <Field label="a2s_address">
                  <Input
                    value={form.a2s_address}
                    onChange={(event) => set("a2s_address", event.target.value)}
                  />
                </Field>
                <Field label="a2s_port">
                  <Input
                    type="number"
                    value={form.a2s_port}
                    onChange={(event) => set("a2s_port", event.target.value)}
                  />
                </Field>
                <Field label="rcon_address">
                  <Input
                    value={form.rcon_address}
                    onChange={(event) => set("rcon_address", event.target.value)}
                  />
                </Field>
                <Field label="rcon_max_clients (1-16)">
                  <Input
                    type="number"
                    value={form.rcon_max_clients}
                    onChange={(event) => set("rcon_max_clients", event.target.value)}
                  />
                </Field>
              </div>
              <label className="block space-y-1 text-xs text-stone-300">
                <span>game_properties &mdash; extra keys not in the form above (JSON object)</span>
                <textarea
                  className="min-h-[6rem] w-full rounded-sm border border-stone-600 bg-stone-950 p-2 font-mono text-xs text-stone-100 outline-none focus:border-amber-400"
                  value={form.advancedGpText}
                  onChange={(event) => set("advancedGpText", event.target.value)}
                  placeholder="{}"
                />
                {!gpParse.ok && <span className="error">{gpParse.error}</span>}
              </label>
              <label className="block space-y-1 text-xs text-stone-300">
                <span>extra_config &mdash; verbatim overrides, deep-merged last (JSON object)</span>
                <textarea
                  className="min-h-[6rem] w-full rounded-sm border border-stone-600 bg-stone-950 p-2 font-mono text-xs text-stone-100 outline-none focus:border-amber-400"
                  value={form.extraConfigText}
                  onChange={(event) => set("extraConfigText", event.target.value)}
                  placeholder="{}"
                />
                {!extraParse.ok && <span className="error">{extraParse.error}</span>}
              </label>
            </div>
          </details>

          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={!canSubmit}>
              {saving
                ? "Saving..."
                : props.mode === "create"
                  ? "Create definition"
                  : "Save changes"}
            </Button>
            {props.mode === "create" && props.onCancel && (
              <Button type="button" variant="ghost" onClick={props.onCancel}>
                Cancel
              </Button>
            )}
            {props.mode === "edit" && !hasChanges && (
              <span className="text-[11px] text-stone-500">No unsaved changes</span>
            )}
            {!jsonOk && (
              <span className="text-[11px] text-red-400">Fix the JSON editors to save</span>
            )}
          </div>

          {props.mode === "edit" && (
            <section className="mt-2 border border-red-900/60 p-3">
              <p className="metric-label">Danger zone</p>
              <p className="mb-2 mt-1 text-[11px] text-stone-400">
                Deleting removes the definition and its mod set. The API refuses while the server
                is running.
              </p>
              <Button type="button" variant="outline" onClick={() => setConfirmDelete(true)}>
                Delete definition
              </Button>
              {deleteError && <p className="error mt-2">{deleteError}</p>}
            </section>
          )}
        </div>

        <aside className="grid content-start gap-2">
          <p className="metric-label">
            Live preview{preview.isFetching ? " (updating...)" : ""}
          </p>
          {props.mode === "create" ? (
            <p className="text-xs text-stone-400">
              Save once to enable the live config preview.
            </p>
          ) : preview.isError ? (
            <p className="error">{errText(preview.error, "Preview failed")}</p>
          ) : (
            <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-words border border-stone-800 bg-stone-950 p-3 text-xs">
              {preview.data
                ? JSON.stringify(preview.data.config, null, 2)
                : "Waiting for input..."}
            </pre>
          )}
        </aside>
      </form>

      <Dialog
        open={confirmDelete}
        title="Delete definition"
        onClose={() => !deleting && setConfirmDelete(false)}
      >
        <div className="space-y-4">
          <p className="text-xs text-stone-300">
            This permanently removes the definition and its mod set. This cannot be undone.
          </p>
          {deleteError && <p className="error">{deleteError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmDelete(false)}
              disabled={deleting}
            >
              Cancel
            </Button>
            <Button type="button" onClick={onDelete} disabled={deleting}>
              {deleting ? "Deleting..." : "Delete"}
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}
