import { useQuery } from "@tanstack/react-query";
import { Empty } from "../Empty";
import { api, type Stats } from "../../lib/api";

const CHART_W = 600;
const CHART_H = 160;
const PAD = { left: 38, right: 12, top: 22, bottom: 14 };
const PLOT_W = CHART_W - PAD.left - PAD.right;
const PLOT_H = CHART_H - PAD.top - PAD.bottom;
const SAMPLE_INTERVAL_S = 5;

function num(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function HistoryPanel({ id }: { id: string }) {
  const stats = useQuery({
    queryKey: ["server-stats", id],
    queryFn: () => api<Stats>(`/api/servers/${id}/stats`),
    refetchInterval: 5000,
  });
  const history = stats.data?.history ?? [];
  const sampleCount = history.length;
  const players = history.map((sample) => num(sample.players));
  const pings = history.map((sample) => num(sample.ping_ms));
  const hasPing = pings.some((ping) => ping > 0);
  const slotMax = history.reduce((acc, sample) => Math.max(acc, num(sample.max_players)), 0);
  const yMax = Math.max(...players, slotMax, 1);
  const pingMax = Math.max(...pings, 1);
  const now = players.length ? players[players.length - 1] : 0;
  const minPlayers = players.reduce((acc, value) => Math.min(acc, value), Number.POSITIVE_INFINITY);
  const maxPlayers = players.reduce((acc, value) => Math.max(acc, value), 0);
  const xAt = (index: number): number =>
    sampleCount === 1 ? PAD.left + PLOT_W / 2 : PAD.left + (index / Math.max(sampleCount - 1, 1)) * PLOT_W;
  const yAt = (value: number): number => PAD.top + PLOT_H - (value / yMax) * PLOT_H;
  const pingYAt = (value: number): number => PAD.top + PLOT_H - (value / pingMax) * PLOT_H;
  const playerPoints = players.map((value, index) => `${xAt(index).toFixed(1)},${yAt(value).toFixed(1)}`).join(" ");
  const pingPoints = pings.map((value, index) => `${xAt(index).toFixed(1)},${pingYAt(value).toFixed(1)}`).join(" ");
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
        {sampleCount > 0 ? (
          <svg
            viewBox={`0 0 ${CHART_W} ${CHART_H}`}
            preserveAspectRatio="xMidYMid meet"
            className="h-40 w-full"
            role="img"
            aria-label={`Player history: ${sampleCount} samples, currently ${now} players`}
          >
            <line
              x1={PAD.left}
              x2={CHART_W - PAD.right}
              y1={yAt(yMax)}
              y2={yAt(yMax)}
              stroke="currentColor"
              opacity={0.15}
            />
            <line
              x1={PAD.left}
              x2={CHART_W - PAD.right}
              y1={yAt(yMax / 2)}
              y2={yAt(yMax / 2)}
              stroke="currentColor"
              opacity={0.15}
            />
            <line
              x1={PAD.left}
              x2={CHART_W - PAD.right}
              y1={yAt(0)}
              y2={yAt(0)}
              stroke="currentColor"
              opacity={0.3}
            />
            <text x={PAD.left - 6} y={yAt(yMax) + 4} textAnchor="end" className="fill-current text-xs">
              {yMax}
            </text>
            <text x={PAD.left - 6} y={yAt(0) + 4} textAnchor="end" className="fill-current text-xs">
              0
            </text>
            <text x={PAD.left} y={13} className="fill-current text-xs">
              {sampleCount} samples · ~{sampleCount * SAMPLE_INTERVAL_S}s
            </text>
            <text x={CHART_W - PAD.right} y={13} textAnchor="end" className="fill-current text-xs">
              now {now} · min {minPlayers} · max {maxPlayers}
            </text>
            {hasPing && sampleCount > 1 ? (
              <polyline
                points={pingPoints}
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                className="text-stone-500"
                opacity={0.4}
              />
            ) : null}
            {sampleCount > 1 ? (
              <>
                <polyline
                  points={playerPoints}
                  fill="none"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <circle cx={xAt(sampleCount - 1)} cy={yAt(now)} r={3} fill="#f59e0b" />
              </>
            ) : (
              <>
                <circle cx={xAt(0)} cy={yAt(now)} r={3.5} fill="#f59e0b" />
                {hasPing ? (
                  <circle
                    cx={xAt(0)}
                    cy={pingYAt(pings[0] ?? 0)}
                    r={2.5}
                    className="fill-stone-500"
                    opacity={0.4}
                  />
                ) : null}
              </>
            )}
            {hasPing ? (
              <text
                x={CHART_W - PAD.right}
                y={CHART_H - 2}
                textAnchor="end"
                className="fill-current text-xs text-stone-500"
              >
                ping ms
              </text>
            ) : null}
          </svg>
        ) : (
          <Empty label="No A2S samples have been recorded." />
        )}
      </section>
    </div>
  );
}
