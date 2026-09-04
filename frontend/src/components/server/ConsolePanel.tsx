import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Badge, Button, Input } from "../ui";
import { api, type LogLine, type LogResponse, websocket } from "../../lib/api";

const tailLimit = 500;

export function ConsolePanel({ id }: { id: string }) {
  const [severity, setSeverity] = useState("");
  const [hideSpam, setHideSpam] = useState(true);
  const [autoScroll, setAutoScroll] = useState(true);
  const [search, setSearch] = useState("");
  const [tail, setTail] = useState<LogLine[]>([]);
  const [socketState, setSocketState] = useState("connecting");
  const preRef = useRef<HTMLPreElement>(null);
  const params = new URLSearchParams();
  if (severity) params.set("severity", severity);
  params.set("hide_spam", String(hideSpam));
  if (search.trim()) params.set("q", search.trim());
  // The server's unsearched log endpoint is intentionally unbounded; only request its bounded search mode.
  const fallback = useQuery({
    queryKey: ["server-log", id, severity, hideSpam, search],
    queryFn: () => api<LogResponse>(`/api/servers/${id}/log?${params}`),
    enabled: Boolean(search.trim()),
  });
  useEffect(() => {
    setTail([]);
    let socket: WebSocket | undefined;
    try {
      socket = websocket(`/api/servers/${id}/console`);
      socket.onopen = () => setSocketState("live");
      socket.onerror = () => setSocketState("unavailable");
      socket.onclose = () => setSocketState("closed");
      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as {
            line?: string;
            event?: string;
            state?: string;
            is_spam?: boolean;
          };
          const text = payload.line ?? (payload.event ? `[${payload.event}]${payload.state ? ` ${payload.state}` : ""}` : "");
          if (text) {
            setTail((lines) =>
              [...lines, { text, severity: severityOf(text), is_spam: payload.is_spam === true }].slice(-tailLimit),
            );
          }
        } catch {
          /* Ignore malformed websocket frames. */
        }
      };
    } catch (error) {
      setSocketState(error instanceof Error ? error.message : "unavailable");
    }
    return () => socket?.close();
  }, [id]);
  const displayed = [...(fallback.data?.lines ?? []), ...tail]
    .filter((line) => (!severity || line.severity === severity) && (!hideSpam || !line.is_spam))
    .slice(-tailLimit);
  useEffect(() => {
    if (autoScroll) preRef.current?.scrollTo({ top: preRef.current.scrollHeight });
  }, [autoScroll, displayed.length]);
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Log severity"
          value={severity}
          onChange={(event) => setSeverity(event.target.value)}
          className="h-9 border border-stone-600 bg-stone-950 px-2 text-sm"
        >
          <option value="">All severities</option>
          <option value="debug">Debug</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
        </select>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={hideSpam} onChange={(event) => setHideSpam(event.target.checked)} />
          Hide known spam
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} />
          Autoscroll
        </label>
        <Button size="sm" variant="ghost" onClick={() => fallback.refetch()} disabled={!search.trim()}>
          Search log
        </Button>
        <Badge tone={socketState === "live" ? "good" : "neutral"}>Stream {socketState}</Badge>
      </div>
      <Input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="Search stored console log"
      />
      <p className="text-xs text-stone-400">
        Live tail is bounded to {tailLimit} lines. Stored log search is the fallback.
      </p>
      {fallback.isError && <p className="error">Stored log could not be loaded.</p>}
      <pre ref={preRef} className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words border border-stone-800 bg-stone-950 p-3 text-xs">
        {displayed.length
          ? displayed.map((line) => `${line.severity ? `[${line.severity.toUpperCase()}] ` : ""}${line.text}`).join("\n")
          : "No matching log lines received."}
      </pre>
    </div>
  );
}

function severityOf(text: string) {
  const code = text.match(/\(([DIWE])\)\s*:/i)?.[1]?.toUpperCase();
  return code === "D" ? "debug" : code === "I" ? "info" : code === "W" ? "warning" : code === "E" ? "error" : null;
}