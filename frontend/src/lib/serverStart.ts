import { api, type Job, type ServerModsReadyOut } from "./api";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Start a server the safe way: ask the backend to make every assigned mod
 * locally ready first, wait out any download job it enqueues, then start.
 *
 * `ensure-ready` returns a null job id when nothing needs downloading, in which
 * case the start follows immediately.
 */
export async function startServerReady(id: number): Promise<void> {
  const ready = await api<ServerModsReadyOut>(`/api/servers/${id}/mods/ensure-ready`, {
    method: "POST",
  });

  if (ready.job_id !== null) {
    for (;;) {
      const job = await api<Job>(`/api/jobs/${ready.job_id}`);
      if (job.state === "failed" || job.state === "cancelled") {
        throw new Error(job.error ?? `Mod download ${job.state}`);
      }
      if (TERMINAL.has(job.state)) break;
      await sleep(2000);
    }
  }

  await api(`/api/servers/${id}/start`, { method: "POST" });
}
