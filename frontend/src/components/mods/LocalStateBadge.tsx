import { useEffect, useRef, useState } from "react";
import { Badge, Button } from "../ui";
import { api, type Job } from "../../lib/api";

type EnqueuedJob = { job_id: number; kind: string };

const TERMINAL_JOB_STATES = new Set(["succeeded", "failed", "cancelled"]);

type LocalStateBadgeProps = {
  guid: string;
  isLocal: boolean;
  isOrphan?: boolean;
  onDone?: () => void;
};

export function LocalStateBadge({ guid, isLocal, isOrphan = false, onDone }: LocalStateBadgeProps) {
  const [jobId, setJobId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    if (jobId === null) return;
    let stopped = false;
    const poll = async () => {
      try {
        const job = await api<Job>(`/api/jobs/${jobId}`);
        if (stopped || !TERMINAL_JOB_STATES.has(job.state)) return;
        stopped = true;
        setJobId(null);
        if (job.state === "failed") setError(job.error || "Download job failed.");
        onDoneRef.current?.();
      } catch {
        return;
      }
    };
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [jobId]);

  const redownload = async () => {
    if (jobId !== null) return;
    setError(null);
    try {
      const enqueued = await api<EnqueuedJob>(`/api/mods/${guid}/download`, { method: "POST" });
      setJobId(enqueued.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download request failed.");
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {isOrphan ? (
        <Badge tone="warn">Orphan</Badge>
      ) : isLocal ? (
        <Badge tone="neutral">downloaded</Badge>
      ) : (
        <>
          <Badge tone="neutral">not cached</Badge>
          <span className="text-[11px] text-stone-500">fetched automatically on next start</span>
        </>
      )}
      <Button size="sm" variant="outline" disabled={jobId !== null} onClick={() => void redownload()}>
        {jobId !== null ? "Downloading..." : "Redownload now"}
      </Button>
      {error && <span className="text-[11px] text-red-400">{error}</span>}
    </span>
  );
}
