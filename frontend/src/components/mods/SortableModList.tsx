import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { type ReactNode } from "react";
import { cn } from "../../lib/utils";

/**
 * Minimal shape a row needs to be dragged and identified. Anything with a
 * stable `key`, a `guid` and a display `name` can be listed — `ServerMod`
 * (Mods tab) and `ModpackItem` (S14) both satisfy it without either owning
 * the other's fields (`enabled`, pins).
 */
export type SortableRow = { key: string; guid: string; name: string | null };

type SortableModListProps<T extends SortableRow> = {
  items: T[];
  /** Called with the full list in its new order after a drag settles. */
  onReorder: (nextItemsInNewOrder: T[]) => void;
  /** Per-row slot rendered under the name (badges, pin reason, ...). */
  renderMeta?: (item: T) => ReactNode;
  /** Per-row slot rendered on the trailing edge (toggle, pin, remove, ...). */
  renderActions?: (item: T) => ReactNode;
  /** Disable all drag interaction (kept mounted so slots still render). */
  disabled?: boolean;
};

export function SortableModList<T extends SortableRow>({
  items,
  onReorder,
  renderMeta,
  renderActions,
  disabled = false,
}: SortableModListProps<T>) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const onDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = items.findIndex((item) => item.key === active.id);
    const to = items.findIndex((item) => item.key === over.id);
    if (from === -1 || to === -1) return;
    onReorder(arrayMove(items, from, to));
  };

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
      <SortableContext items={items.map((item) => item.key)} strategy={verticalListSortingStrategy}>
        <ul className="grid gap-2">
          {items.map((item, index) => (
            <SortableModRow
              key={item.key}
              item={item}
              index={index}
              disabled={disabled}
              renderMeta={renderMeta}
              renderActions={renderActions}
            />
          ))}
        </ul>
      </SortableContext>
    </DndContext>
  );
}

function SortableModRow<T extends SortableRow>({
  item,
  index,
  disabled,
  renderMeta,
  renderActions,
}: {
  item: T;
  index: number;
  disabled: boolean;
  renderMeta?: (item: T) => ReactNode;
  renderActions?: (item: T) => ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: item.key,
    disabled,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <li
      ref={setNodeRef}
      style={style}
      className={cn(
        "flex flex-wrap items-center gap-3 border border-stone-700 bg-stone-900/60 px-3 py-2",
        isDragging && "relative z-10 opacity-80 shadow-lg ring-1 ring-amber-400",
      )}
    >
      <button
        type="button"
        aria-label={`Drag to reorder ${item.name ?? item.guid}`}
        className={cn(
          "shrink-0 touch-none select-none px-1 text-base leading-none text-stone-500",
          disabled ? "cursor-not-allowed" : "cursor-grab hover:text-stone-200 active:cursor-grabbing",
        )}
        {...attributes}
        {...listeners}
      >
        ⠿
      </button>
      <span className="inline-flex h-5 min-w-[1.5rem] shrink-0 items-center justify-center border border-stone-600 px-1 font-mono text-[10px] text-stone-400">
        {index + 1}
      </span>
      <span className="min-w-0 flex-1 space-y-1">
        <span className="block truncate font-display text-sm uppercase tracking-wide text-stone-100">
          {item.name ?? item.guid}
        </span>
        <span className="block truncate font-mono text-[10px] text-stone-500">{item.guid}</span>
        {renderMeta?.(item)}
      </span>
      {renderActions && (
        <span className="flex shrink-0 flex-wrap items-center gap-2">{renderActions(item)}</span>
      )}
    </li>
  );
}
