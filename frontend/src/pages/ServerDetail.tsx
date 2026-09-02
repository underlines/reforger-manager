import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { PageHeading } from "../components/PageHeading";
import { ConsolePanel } from "../components/server/ConsolePanel";
import { ConfigPanel } from "../components/server/ConfigPanel";
import { HistoryPanel } from "../components/server/HistoryPanel";
import { ModsPanel } from "../components/server/ModsPanel";
import { PlayersPanel } from "../components/server/PlayersPanel";
import { Badge, Button, Card, CardContent, Dialog, Input } from "../components/ui";
import { api, apiClient, type DetailServer, type Server } from "../lib/api";

const tabs = ["Config", "Mods", "Console", "Players", "History"] as const;
type Tab = (typeof tabs)[number];

export function ServerDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("Config");
  const [action, setAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [cloneName, setCloneName] = useState("");
  const [cloning, setCloning] = useState(false);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const serverQuery = useQuery({
    queryKey: ["server", id],
    queryFn: () => apiClient.server(id) as Promise<DetailServer>,
    enabled: Boolean(id),
  });
  const server = serverQuery.data;

  const run = async (name: string, request: () => Promise<unknown>) => {
    setAction(name);
    setActionError(null);
    try {
      await request();
      await queryClient.invalidateQueries({ queryKey: ["server", id] });
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Command failed");
    } finally {
      setAction(null);
    }
  };

  if (serverQuery.isLoading) return <p>Loading server definition...</p>;
  if (serverQuery.isError || !server) return <p className="error">Server definition could not be loaded.</p>;

  const onClone = async () => {
    const name = cloneName.trim();
    if (!name) return;
    setCloning(true);
    setCloneError(null);
    try {
      const created = await api<Server>(`/api/servers/${id}/clone`, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      await queryClient.invalidateQueries({ queryKey: ["servers"] });
      navigate(`/servers/${created.id}`);
    } catch (error) {
      setCloneError(error instanceof Error ? error.message : "Clone failed");
      setCloning(false);
    }
  };

  return (
    <>
      <PageHeading
        title={server.name}
        detail={`Definition #${server.id} // ${server.is_running ? "running" : "standing by"}`}
        actions={
          <>
            <Badge tone={server.is_running ? "good" : "neutral"}>
              {server.is_running ? "Running" : "Standby"}
            </Badge>
            <Button
              size="sm"
              variant="ghost"
              title={server.is_favourite ? "Remove from favourites" : "Mark as favourite"}
              disabled={Boolean(action)}
              onClick={() =>
                run("favourite", () =>
                  api(`/api/servers/${id}`, {
                    method: "PATCH",
                    // Only the flag -- never the whole form body, which would
                    // drag mods[] through the mod-set replace path.
                    body: JSON.stringify({ is_favourite: !server.is_favourite }),
                  }),
                )
              }
            >
              <span className={server.is_favourite ? "text-amber-400" : "text-stone-500"}>
                {server.is_favourite ? "\u2605" : "\u2606"}
              </span>
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={Boolean(action)}
              onClick={() => {
                setCloneName(`${server.name} (copy)`);
                setCloneError(null);
                setCloneOpen(true);
              }}
            >
              Duplicate this definition
            </Button>
            <Button
              size="sm"
              disabled={Boolean(action)}
              onClick={() =>
                run(server.is_running ? "stop" : "start", () =>
                  api(`/api/servers/${id}/${server.is_running ? "stop" : "start"}`, { method: "POST" }),
                )
              }
            >
              {action === (server.is_running ? "stop" : "start")
                ? "Working..."
                : server.is_running
                  ? "Stop"
                  : "Start"}
            </Button>
          </>
        }
      />
      {actionError && <p className="error">{actionError}</p>}
      <div className="tabs" role="tablist">
        {tabs.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "active" : ""}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <Card>
        <CardContent>
          {tab === "Config" && <ConfigPanel id={id} server={server} />}
          {tab === "Mods" && <ModsPanel id={id} server={server} action={action} run={run} />}
          {tab === "Console" && <ConsolePanel id={id} />}
          {tab === "Players" && <PlayersPanel id={id} />}
          {tab === "History" && <HistoryPanel id={id} />}
        </CardContent>
      </Card>

      <Dialog
        open={cloneOpen}
        title="Duplicate this definition"
        onClose={() => !cloning && setCloneOpen(false)}
      >
        <div className="space-y-4">
          <p className="text-xs text-stone-300">
            Deep-copies every setting and the mod set (including pins) into a new, independent
            definition. Runtime state is not copied. Ports are copied too, so the copy collides
            with this one until edited.
          </p>
          <label className="block space-y-1 text-xs text-stone-300">
            <span>New definition name</span>
            <Input
              value={cloneName}
              onChange={(event) => setCloneName(event.target.value)}
              autoFocus
            />
          </label>
          {cloneError && <p className="error">{cloneError}</p>}
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setCloneOpen(false)}
              disabled={cloning}
            >
              Cancel
            </Button>
            <Button type="button" onClick={onClone} disabled={cloning || !cloneName.trim()}>
              {cloning ? "Duplicating..." : "Duplicate"}
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}