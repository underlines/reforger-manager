import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Empty } from "../Empty";
import { Badge, Button, Input } from "../ui";
import { api, apiVoid, type Ban, type BansResponse, type Player, type Server } from "../../lib/api";

type Schedule = {
  armed: boolean;
  restart_at: string | null;
  warn_at: number[];
  seconds_remaining: number | null;
};

type Unit = "minutes" | "seconds";

const UNIT_FACTOR: Record<Unit, number> = { minutes: 60, seconds: 1 };

const BANS_PER_PAGE = 25;

type DurationPreset = "3600" | "86400" | "0" | "custom";

const DURATION_LABEL: Record<Exclude<DurationPreset, "custom">, string> = {
  "3600": "1 hour",
  "86400": "24 hours",
  "0": "Permanent",
};

/**
 * Ban a player offline (by UID / name) or a live player (identifier locked to
 * their session playerId). `POST /api/servers/{id}/bans` — the backend's single-
 * token rule means multi-word names must go through the raw command box.
 */
function BanForm({
  serverId,
  lockedIdentifier,
  onDone,
}: {
  serverId: string;
  lockedIdentifier?: string;
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  const [identifier, setIdentifier] = useState(lockedIdentifier ?? "");
  const [preset, setPreset] = useState<DurationPreset>("86400");
  const [customSeconds, setCustomSeconds] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const target = (lockedIdentifier ?? identifier).trim();
    if (!target) {
      setError("Enter a UID or single-token player name to ban.");
      return;
    }
    const duration_seconds = preset === "custom" ? Number(customSeconds) : Number(preset);
    if (!Number.isFinite(duration_seconds) || duration_seconds < 0) {
      setError("Enter a non-negative duration in seconds (0 = permanent).");
      return;
    }
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      await api<{ echo: string }>(`/api/servers/${serverId}/bans`, {
        method: "POST",
        body: JSON.stringify({ identifier: target, duration_seconds, reason: reason.trim() || null }),
      });
      await queryClient.invalidateQueries({ queryKey: ["server-bans", serverId] });
      setOk("Ban submitted.");
      setReason("");
      if (!lockedIdentifier) setIdentifier("");
      onDone?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Ban failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="grid gap-2 border border-stone-800 bg-stone-950/60 p-3" onSubmit={submit}>
      {!lockedIdentifier && (
        <label className="block space-y-1 text-xs text-stone-300">
          <span>Identifier (UID or single-token player name)</span>
          <Input
            value={identifier}
            onChange={(event) => setIdentifier(event.target.value)}
            placeholder="76561198... or a name"
          />
        </label>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="block space-y-1 text-xs text-stone-300">
          <span>Duration</span>
          <select
            aria-label="Ban duration"
            value={preset}
            onChange={(event) => setPreset(event.target.value as DurationPreset)}
            className="h-8 border border-stone-600 bg-stone-950 px-2 text-sm text-stone-100"
          >
            <option value="3600">{DURATION_LABEL["3600"]}</option>
            <option value="86400">{DURATION_LABEL["86400"]}</option>
            <option value="0">{DURATION_LABEL["0"]}</option>
            <option value="custom">Custom (seconds)</option>
          </select>
        </label>
        {preset === "custom" && (
          <label className="block space-y-1 text-xs text-stone-300">
            <span>Seconds</span>
            <Input
              className="w-28"
              type="number"
              min="0"
              value={customSeconds}
              onChange={(event) => setCustomSeconds(event.target.value)}
              placeholder="Seconds"
            />
          </label>
        )}
        <label className="block flex-1 space-y-1 text-xs text-stone-300">
          <span>Reason (optional)</span>
          <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Reason" />
        </label>
        <Button size="sm" type="submit" disabled={busy}>
          {busy ? "Banning..." : "Ban"}
        </Button>
      </div>
      {error && <p className="error">{error}</p>}
      {ok && <p className="text-xs text-emerald-400">{ok}</p>}
    </form>
  );
}

export function RconPanel({ id }: { id: string }) {
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [response, setResponse] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [unit, setUnit] = useState<Unit>("minutes");
  const [page, setPage] = useState(1);
  const [banFor, setBanFor] = useState<string | null>(null);
  const [rawCommand, setRawCommand] = useState("");
  const [rawResponse, setRawResponse] = useState<string | null>(null);

  const players = useQuery({
    queryKey: ["server-players", id],
    queryFn: () => api<{ players: Player[]; raw?: string }>(`/api/servers/${id}/players`),
    refetchInterval: 5000,
  });
  const serverQuery = useQuery({
    queryKey: ["server", id],
    queryFn: () => api<Server>(`/api/servers/${id}`),
  });
  const bans = useQuery({
    queryKey: ["server-bans", id, page],
    queryFn: () => api<BansResponse>(`/api/servers/${id}/bans?page=${page}`),
  });
  const schedule = useQuery({
    queryKey: ["server-restart-schedule", id],
    queryFn: () => api<Schedule>(`/api/servers/${id}/schedule-restart`),
    refetchInterval: 10000,
  });

  const gameAdmins = serverQuery.data?.game_admins ?? [];
  const bannedUids = new Set((bans.data?.bans ?? []).map((ban) => ban.uid));

  const armed = schedule.data?.armed === true && typeof schedule.data.seconds_remaining === "number";
  const [deadline, setDeadline] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const remaining = schedule.data?.armed ? schedule.data.seconds_remaining : null;
    setNow(Date.now());
    setDeadline(typeof remaining === "number" ? Date.now() + remaining * 1000 : null);
  }, [schedule.data]);
  useEffect(() => {
    if (deadline === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);
  const remaining = deadline === null ? null : Math.max(0, Math.ceil((deadline - now) / 1000));

  const rcon = async (command: string) => {
    const result = await api<{ response: unknown }>(`/api/servers/${id}/rcon`, {
      method: "POST",
      body: JSON.stringify({ command }),
    });
    return typeof result.response === "string" ? result.response : JSON.stringify(result.response);
  };

  const runRcon = async (key: string, command: string) => {
    setBusy(key);
    setError(null);
    try {
      setResponse(await rcon(command));
      await players.refetch();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "RCON command failed");
    } finally {
      setBusy(null);
    }
  };

  const send = async (value: "#restart" | "#shutdown" | "#say") => {
    const text = value === "#say" ? message.trim() : "";
    if (value === "#say" && !text) {
      setError("Enter a message before sending it to players.");
      return;
    }
    if ((value === "#restart" || value === "#shutdown") && !window.confirm(`${value.slice(1)} the active server?`)) return;
    await runRcon(value, value === "#say" ? `#say ${text}` : value);
    if (value === "#say") setMessage("");
  };

  const moderate = async (verb: "kick" | "ban", player: Player) => {
    const label = player.name ?? `player ${player.id ?? "?"}`;
    if (!window.confirm(`${verb === "kick" ? "Kick" : "Ban"} ${label} (id ${player.id})?`)) return;
    // The player id from parse_players is exactly the integer the RCON
    // whitelist wants: `#kick N` / `#ban N`, no reason string.
    await runRcon(`${verb}-${player.id}`, `#${verb} ${player.id}`);
  };

  const removeBan = async (ban: Ban) => {
    if (!window.confirm(`Remove ban ${ban.ban_id || ban.uid}?`)) return;
    setBusy(`unban-${ban.ban_id || ban.uid}`);
    setError(null);
    try {
      await apiVoid(`/api/servers/${id}/bans/${ban.uid}`, { method: "DELETE" });
      await queryClient.invalidateQueries({ queryKey: ["server-bans", id] });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Removing the ban failed");
    } finally {
      setBusy(null);
    }
  };

  const setAdmin = async (player: Player, makeAdmin: boolean) => {
    if (!player.uid) return;
    const next = makeAdmin
      ? [...gameAdmins, player.uid]
      : gameAdmins.filter((uid) => uid !== player.uid);
    setBusy(`admin-${player.uid}`);
    setError(null);
    try {
      await api(`/api/servers/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ game_admins: next }),
      });
      await queryClient.invalidateQueries({ queryKey: ["server", id] });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Updating admins failed");
    } finally {
      setBusy(null);
    }
  };

  const sendRaw = async () => {
    const command = rawCommand.trim();
    if (!command) {
      setError("Enter a command before sending it.");
      return;
    }
    setBusy("raw");
    setError(null);
    try {
      const result = await api<{ response: unknown }>(`/api/servers/${id}/rcon?raw=1`, {
        method: "POST",
        body: JSON.stringify({ command }),
      });
      setRawResponse(
        typeof result.response === "string" ? result.response : JSON.stringify(result.response),
      );
      await players.refetch();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "RCON command failed");
    } finally {
      setBusy(null);
    }
  };

  const copyUid = (uid: string) => {
    void navigator.clipboard?.writeText(uid);
  };

  const armRestart = async () => {
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Enter a positive delay before arming the restart.");
      return;
    }
    const in_seconds = Math.round(value * UNIT_FACTOR[unit]);
    setBusy("schedule-arm");
    setError(null);
    try {
      await api<Schedule>(`/api/servers/${id}/schedule-restart`, {
        method: "POST",
        body: JSON.stringify({ in_seconds }),
      });
      setAmount("");
      await schedule.refetch();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Scheduling the restart failed");
    } finally {
      setBusy(null);
    }
  };

  const cancelRestart = async () => {
    if (!window.confirm("Cancel the scheduled restart?")) return;
    setBusy("schedule-cancel");
    setError(null);
    try {
      await api<Schedule>(`/api/servers/${id}/schedule-restart`, { method: "DELETE" });
      await schedule.refetch();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Cancelling the restart failed");
    } finally {
      setBusy(null);
    }
  };

  const formatClock = (total: number) => {
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  };

  const banList = bans.data?.bans ?? [];

  return (
    <div className="grid gap-8">
      {/* ------------------------------ players ------------------------------ */}
      <section className="grid gap-3">
        <div className="flex items-center justify-between">
          <h3 className="font-display text-sm font-bold uppercase tracking-wide text-stone-200">Players</h3>
          <Button size="sm" variant="outline" onClick={() => players.refetch()} disabled={players.isFetching}>
            Refresh players
          </Button>
        </div>
        {error && <p className="error">{error}</p>}
        {response && (
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs">
            {response}
          </pre>
        )}
        {players.isLoading ? (
          <p>Querying active server via RCON...</p>
        ) : players.isError ? (
          <p className="error">
            Players are unavailable. The selected definition must be the active RCON-enabled server.
          </p>
        ) : players.data?.players.length ? (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-stone-800 text-left text-xs uppercase tracking-wider text-stone-400">
                <th className="py-2 pr-3">Name</th>
                <th className="py-2 pr-3">Player ID</th>
                <th className="py-2 pr-3">Identity (UID)</th>
                <th className="py-2 pr-3">Status</th>
                <th className="py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {players.data.players.map((player, index) => {
                const rowKey = `${player.id ?? player.name ?? "player"}-${index}`;
                const uid = typeof player.uid === "string" ? player.uid : null;
                const isBanned = uid !== null && bannedUids.has(uid);
                const isAdmin = uid !== null && gameAdmins.includes(uid);
                return (
                  <Fragment key={rowKey}>
                    <tr className="border-b border-stone-900">
                      <td className="py-2 pr-3 font-medium">
                        {player.name ?? `Player ${player.id ?? index + 1}`}
                      </td>
                      <td className="py-2 pr-3 font-mono text-xs">{player.id ?? "-"}</td>
                      <td className="py-2 pr-3 font-mono text-xs break-all">
                        {uid ?? "—"}
                        {uid && (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="ml-2 h-6"
                            onClick={() => copyUid(uid)}
                          >
                            Copy
                          </Button>
                        )}
                      </td>
                      <td className="py-2 pr-3">
                        <span className="inline-flex gap-1">
                          {isBanned && <Badge tone="bad">Banned</Badge>}
                          {isAdmin && <Badge tone="good">Admin</Badge>}
                          {!isBanned && !isAdmin && <span className="text-xs text-stone-500">—</span>}
                        </span>
                      </td>
                      <td className="py-2 text-right">
                        <span className="inline-flex flex-wrap justify-end gap-2">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => moderate("kick", player)}
                            disabled={player.id === undefined || Boolean(busy)}
                          >
                            Kick
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setBanFor(banFor === rowKey ? null : rowKey)}
                            disabled={player.id === undefined}
                          >
                            Ban...
                          </Button>
                          {uid && (
                            <Button size="sm" variant="ghost" onClick={() => copyUid(uid)}>
                              Copy UID
                            </Button>
                          )}
                          {uid &&
                            (isAdmin ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => setAdmin(player, false)}
                                disabled={busy === `admin-${uid}`}
                              >
                                {busy === `admin-${uid}` ? "Working..." : "Remove admin"}
                              </Button>
                            ) : (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => setAdmin(player, true)}
                                disabled={busy === `admin-${uid}`}
                              >
                                {busy === `admin-${uid}` ? "Working..." : "Make admin"}
                              </Button>
                            ))}
                        </span>
                      </td>
                    </tr>
                    {banFor === rowKey && (
                      <tr className="border-b border-stone-900">
                        <td colSpan={5} className="py-2">
                          <BanForm
                            serverId={id}
                            lockedIdentifier={String(player.id)}
                            onDone={() => setBanFor(null)}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="grid gap-2">
            <Empty label="No players reported by RCON." />
            {players.data?.raw?.trim() ? (
              <>
                <p className="text-xs text-stone-500">
                  RCON replied but no player rows were parsed — paste this into .sprints/7/PLAN.md -&gt; RCON
                  #players format -&gt; live capture.
                </p>
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs">
                  {players.data?.raw}
                </pre>
              </>
            ) : null}
          </div>
        )}
        <p className="text-xs text-stone-500">
          The player list refreshes every 5 seconds while this tab is open. "Make admin" applies on the
          next server restart.
        </p>
      </section>

      {/* ------------------------------ bans ------------------------------ */}
      <section className="grid gap-3">
        <h3 className="font-display text-sm font-bold uppercase tracking-wide text-stone-200">Bans</h3>
        <div className="grid gap-1">
          <p className="text-xs text-stone-400">Offline ban — by UID or a single-token player name.</p>
          <BanForm serverId={id} />
        </div>
        {bans.isError ? (
          <p className="error">
            Bans are unavailable: {bans.error instanceof Error ? bans.error.message : "request failed"}
          </p>
        ) : bans.isLoading ? (
          <p>Loading bans...</p>
        ) : banList.length ? (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-stone-800 text-left text-xs uppercase tracking-wider text-stone-400">
                <th className="py-2 pr-3">Ban ID</th>
                <th className="py-2 pr-3">UID</th>
                <th className="py-2 pr-3">Duration</th>
                <th className="py-2 text-right">Remove</th>
              </tr>
            </thead>
            <tbody>
              {banList.map((ban, index) => (
                <tr className="border-b border-stone-900" key={`${ban.ban_id || ban.uid}-${index}`}>
                  <td className="py-2 pr-3 font-mono text-xs break-all">{ban.ban_id || "-"}</td>
                  <td className="py-2 pr-3 font-mono text-xs break-all">
                    {ban.uid}
                    {ban.uid && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ml-2 h-6"
                        onClick={() => copyUid(ban.uid)}
                      >
                        Copy
                      </Button>
                    )}
                  </td>
                  <td className="py-2 pr-3">{ban.duration || "-"}</td>
                  <td className="py-2 text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => removeBan(ban)}
                      disabled={busy === `unban-${ban.ban_id || ban.uid}`}
                    >
                      {busy === `unban-${ban.ban_id || ban.uid}` ? "Removing..." : "Remove"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty label="No bans on this page." />
        )}
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            disabled={page <= 1 || bans.isFetching}
          >
            Prev
          </Button>
          <span className="text-xs text-stone-400">Page {page}</span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPage((current) => current + 1)}
            disabled={banList.length < BANS_PER_PAGE || bans.isFetching}
          >
            Next
          </Button>
        </div>
      </section>

      {/* ------------------------------ server control ------------------------------ */}
      <section className="grid gap-3">
        <h3 className="font-display text-sm font-bold uppercase tracking-wide text-stone-200">
          Server control
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => send("#restart")} disabled={Boolean(busy)}>
            {busy === "#restart" ? "Sending..." : "Restart"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => send("#shutdown")} disabled={Boolean(busy)}>
            {busy === "#shutdown" ? "Sending..." : "Shutdown"}
          </Button>
          <div className="flex flex-1 flex-wrap items-center justify-end gap-2">
            {armed && remaining !== null ? (
              <>
                <Badge tone="warn">Restart in {formatClock(remaining)}</Badge>
                <span className="text-xs text-stone-400">
                  at {schedule.data?.restart_at ? new Date(schedule.data.restart_at).toLocaleTimeString() : "?"}
                  {schedule.data?.warn_at.length ? ` // warnings at ${[...schedule.data.warn_at].sort((a, b) => b - a).join("/")}s` : ""}
                </span>
                <Button size="sm" variant="ghost" onClick={cancelRestart} disabled={Boolean(busy)}>
                  {busy === "schedule-cancel" ? "Cancelling..." : "Cancel restart"}
                </Button>
              </>
            ) : (
              <>
                <Input
                  className="w-28"
                  type="number"
                  min="1"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                  placeholder="Delay"
                  aria-label={`Restart delay in ${unit}`}
                />
                <select
                  aria-label="Restart delay unit"
                  value={unit}
                  onChange={(event) => setUnit(event.target.value as Unit)}
                  className="h-8 border border-stone-600 bg-stone-950 px-2 text-sm"
                >
                  <option value="minutes">minutes</option>
                  <option value="seconds">seconds</option>
                </select>
                <Button size="sm" variant="outline" onClick={armRestart} disabled={Boolean(busy)}>
                  {busy === "schedule-arm" ? "Arming..." : "Schedule restart"}
                </Button>
              </>
            )}
          </div>
        </div>
        <p className="text-xs text-stone-500">
          The scheduled restart is in-memory: it disappears when the server stops or the backend
          restarts.
        </p>
      </section>

      {/* ------------------------------ raw command ------------------------------ */}
      <details className="border border-stone-800 bg-stone-950/40 p-3">
        <summary className="cursor-pointer text-sm font-bold uppercase tracking-wide text-stone-300">
          Raw command
        </summary>
        <div className="grid gap-3 pt-3">
          <div className="flex gap-2">
            <Input
              value={rawCommand}
              onChange={(event) => setRawCommand(event.target.value)}
              placeholder="Raw RCON command (whitelist bypassed)"
            />
            <Button size="sm" onClick={sendRaw} disabled={Boolean(busy)}>
              {busy === "raw" ? "Sending..." : "Send"}
            </Button>
          </div>
          {rawResponse && (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs">
              {rawResponse}
            </pre>
          )}
          <div className="flex gap-2">
            <Input
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="Broadcast (#say — best-effort, not a vanilla RCON command)"
            />
            <Button size="sm" onClick={() => send("#say")} disabled={Boolean(busy)}>
              {busy === "#say" ? "Sending..." : "Say"}
            </Button>
          </div>
        </div>
      </details>
    </div>
  );
}
