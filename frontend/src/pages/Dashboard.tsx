import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "../components/ui";
import { api, apiClient, type Server } from "../lib/api";

type Stats = {
  current: { name: string; map_name: string; players: number; max_players: number; ping_ms: number };
  history: unknown[];
};

export function DashboardPage() {
  const queryClient = useQueryClient();
  const engine = useQuery({ queryKey: ["engine"], queryFn: apiClient.engine });
  const servers = useQuery({ queryKey: ["servers"], queryFn: apiClient.servers });
  const running = servers.data?.find((server) => server.is_running);
  const stats = useQuery({
    queryKey: ["server-stats", running?.id],
    queryFn: () => api<Stats>(`/api/servers/${running!.id}/stats`),
    enabled: Boolean(running),
    refetchInterval: 10_000,
  });
  const check = useMutation({
    mutationFn: () => api("/api/engine/check", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["engine"] }),
  });
  const update = useMutation({
    mutationFn: () => api("/api/engine/update", { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["engine"] });
      queryClient.invalidateQueries({ queryKey: ["servers"] });
    },
  });
  const actionError = check.error ?? update.error;
  const engineReady = Boolean(engine.data?.update_available);

  return (
    <>
      <PageHeading
        title="Situation Board"
        detail="Runtime posture, engine readiness, and active deployment telemetry."
        actions={
          <>
            <Button
              variant="outline"
              onClick={() => {
                update.reset();
                check.mutate();
              }}
              disabled={check.isPending || update.isPending}
            >
              {check.isPending ? "Checking engine..." : "Check engine"}
            </Button>
            <Button
              onClick={() => {
                check.reset();
                update.mutate();
              }}
              disabled={!engineReady || check.isPending || update.isPending}
              title={engineReady ? "Queue engine update" : "No engine update is available"}
            >
              {update.isPending ? "Queueing update..." : "Update engine"}
            </Button>
          </>
        }
      />
      <div className="metric-grid">
        <Metric
          label="Installed build"
          value={engine.data?.installed_build ?? "Unreported"}
          status={
            engine.isLoading ? "CHECKING" : engine.data?.update_available ? "UPDATE AVAILABLE" : engine.data ? "CURRENT" : "UNKNOWN"
          }
          tone={engine.data?.update_available ? "warn" : engine.data ? "good" : "neutral"}
        />
        <Metric
          label="Available build"
          value={engine.data?.latest_build ?? "Unreported"}
          status={engine.data?.update_available ? "READY TO APPLY" : "NO UPDATE"}
          tone={engine.data?.update_available ? "warn" : "neutral"}
        />
        <Metric
          label="Installed display version"
          value={engine.data?.installed_version ?? "Unreported"}
          status="SERVER VERSION"
          tone="neutral"
        />
        <Metric
          label="Running server"
          value={running?.name ?? "None"}
          status={running ? "RUNNING" : "STANDBY"}
          tone={running ? "good" : "neutral"}
        />
        <Metric
          label="Saved definitions"
          value={servers.data ? String(servers.data.length) : "Unreported"}
          status="SERVER TEMPLATES"
          tone="neutral"
        />
        <Metric
          label="A2S players"
          value={stats.data ? `${stats.data.current.players} / ${stats.data.current.max_players}` : "Unavailable"}
          status={stats.data ? "LIVE QUERY" : stats.isFetching ? "QUERYING" : running ? "AWAITING A2S" : "NO ACTIVE SERVER"}
          tone={stats.data ? "good" : "neutral"}
        />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Engine Control</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={engine.data?.update_available ? "warn" : "neutral"}>
                {engine.data?.update_available ? "Update available" : "No update available"}
              </Badge>
              {engine.data?.last_checked && (
                <span className="text-xs text-stone-500 dark:text-stone-400">
                  Last checked {new Date(engine.data.last_checked).toLocaleString()}
                </span>
              )}
            </div>
            <p className="m-0 text-xs leading-6 text-stone-600 dark:text-stone-300">
              {engine.data?.update_available
                ? "An engine update can be queued. The backend will refuse it if a server is running."
                : "Update is disabled until the engine check reports a newer available build."}
            </p>
            {actionError && (
              <p className="error m-0" role="alert">
                {actionError.message}
              </p>
            )}
            {update.isSuccess && (
              <p className="m-0 text-xs text-emerald-700 dark:text-emerald-300" role="status">
                Engine update queued. Follow progress in Jobs.
              </p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Active Deployment</CardTitle>
          </CardHeader>
          <CardContent>
            {running ? (
              <Telemetry server={running} stats={stats.data} loading={stats.isFetching} error={stats.isError} />
            ) : (
              <p className="m-0 text-sm text-stone-600 dark:text-stone-300">
                No server is running. A2S telemetry becomes available when a deployment is active.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
      {(engine.isError || servers.isError) && (
        <p className="error" role="alert">
          {engine.isError ? "Engine status could not be loaded." : "Server definitions could not be loaded."}
        </p>
      )}
    </>
  );
}

function Telemetry({
  server,
  stats,
  loading,
  error,
}: {
  server: Server;
  stats?: Stats;
  loading: boolean;
  error: boolean;
}) {
  if (error) return <p className="error m-0" role="alert">A2S telemetry is unavailable for this server.</p>;
  if (!stats)
    return (
      <p className="m-0 text-sm text-stone-600 dark:text-stone-300">
        {loading ? `Querying A2S telemetry for ${server.name}...` : "A2S telemetry has not reported yet."}
      </p>
    );
  return (
    <dl className="definition">
      <dt>Server</dt>
      <dd>{stats.current.name}</dd>
      <dt>Map</dt>
      <dd>{stats.current.map_name}</dd>
      <dt>Players</dt>
      <dd>
        {stats.current.players} / {stats.current.max_players}
      </dd>
      <dt>Query latency</dt>
      <dd>{Math.round(stats.current.ping_ms)} ms</dd>
    </dl>
  );
}

function Metric({
  label,
  value,
  status,
  tone,
}: {
  label: string;
  value: string;
  status: string;
  tone: "neutral" | "good" | "warn";
}) {
  return (
    <Card>
      <CardContent>
        <p className="metric-label">{label}</p>
        <strong className="metric-value">{value}</strong>
        <Badge tone={tone}>{status}</Badge>
      </CardContent>
    </Card>
  );
}