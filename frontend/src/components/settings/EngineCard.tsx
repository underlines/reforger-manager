import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle } from "../ui";
import { api, apiClient, type Engine } from "../../lib/api";

type EnqueuedJob = { job_id: number; kind: string };

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");
const formatDate = (value: string | null) => (value ? new Date(value).toLocaleString() : "Not recorded");

export function EngineCard() {
  const queryClient = useQueryClient();
  const engine = useQuery({ queryKey: ["engine"], queryFn: apiClient.engine });
  const check = useMutation({
    mutationFn: () => api<Engine>("/api/engine/check", { method: "POST" }),
    onSuccess: (result) => queryClient.setQueryData(["engine"], result),
  });
  const update = useMutation({
    mutationFn: () => api<EnqueuedJob>("/api/engine/update", { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["engine"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  const engineBusy = check.isPending || update.isPending;
  const actionError = check.error ?? update.error;
  const updateAvailable = engine.data?.update_available === true;
  const notInstalled = engine.data != null && engine.data.installed_build == null;
  const canQueue = updateAvailable || notInstalled;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Engine Maintenance</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        {engine.isLoading && <p role="status">Loading engine status...</p>}
        {engine.isError && (
          <div className="grid gap-3">
            <p className="error" role="alert">
              Could not load engine status: {errorMessage(engine.error)}
            </p>
            <div>
              <Button size="sm" variant="outline" onClick={() => void engine.refetch()}>
                Retry
              </Button>
            </div>
          </div>
        )}
        {engine.data && (
          <>
            <dl className="definition">
              <dt>Installed</dt>
              <dd>
                {engine.data.installed_build ?? "Unreported"}
                {engine.data.installed_version ? ` (${engine.data.installed_version})` : ""}
              </dd>
              <dt>Available</dt>
              <dd>
                {engine.data.latest_build ?? "Unreported"}
                {engine.data.latest_version ? ` (${engine.data.latest_version})` : ""}
              </dd>
              <dt>Last check</dt>
              <dd>{formatDate(engine.data.last_checked)}</dd>
              <dt>Last update</dt>
              <dd>{formatDate(engine.data.last_updated_at)}</dd>
              <dt>Readiness</dt>
              <dd>
                <Badge tone={notInstalled ? "bad" : updateAvailable ? "warn" : "good"}>
                  {notInstalled ? "Not installed" : updateAvailable ? "Update available" : "Current"}
                </Badge>
              </dd>
            </dl>
            <p>
              {notInstalled
                ? "No engine is installed. Queue an install to download the server files via steamcmd; the backend will refuse it while a server is running."
                : updateAvailable
                  ? "Queueing an update validates the server files. The backend will refuse this operation while a server is running."
                  : "Run a check to compare the installed engine with the latest available build."}
            </p>
          </>
        )}
        {actionError && (
          <p className="error" role="alert">
            {errorMessage(actionError)}
          </p>
        )}
        {update.isSuccess && (
          <p className="text-xs text-emerald-700 dark:text-emerald-300" role="status">
            Engine {notInstalled ? "install" : "update"} queued as job #{update.data.job_id}. Track its progress in Jobs.
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            onClick={() => {
              update.reset();
              check.mutate();
            }}
            disabled={engineBusy}
          >
            {check.isPending ? "Checking..." : "Check engine"}
          </Button>
          <Button
            onClick={() => {
              check.reset();
              update.mutate();
            }}
            disabled={engineBusy || !canQueue}
            title={
              notInstalled
                ? "Download and install the engine via steamcmd"
                : canQueue
                  ? "Queue an engine update"
                  : "An engine check must report an available update first"
            }
          >
            {update.isPending ? "Queueing..." : notInstalled ? "Install engine" : "Queue update"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}