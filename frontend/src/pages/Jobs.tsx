import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Empty } from "../components/Empty";
import { PageHeading } from "../components/PageHeading";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../components/ui";
import { api, apiVoid, type Job, websocket } from "../lib/api";

type JobRecord = Job & {
  params?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  log_tail?: unknown[] | null;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string | null;
};

type JobEvent = Partial<JobRecord> & { id: number; type?: "job" | "log"; last_line?: string };

const MAX_LIVE_JOBS = 100;
const states = ["queued", "running", "succeeded", "failed", "cancelled"];
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const isTerminal = (state: string) => TERMINAL.has(state);

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : "Request failed");
const label = (value: string) => value.replaceAll("_", " ");
const progress = (value: number) => `${Math.round(Math.max(0, Math.min(100, value)))}%`;
const toneFor = (state: string) =>
  state === "succeeded" ? "good" : state === "failed" || state === "cancelled" ? "bad" : "warn";
const formatDate = (value: string | null | undefined) => (value ? new Date(value).toLocaleString() : "Not recorded");
const fullDate = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : undefined;
const relTime = (value: string | null | undefined) => {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 10) return "just now";
  if (secs < 90) return `${Math.max(1, Math.round(secs / 60))}m ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(value).toLocaleDateString();
};

export function JobsPage() {
  const [state, setState] = useState("all");
  const [kind, setKind] = useState("");
  const [selected, setSelected] = useState<JobRecord | null>(null);
  const [liveJobs, setLiveJobs] = useState<Map<number, Partial<JobRecord>>>(new Map());
  const [streamConnected, setStreamConnected] = useState(false);
  const [busy, setBusy] = useState<number | "prune" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const filters = new URLSearchParams();
  if (state !== "all") filters.set("state", state);
  if (kind.trim()) filters.set("kind", kind.trim());
  const filterString = filters.toString();
  const jobsQuery = useQuery({
    queryKey: ["jobs", filterString],
    queryFn: () => api<JobRecord[]>(`/api/jobs${filterString ? `?${filterString}` : ""}`),
    refetchInterval: streamConnected ? 15000 : 5000,
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    try {
      socket = websocket("/api/jobs/stream");
      socket.onopen = () => setStreamConnected(true);
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as JobEvent;
          if (!Number.isInteger(event.id)) return;
          setLiveJobs((current) => {
            const next = new Map(current);
            next.set(event.id, { ...next.get(event.id), ...event });
            while (next.size > MAX_LIVE_JOBS) next.delete(next.keys().next().value as number);
            return next;
          });
        } catch {
          // Ignore malformed stream messages; the REST query remains authoritative.
        }
      };
      socket.onerror = () => setStreamConnected(false);
      socket.onclose = () => setStreamConnected(false);
    } catch {
      setStreamConnected(false);
    }
    return () => {
      socket?.close();
      setStreamConnected(false);
    };
  }, []);

  const forget = (id: number) =>
    setLiveJobs((current) => {
      if (!current.has(id)) return current;
      const next = new Map(current);
      next.delete(id);
      return next;
    });

  const cancelJob = async (id: number) => {
    if (!window.confirm(`Cancel job #${id}? A running job is asked to stop and its work is abandoned.`)) return;
    setBusy(id);
    setActionError(null);
    try {
      await api(`/api/jobs/${id}/cancel`, { method: "POST" });
      await jobsQuery.refetch();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(null);
    }
  };

  const removeJob = async (id: number) => {
    setBusy(id);
    setActionError(null);
    try {
      await apiVoid(`/api/jobs/${id}`, { method: "DELETE" });
      forget(id);
      if (selected?.id === id) setSelected(null);
      await jobsQuery.refetch();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(null);
    }
  };

  const pruneFinished = async () => {
    if (!window.confirm("Remove every succeeded, failed and cancelled job from the queue?")) return;
    setBusy("prune");
    setActionError(null);
    try {
      const { deleted } = await api<{ deleted: number }>("/api/jobs/prune", { method: "POST" });
      setLiveJobs((current) => {
        const next = new Map(current);
        for (const [id, job] of current) if (job.state && isTerminal(job.state)) next.delete(id);
        return next;
      });
      await jobsQuery.refetch();
      if (deleted === 0) setActionError("No finished jobs to remove.");
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(null);
    }
  };

  const jobs = new Map<number, JobRecord>();
  for (const job of jobsQuery.data ?? []) jobs.set(job.id, job);
  for (const [id, update] of liveJobs) {
    const base = jobs.get(id);
    if (base) jobs.set(id, { ...base, ...update });
    else if (update.kind && update.state && typeof update.progress === "number") jobs.set(id, update as JobRecord);
  }
  const visibleJobs = [...jobs.values()]
    .filter((job) => (state === "all" || job.state === state) && (!kind.trim() || job.kind === kind.trim()))
    .sort((a, b) => b.id - a.id);

  return (
    <>
      <PageHeading
        title="Job Queue"
        detail="Recent background work with live progress when the job stream is available."
      />
      <Card className="mb-4">
        <CardContent className="grid gap-3 pt-4 md:grid-cols-[12rem_minmax(0,1fr)_auto]">
          <select
            className="h-10 border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100"
            value={state}
            onChange={(event) => setState(event.target.value)}
            aria-label="Job state filter"
          >
            <option value="all">All states</option>
            {states.map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
          </select>
          <Input
            value={kind}
            onChange={(event) => setKind(event.target.value)}
            placeholder="Exact job kind, e.g. mod_sync"
            aria-label="Job kind filter"
          />
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-2 text-xs text-stone-400">
              <span
                className={`h-2 w-2 rounded-full ${streamConnected ? "bg-emerald-400" : "bg-stone-600"}`}
                aria-hidden="true"
              />
              {streamConnected ? "Live updates" : "Polling every 5s"}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void pruneFinished()}
              disabled={busy !== null || !visibleJobs.some((job) => isTerminal(job.state))}
            >
              {busy === "prune" ? "Clearing..." : "Clear finished"}
            </Button>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Recent Operations {visibleJobs.length ? `(${visibleJobs.length})` : ""}</CardTitle>
        </CardHeader>
        <CardContent>
          {actionError && <p className="error mb-3">{actionError}</p>}
          {jobsQuery.isLoading && <p className="text-sm text-stone-400">Loading jobs...</p>}
          {jobsQuery.isError && (
            <div className="space-y-3">
              <p className="error">Could not load jobs: {errorMessage(jobsQuery.error)}</p>
              <Button size="sm" variant="outline" onClick={() => void jobsQuery.refetch()}>
                Retry
              </Button>
            </div>
          )}
          {!jobsQuery.isLoading && !jobsQuery.isError && !visibleJobs.length && (
            <Empty label="No jobs match these filters." />
          )}
          {visibleJobs.length ? (
            <div className="divide-y divide-stone-800">
              {visibleJobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  busy={busy === job.id}
                  disabled={busy !== null}
                  onOpen={() => setSelected(job)}
                  onCancel={() => void cancelJob(job.id)}
                  onRemove={() => void removeJob(job.id)}
                />
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Dialog
        open={selected !== null}
        title={selected ? `${label(selected.kind)} #${selected.id}` : "Job details"}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <JobDetail
            job={liveJobs.has(selected.id) ? { ...selected, ...liveJobs.get(selected.id) } : selected}
            busy={busy === selected.id}
            disabled={busy !== null}
            onCancel={() => void cancelJob(selected.id)}
            onRemove={() => void removeJob(selected.id)}
          />
        )}
      </Dialog>
    </>
  );
}

type JobActionProps = {
  busy: boolean;
  disabled: boolean;
  onCancel: () => void;
  onRemove: () => void;
};

function JobRow({
  job,
  busy,
  disabled,
  onOpen,
  onCancel,
  onRemove,
}: { job: JobRecord; onOpen: () => void } & JobActionProps) {
  return (
    <article className="grid gap-3 py-4 md:grid-cols-[minmax(0,1fr)_10rem_auto]">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <strong className="font-display text-lg uppercase tracking-wide text-stone-100">{label(job.kind)}</strong>
          <span className="font-mono text-xs text-stone-500">#{job.id}</span>
        </div>
        <p className="truncate text-xs text-stone-400">{job.current_step ?? "Awaiting status"}</p>
        <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-stone-500">
          <span title={fullDate(job.started_at ?? job.created_at)}>
            {job.started_at ? `Started ${relTime(job.started_at)}` : `Queued ${relTime(job.created_at)}`}
          </span>
          <span title={fullDate(job.finished_at ?? job.updated_at)}>
            {job.finished_at
              ? `Finished ${relTime(job.finished_at)}`
              : `Updated ${relTime(job.updated_at)}`}
          </span>
        </p>
        <div className="h-1.5 overflow-hidden bg-stone-800">
          <div className="h-full bg-amber-400 transition-[width]" style={{ width: progress(job.progress) }} />
        </div>
      </div>
      <div className="self-center text-sm text-stone-300">{progress(job.progress)}</div>
      <div className="flex flex-wrap items-center gap-2 md:justify-end">
        <Badge tone={toneFor(job.state)}>{job.state}</Badge>
        <Button size="sm" variant="ghost" onClick={onOpen}>
          Details
        </Button>
        {isTerminal(job.state) ? (
          <Button size="sm" variant="ghost" onClick={onRemove} disabled={disabled}>
            {busy ? "Removing..." : "Remove"}
          </Button>
        ) : (
          <Button size="sm" variant="outline" onClick={onCancel} disabled={disabled}>
            {busy ? "Cancelling..." : "Cancel"}
          </Button>
        )}
      </div>
    </article>
  );
}

function JobDetail({
  job,
  busy,
  disabled,
  onCancel,
  onRemove,
}: { job: JobRecord } & JobActionProps) {
  const logs = job.log_tail?.map(String) ?? [];
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={toneFor(job.state)}>{job.state}</Badge>
        <span className="font-mono text-stone-400">#{job.id}</span>
        <span>{progress(job.progress)}</span>
        <span className="ml-auto flex gap-2">
          {isTerminal(job.state) ? (
            <Button size="sm" variant="ghost" onClick={onRemove} disabled={disabled}>
              {busy ? "Removing..." : "Remove"}
            </Button>
          ) : (
            <Button size="sm" variant="outline" onClick={onCancel} disabled={disabled}>
              {busy ? "Cancelling..." : "Cancel job"}
            </Button>
          )}
        </span>
      </div>
      <div className="h-2 overflow-hidden bg-stone-800">
        <div className="h-full bg-amber-400" style={{ width: progress(job.progress) }} />
      </div>
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-xs">
        <dt className="text-stone-500">Current step</dt>
        <dd className="break-words text-stone-200">{job.current_step ?? "Awaiting status"}</dd>
        <dt className="text-stone-500">Created</dt>
        <dd>{formatDate(job.created_at)}</dd>
        <dt className="text-stone-500">Started</dt>
        <dd>{formatDate(job.started_at)}</dd>
        <dt className="text-stone-500">Last update</dt>
        <dd>{formatDate(job.updated_at)}</dd>
        <dt className="text-stone-500">Finished</dt>
        <dd>{formatDate(job.finished_at)}</dd>
      </dl>
      {job.error && (
        <section>
          <h3 className="mb-1 text-xs font-bold uppercase tracking-wider text-red-300">Error</h3>
          <pre className="max-h-36 overflow-auto whitespace-pre-wrap border border-red-900 bg-red-950/30 p-3 text-xs text-red-200">
            {job.error}
          </pre>
        </section>
      )}
      {job.result && (
        <section>
          <h3 className="mb-1 text-xs font-bold uppercase tracking-wider text-stone-400">Result</h3>
          <pre className="max-h-36 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs text-stone-300">
            {JSON.stringify(job.result, null, 2)}
          </pre>
        </section>
      )}
      {logs.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-bold uppercase tracking-wider text-stone-400">Log tail</h3>
          <pre className="max-h-52 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 font-mono text-xs text-stone-300">
            {logs.join("\n")}
          </pre>
        </section>
      )}
    </div>
  );
}