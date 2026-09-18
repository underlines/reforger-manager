import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Job } from "./api";

export type ModGraphNode = {
  guid: string;
  name: string | null;
  is_local: boolean;
  api_state: "ok" | "not_found" | "unchecked";
  is_unlisted: boolean;
  is_private: boolean;
  is_obsolete: boolean;
  api_checked_at: string | null;
  is_builtin: boolean;
};
export type ModGraphEdge = { from: string; to: string; source: string | null };
export type ModGraph = { nodes: ModGraphNode[]; edges: ModGraphEdge[] };

export type TreeNode = {
  guid: string;
  node: ModGraphNode | null; // null => dangling/unresolved guid, no mods row
  depth: number;
  children: TreeNode[];
};

const MAX_DEPTH = 12;

export function useModGraph() {
  return useQuery({
    queryKey: ["mods-graph"],
    queryFn: () => api<ModGraph>("/api/mods/graph"),
    staleTime: 5 * 60_000,
  });
}

/** Poll GET /api/jobs/{jobId} until terminal, then invalidate ["mods-graph"] once. */
export function useInvalidateGraphOnJob(jobId: number | null) {
  const queryClient = useQueryClient();
  const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
  useQuery({
    queryKey: ["mods-graph-watch", jobId],
    queryFn: async () => {
      const job = await api<Job>(`/api/jobs/${jobId}`);
      if (TERMINAL.has(job.state)) {
        void queryClient.invalidateQueries({ queryKey: ["mods-graph"] });
      }
      return job;
    },
    enabled: jobId !== null,
    refetchInterval: (query) => (query.state.data && TERMINAL.has(query.state.data.state) ? false : 2000),
  });
}

function nodeIndex(graph: ModGraph): Map<string, ModGraphNode> {
  return new Map(graph.nodes.map((node) => [node.guid, node]));
}

function forwardAdjacency(graph: ModGraph): Map<string, ModGraphEdge[]> {
  const map = new Map<string, ModGraphEdge[]>();
  for (const edge of graph.edges) {
    const list = map.get(edge.from);
    if (list) list.push(edge);
    else map.set(edge.from, [edge]);
  }
  return map;
}

function reverseAdjacency(graph: ModGraph): Map<string, ModGraphEdge[]> {
  const map = new Map<string, ModGraphEdge[]>();
  for (const edge of graph.edges) {
    const list = map.get(edge.to);
    if (list) list.push(edge);
    else map.set(edge.to, [edge]);
  }
  return map;
}

/* A guid may legitimately appear more than once in one render (once per parent
 * that reaches it), so only the active root-to-leaf path is deduped — enough to
 * stop a metadata cycle from looping forever. */
export function buildNested(roots: string[], graph: ModGraph): TreeNode[] {
  const byGuid = nodeIndex(graph);
  const adjacency = forwardAdjacency(graph);

  const walk = (guid: string, depth: number, ancestors: Set<string>): TreeNode => {
    const node: TreeNode = { guid, node: byGuid.get(guid) ?? null, depth, children: [] };
    if (depth >= MAX_DEPTH || ancestors.has(guid)) return node;
    const next = new Set(ancestors);
    next.add(guid);
    for (const edge of adjacency.get(guid) ?? []) {
      node.children.push(walk(edge.to, depth + 1, next));
    }
    return node;
  };

  return roots.map((root) => walk(root, 0, new Set()));
}

/* Full reachable closure, deduped, roots excluded (mirrors useModCoverage's
 * depth > 0 filter). Dangling guids are kept — callers resolve them against
 * graph.nodes when they need node data. */
export function buildFlat(roots: string[], graph: ModGraph): ReadonlySet<string> {
  const adjacency = forwardAdjacency(graph);
  const reached = new Set<string>();
  const seen = new Set<string>(roots);
  const stack: string[] = [];
  for (const root of roots) {
    for (const edge of adjacency.get(root) ?? []) stack.push(edge.to);
  }
  while (stack.length) {
    const guid = stack.pop() as string;
    if (seen.has(guid)) continue;
    seen.add(guid);
    reached.add(guid);
    for (const edge of adjacency.get(guid) ?? []) stack.push(edge.to);
  }
  return reached;
}

export function buildReverse(root: string, graph: ModGraph, mode: "nested"): TreeNode[];
export function buildReverse(root: string, graph: ModGraph, mode: "flat"): ReadonlySet<string>;
export function buildReverse(root: string, graph: ModGraph, mode: "nested" | "flat"): TreeNode[] | ReadonlySet<string> {
  // Walking backward: an edge whose `to === root` means its `from` requires root.
  const reverse = reverseAdjacency(graph);

  if (mode === "flat") {
    const reached = new Set<string>();
    const seen = new Set<string>([root]);
    const stack: string[] = [];
    for (const edge of reverse.get(root) ?? []) stack.push(edge.from);
    while (stack.length) {
      const guid = stack.pop() as string;
      if (seen.has(guid)) continue;
      seen.add(guid);
      reached.add(guid);
      for (const edge of reverse.get(guid) ?? []) stack.push(edge.from);
    }
    return reached;
  }

  const byGuid = nodeIndex(graph);
  const walk = (guid: string, depth: number, ancestors: Set<string>): TreeNode => {
    const node: TreeNode = { guid, node: byGuid.get(guid) ?? null, depth, children: [] };
    if (depth >= MAX_DEPTH || ancestors.has(guid)) return node;
    const next = new Set(ancestors);
    next.add(guid);
    for (const edge of reverse.get(guid) ?? []) {
      node.children.push(walk(edge.from, depth + 1, next));
    }
    return node;
  };
  return [walk(root, 0, new Set())];
}

/* Oldest Workshop check timestamp across the given guids — the graph is only as
 * fresh as its stalest node. */
export function freshnessOf(guids: string[], graph: ModGraph): string | null {
  const byGuid = nodeIndex(graph);
  let oldest: number | null = null;
  for (const guid of guids) {
    const checked = byGuid.get(guid)?.api_checked_at;
    if (!checked) continue;
    const ts = Date.parse(checked);
    if (Number.isNaN(ts)) continue;
    if (oldest === null || ts < oldest) oldest = ts;
  }
  return oldest === null ? null : new Date(oldest).toISOString();
}

export function coverageFromGraph(
  roots: string[],
  graph: ModGraph | undefined,
): { coverageByParent: Map<string, ReadonlySet<string>>; covered: ReadonlySet<string> } {
  if (!graph) {
    return { coverageByParent: new Map<string, ReadonlySet<string>>(), covered: new Set<string>() };
  }
  const coverageByParent = new Map<string, ReadonlySet<string>>();
  const covered = new Set<string>();
  for (const guid of roots) {
    const closure = buildFlat([guid], graph);
    coverageByParent.set(guid, closure);
    for (const reached of closure) covered.add(reached);
  }
  // A root that is reachable from another root must still render at top level.
  for (const guid of roots) covered.delete(guid);
  return { coverageByParent, covered };
}