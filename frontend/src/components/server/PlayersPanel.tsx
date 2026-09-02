import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Empty } from "../Empty";
import { Badge, Button, Input } from "../ui";
import { api, type Player } from "../../lib/api";

type Schedule = {
  armed: boolean;
  restart_at: string | null;
  warn_at: number[];
  seconds_remaining: number | null;
};

type Unit = "minutes" | "seconds";

const UNIT_FACTOR: Record<Unit, number> = { minutes: 60, seconds: 1 };

export function PlayersPanel({ id }: { id: string }) {
  const [message, setMessage] = useState("");
  const [response, setResponse] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [unit, setUnit] = useState<Unit>("minutes");
  const players = useQuery({
    queryKey: ["server-players", id],
    queryFn: () => api<{ players: Player[] }>(`/api/servers/${id}/players`),
    refetchInterval: 5000,
  });
  const schedule = useQuery({
    queryKey: ["server-restart-schedule", id],
    queryFn: () => api<Schedule>(`/api/servers/${id}/schedule-restart`),
    refetchInterval: 10000,
  });

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

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => players.refetch()} disabled={players.isFetching}>
          Refresh players
        </Button>
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
      <div className="flex gap-2">
        <Input value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Broadcast with #say" />
        <Button size="sm" onClick={() => send("#say")} disabled={Boolean(busy)}>
          {busy === "#say" ? "Sending..." : "Say"}
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
              <th className="py-2 pr-3">Player</th>
              <th className="py-2 pr-3">ID</th>
              <th className="py-2 pr-3">IP</th>
              <th className="py-2 pr-3">Ping</th>
              <th className="py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {players.data.players.map((player, index) => (
              <tr
                className="border-b border-stone-900"
                key={`${player.id ?? player.name ?? "player"}-${index}`}
              >
                <td className="py-2 pr-3 font-medium">{player.name ?? `Player ${player.id ?? index + 1}`}</td>
                <td className="py-2 pr-3 font-mono text-xs">{player.id ?? "-"}</td>
                <td className="py-2 pr-3 font-mono text-xs">{player.ip ?? "not reported"}</td>
                <td className="py-2 pr-3">{player.ping === undefined ? "-" : `${player.ping} ms`}</td>
                <td className="py-2 text-right">
                  <span className="inline-flex gap-2">
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
                      onClick={() => moderate("ban", player)}
                      disabled={player.id === undefined || Boolean(busy)}
                    >
                      Ban
                    </Button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty label="No players reported by RCON." />
      )}
      <p className="text-xs text-stone-500">
        The player list refreshes every 5 seconds while this tab is open. The scheduled restart is
        in-memory: it disappears when the server stops or the backend restarts.
      </p>
    </div>
  );
}
