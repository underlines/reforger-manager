import { Badge } from "../ui";

const relativeTime = (value: string | null | undefined) => {
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

type FreshnessPillProps = {
  timestamp: string | null;
  syncing?: boolean;
};

export function FreshnessPill({ timestamp, syncing }: FreshnessPillProps) {
  if (syncing) return <Badge tone="warn">syncing…</Badge>;
  if (timestamp === null) return <Badge tone="neutral">never synced</Badge>;
  return <Badge tone="neutral">synced {relativeTime(timestamp)}</Badge>;
}