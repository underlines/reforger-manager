import { ExternalLink } from "lucide-react";
import { cn } from "../../lib/utils";

/* Bohemia has no public API — this is the same base URL the backend's
 * workshop.py docstring cites, and /mods/add accepts a bare
 * `.../workshop/{guid}` URL (see scripts/e2e_live.py), so a guid alone is
 * enough to link out without hitting the API for a slug. */
const workshopUrl = (guid: string) => `https://reforger.armaplatform.com/workshop/${guid}`;

export function WorkshopLink({ guid, size = 12, className }: { guid: string; size?: number; className?: string }) {
  return (
    <a
      href={workshopUrl(guid)}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => event.stopPropagation()}
      title="Open on the Reforger Workshop"
      aria-label="Open on the Reforger Workshop"
      className={cn("inline-flex shrink-0 text-stone-500 hover:text-amber-400", className)}
    >
      <ExternalLink size={size} />
    </a>
  );
}
