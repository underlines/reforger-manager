import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Dialog, Input } from "../ui";
import { api, apiVoid } from "../../lib/api";

const errText = (error: unknown, fallback: string) => (error instanceof Error ? error.message : fallback);

const formatDate = (value: string | null) => (value ? new Date(value).toLocaleString() : "Never");

type McpToken = {
  id: number;
  label: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
};

type McpTokenCreated = McpToken & { token: string };

const copyText = async (text: string) => {
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
};

export function McpTokensCard() {
  const queryClient = useQueryClient();
  const tokens = useQuery({ queryKey: ["mcp-tokens"], queryFn: () => api<McpToken[]>("/api/mcp/tokens") });

  const [createOpen, setCreateOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [created, setCreated] = useState<McpTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api<McpTokenCreated>("/api/mcp/tokens", {
        method: "POST",
        body: JSON.stringify({ label: label.trim() }),
      }),
    onSuccess: (result) => {
      setCreated(result);
      setCopied(false);
      setLabel("");
      setFormError(null);
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["mcp-tokens"] });
    },
    onError: (error) => setFormError(errText(error, "Token creation failed")),
  });

  const revoke = useMutation({
    mutationFn: (id: number) => apiVoid(`/api/mcp/tokens/${id}`, { method: "DELETE" }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["mcp-tokens"] }),
  });

  const closeCreated = () => {
    // The raw token is never refetchable — drop it from state for good.
    setCreated(null);
    setCopied(false);
  };

  const requestRevoke = (token: McpToken) => {
    if (!window.confirm(`Revoke token "${token.label}"? Agents using it will stop working.`)) return;
    revoke.mutate(token.id);
  };

  return (
    <Card className="md:col-span-2">
      <CardHeader>
        <CardTitle>MCP Tokens</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4">
        <p>
          Bearer tokens for CLI agents (Claude Code, Codex) to administer the server over the MCP
          endpoint at <code>/mcp</code>. The raw token is shown exactly once at creation — copy it
          then; it cannot be retrieved again. Revoking a token immediately cuts off the agents
          using it.
        </p>
        {tokens.isLoading && <p role="status">Loading tokens...</p>}
        {tokens.isError && (
          <p className="error" role="alert">
            Could not load tokens: {errText(tokens.error, "Request failed")}
          </p>
        )}
        {tokens.data && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-stone-300">
              <thead>
                <tr className="border-b border-stone-700 text-left text-[10px] uppercase tracking-widest text-stone-500">
                  <th className="py-2 pr-4 font-semibold">Label</th>
                  <th className="py-2 pr-4 font-semibold">Created</th>
                  <th className="py-2 pr-4 font-semibold">Last used</th>
                  <th className="py-2 pr-4 font-semibold">Status</th>
                  <th className="py-2 font-semibold text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-800">
                {tokens.data.length === 0 && (
                  <tr>
                    <td className="py-3 text-stone-500" colSpan={5}>
                      No tokens yet. Create one to connect an agent.
                    </td>
                  </tr>
                )}
                {tokens.data.map((token) => {
                  const revoked = token.revoked_at !== null;
                  return (
                    <tr key={token.id}>
                      <td className="py-2 pr-4 text-stone-200">{token.label}</td>
                      <td className="py-2 pr-4">{formatDate(token.created_at)}</td>
                      <td className="py-2 pr-4">{formatDate(token.last_used_at)}</td>
                      <td className="py-2 pr-4">
                        <Badge tone={revoked ? "bad" : "good"}>{revoked ? "Revoked" : "Active"}</Badge>
                      </td>
                      <td className="py-2 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => requestRevoke(token)}
                          disabled={revoked || (revoke.isPending && revoke.variables === token.id)}
                        >
                          {revoke.isPending && revoke.variables === token.id ? "Revoking..." : "Revoke"}
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <div>
          <Button
            size="sm"
            onClick={() => {
              setFormError(null);
              setCreateOpen(true);
            }}
          >
            Create token
          </Button>
        </div>
      </CardContent>

      <Dialog
        open={createOpen}
        title="Create MCP token"
        onClose={() => !create.isPending && setCreateOpen(false)}
      >
        <div className="grid gap-4">
          <p className="text-xs text-stone-300">
            Give the token a label so you can recognise it later (e.g. the machine or agent that
            will use it).
          </p>
          <label className="grid gap-1 text-xs">
            Label
            <Input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="e.g. dev laptop"
              aria-label="Token label"
              maxLength={120}
            />
          </label>
          {formError && (
            <p className="error" role="alert">
              {formError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={create.isPending}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                create.reset();
                create.mutate();
              }}
              disabled={!label.trim() || create.isPending}
            >
              {create.isPending ? "Creating..." : "Create"}
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog open={created !== null} title="Token created" onClose={closeCreated}>
        <div className="grid gap-4">
          <p className="error" role="note">
            Copy the token now — it is shown only once and cannot be retrieved again.
          </p>
          <code className="block break-all border border-stone-700 bg-stone-950 p-3 font-mono text-xs text-amber-300">
            {created?.token}
          </code>
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-stone-500" role="status">
              {copied ? "Copied to clipboard." : ""}
            </span>
            <div className="flex justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  if (!created) return;
                  copyText(created.token)
                    .then(() => setCopied(true))
                    .catch(() => setCopied(false));
                }}
              >
                {copied ? "Copied" : "Copy"}
              </Button>
              <Button size="sm" onClick={closeCreated}>
                Done
              </Button>
            </div>
          </div>
        </div>
      </Dialog>
    </Card>
  );
}
