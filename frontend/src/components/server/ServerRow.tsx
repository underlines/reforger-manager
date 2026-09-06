import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Badge, Button } from "../ui";
import { api, type Server } from "../../lib/api";

export function ServerRow({ server, runningServer }: { server: Server; runningServer?: Server }) {
  const queryClient = useQueryClient();
  const favourite = useMutation({
    mutationFn: () =>
      api<Server>(`/api/servers/${server.id}`, {
        method: "PATCH",
        // Only the favourite flag: a full form body would needlessly drag
        // mods[] through the mod-set replace path.
        body: JSON.stringify({ is_favourite: !server.is_favourite }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      await queryClient.invalidateQueries({ queryKey: ["server", String(server.id)] });
    },
  });
  const power = useMutation({
    mutationFn: async () => {
      if (server.is_running) {
        await api(`/api/servers/${server.id}/stop`, { method: "POST" });
        return;
      }
      // Single-server rule: starting this one first stops whoever is running.
      if (runningServer && runningServer.id !== server.id) {
        if (!window.confirm(`Stop '${runningServer.name}' and start '${server.name}'?`)) return;
        await api(`/api/servers/${runningServer.id}/stop`, { method: "POST" });
      }
      await api(`/api/servers/${server.id}/start`, { method: "POST" });
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      await queryClient.invalidateQueries({ queryKey: ["server", String(server.id)] });
    },
  });

  return (
    <Link to={`/servers/${server.id}`} className="row">
      <button
        type="button"
        title={server.is_favourite ? "Remove from favourites" : "Mark as favourite"}
        className={
          "text-lg leading-none " +
          (server.is_favourite ? "text-amber-400" : "text-stone-600 hover:text-stone-300")
        }
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          favourite.mutate();
        }}
      >
        {server.is_favourite ? "\u2605" : "\u2606"}
      </button>
      <span className="row-main">
        <strong>{server.name}</strong>
        <small>{server.scenario_game_id ?? "No scenario selected"}</small>
      </span>
      <span className="row-mods">{server.mods.length} mods</span>
      <Badge className="row-status" tone={server.is_running ? "good" : "neutral"}>
        {server.is_running ? "Running" : "Standby"}
      </Badge>
      <Button
        size="sm"
        variant="outline"
        disabled={power.isPending}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          power.mutate();
        }}
      >
        {server.is_running ? "Stop" : "Start"}
      </Button>
      {power.error && <p className="error">{power.error.message}</p>}
    </Link>
  );
}
