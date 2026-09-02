import { useQuery } from "@tanstack/react-query";
import { Empty } from "../Empty";
import { Badge } from "../ui";
import { api, type Stats } from "../../lib/api";

export function HistoryPanel({ id }: { id: string }) {
  const stats = useQuery({
    queryKey: ["server-stats", id],
    queryFn: () => api<Stats>(`/api/servers/${id}/stats`),
    refetchInterval: 5000,
  });
  return (
    <div className="grid gap-4">
      <section>
        <p className="metric-label">Current A2S sample</p>
        {stats.isLoading ? (
          <p>Querying server statistics...</p>
        ) : stats.isError ? (
          <p className="error">Live A2S statistics are unavailable.</p>
        ) : (
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap border border-stone-800 bg-stone-950 p-3 text-xs">
            {JSON.stringify(stats.data?.current, null, 2)}
          </pre>
        )}
      </section>
      <section>
        <p className="metric-label">Rolling history</p>
        {stats.data?.history.length ? (
          <div className="list">
            {stats.data.history
              .slice()
              .reverse()
              .map((sample, index) => (
                <div className="row" key={index}>
                  <span className="row-main">
                    <strong>{String(sample.name ?? sample.map_name ?? "A2S sample")}</strong>
                    <small>
                      {String(sample.players ?? "?")} / {String(sample.max_players ?? "?")} players
                    </small>
                  </span>
                  <Badge tone="neutral">
                    {sample.ping_ms === undefined ? "Ping n/a" : `${String(sample.ping_ms)} ms`}
                  </Badge>
                </div>
              ))}
          </div>
        ) : (
          <Empty label="No A2S samples have been recorded." />
        )}
      </section>
    </div>
  );
}