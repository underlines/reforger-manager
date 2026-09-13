import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";
import { api } from "./api";

/* Shared dependency-coverage plumbing (moved verbatim out of the Mods tab in
 * Sprint 9 S1). Given a list of explicitly-assigned mods, fetches each pick's
 * dependency tree — one ["mod", guid, "deps"] query per pick, the exact cache
 * key AddModRow / AssignedModExpanded share — and computes which OTHER explicit
 * picks each pick covers through its dependency closure. Consumers hide covered
 * picks from the top level but keep them in storage. */

export type DepNode = { guid: string; name: string | null; via: string; state: string; depth: number };
export type ModDeps = { dependency_tree: { nodes: DepNode[] } | null };

export function useModCoverage<T extends { mod_guid: string }>(
  items: T[],
): { coverageByParent: Map<string, ReadonlySet<string>>; covered: ReadonlySet<string> } {
  // One deps query per explicit pick, keyed exactly like AddModRow /
  // AssignedModExpanded so all consumers share a single cache entry.
  const depResults = useQueries({
    queries: items.map((item) => ({
      queryKey: ["mod", item.mod_guid, "deps"],
      queryFn: () => api<ModDeps>(`/api/mods/${item.mod_guid}`),
      staleTime: 60_000,
    })),
  });

  // Signature that changes only when a deps query gains/loses data — keeps the
  // memo below from recomputing on every unrelated render.
  const depSig = depResults
    .map((result) => (result.data ? String(result.dataUpdatedAt) : result.status))
    .join("|");

  // guid -> set of directly-declared, resolved dependency guids (undefined while
  // that query is still loading → "covers nothing" for now).
  const depEdges = useMemo(() => {
    const map = new Map<string, Set<string> | undefined>();
    items.forEach((item, index) => {
      const data = depResults[index]?.data;
      if (!data) {
        map.set(item.mod_guid, undefined);
        return;
      }
      const nodes = data.dependency_tree?.nodes ?? [];
      map.set(
        item.mod_guid,
        new Set(
          nodes
            .filter((node) => node.depth > 0 && node.state !== "unresolved")
            .map((node) => node.guid),
        ),
      );
    });
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, depSig]);

  // parent guid -> every OTHER explicit pick reachable through its closure.
  // Walked with a visited set so A→B→A metadata can't loop.
  const coverageByParent = useMemo<Map<string, ReadonlySet<string>>>(() => {
    const explicit = new Set(items.map((item) => item.mod_guid));
    const map = new Map<string, ReadonlySet<string>>();
    for (const parent of items) {
      const reached = new Set<string>();
      const seen = new Set<string>([parent.mod_guid]);
      const stack = [...(depEdges.get(parent.mod_guid) ?? [])];
      while (stack.length) {
        const guid = stack.pop() as string;
        if (seen.has(guid)) continue;
        seen.add(guid);
        if (explicit.has(guid)) reached.add(guid);
        const next = depEdges.get(guid);
        if (next) for (const g of next) if (!seen.has(g)) stack.push(g);
      }
      reached.delete(parent.mod_guid);
      map.set(parent.mod_guid, reached);
    }
    return map;
  }, [items, depEdges]);

  const covered = useMemo<ReadonlySet<string>>(() => {
    const set = new Set<string>();
    for (const reached of coverageByParent.values())
      for (const guid of reached) set.add(guid);
    return set;
  }, [coverageByParent]);

  return { coverageByParent, covered };
}
