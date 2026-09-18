import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Badge } from "../ui";
import { cn } from "../../lib/utils";
import type { ModGraph, ModGraphNode, TreeNode } from "../../lib/modGraph";

type ModTreeProps = {
  graph: ModGraph;
  mode: "nested" | "flat";
  nested?: TreeNode[];
  flat?: ReadonlySet<string>;
  /** When given, a resolvable guid renders as <Link to={linkTo(guid)}>. */
  linkTo?: (guid: string) => string;
  /** Guids to apply a brief highlight ring to. Caller owns clearing this after render. */
  highlightGuids?: ReadonlySet<string>;
};

function apiStateTone(state: ModGraphNode["api_state"]): "good" | "bad" | "warn" {
  switch (state) {
    case "ok":
      return "good";
    case "not_found":
      return "bad";
    case "unchecked":
    default:
      return "warn";
  }
}

/** One row's contents — shared by nested and flat rendering. */
function ModTreeRowContent({
  guid,
  node,
  linkTo,
}: {
  guid: string;
  node: ModGraphNode | null;
  linkTo?: (guid: string) => string;
}) {
  if (!node) {
    return (
      <>
        <span className="min-w-0 truncate font-mono text-stone-500">{guid}</span>
        <Badge tone="warn">unresolved</Badge>
      </>
    );
  }

  const label = node.name ?? node.guid;
  const nameNode =
    linkTo && !node.is_builtin ? (
      <Link
        to={linkTo(guid)}
        onClick={(e) => e.stopPropagation()}
        className="min-w-0 truncate font-mono text-stone-300 hover:text-amber-400"
      >
        {label}
      </Link>
    ) : (
      <span className="min-w-0 truncate font-mono text-stone-300">{label}</span>
    );

  return (
    <>
      {nameNode}
      {node.is_builtin ? (
        <Badge tone="neutral">engine</Badge>
      ) : (
        <Badge tone={apiStateTone(node.api_state)}>{node.api_state}</Badge>
      )}
    </>
  );
}

function ModTreeRow({
  path,
  guid,
  node,
  depth,
  linkTo,
  highlightGuids,
}: {
  path: string;
  guid: string;
  node: ModGraphNode | null;
  depth: number;
  linkTo?: (guid: string) => string;
  highlightGuids?: ReadonlySet<string>;
}) {
  return (
    <li
      key={`${path}-${guid}`}
      className={cn(
        "flex flex-wrap items-center gap-2 py-2",
        highlightGuids?.has(guid) && "relative z-10 shadow-lg ring-1 ring-amber-400",
      )}
      style={{ paddingLeft: `${Math.min(depth, 8) * 1.25}rem` }}
    >
      <ModTreeRowContent guid={guid} node={node} linkTo={linkTo} />
    </li>
  );
}

/* Flattened pre-order walk into one sibling list, each row keyed by its full
 * ancestor path — a guid can legitimately recur at multiple depths/parents, so
 * <li> elements are never DOM-nested inside each other (invalid HTML; <li> may
 * not contain another <li> without an intervening <ul>). Depth-based padding
 * on each row conveys the hierarchy instead, matching the existing flat,
 * depth-indented convention in ModDetail.tsx's dependency list. */
function renderNested(
  nodes: TreeNode[],
  parentPath: string,
  linkTo: ((guid: string) => string) | undefined,
  highlightGuids: ReadonlySet<string> | undefined,
): ReactNode[] {
  return nodes.flatMap((treeNode) => {
    const path = `${parentPath}/${treeNode.guid}`;
    const row = (
      <ModTreeRow
        key={`${path}-${treeNode.guid}`}
        path={path}
        guid={treeNode.guid}
        node={treeNode.node}
        depth={treeNode.depth}
        linkTo={linkTo}
        highlightGuids={highlightGuids}
      />
    );
    return treeNode.children.length
      ? [row, ...renderNested(treeNode.children, path, linkTo, highlightGuids)]
      : [row];
  });
}

export function ModTree({ graph, mode, nested, flat, linkTo, highlightGuids }: ModTreeProps) {
  if (mode === "nested") {
    const roots = nested ?? [];
    return <ul className="divide-y divide-stone-800">{renderNested(roots, "", linkTo, highlightGuids)}</ul>;
  }

  const byGuid = new Map(graph.nodes.map((node) => [node.guid, node]));
  const guids = [...(flat ?? [])].sort();

  return (
    <ul className="divide-y divide-stone-800">
      {guids.map((guid) => (
        <ModTreeRow
          key={`${guid}-${guid}`}
          path={guid}
          guid={guid}
          node={byGuid.get(guid) ?? null}
          depth={0}
          linkTo={linkTo}
          highlightGuids={highlightGuids}
        />
      ))}
    </ul>
  );
}
