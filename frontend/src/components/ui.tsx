import { cva, type VariantProps } from "class-variance-authority";
import {
  forwardRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";
import { cn } from "../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-sm border font-display text-sm font-bold uppercase tracking-wider transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "border-amber-500 bg-amber-500 px-3 py-2 text-stone-950 hover:bg-amber-400",
        outline: "border-stone-500 bg-transparent px-3 py-2 text-stone-100 hover:border-stone-300",
        ghost: "border-transparent bg-transparent px-3 py-2 text-stone-300 hover:bg-stone-800",
      },
      size: {
        default: "h-9",
        sm: "h-8 text-xs",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>;

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, ...props }, ref) => (
  <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
));
Button.displayName = "Button";

export const Card = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <section className={cn("border border-stone-700 bg-stone-900/80", className)} {...props} />
);
export const CardHeader = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("border-b border-stone-800 px-4 py-3", className)} {...props} />
);
export const CardTitle = ({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) => (
  <h2 className={cn("font-display text-lg font-bold uppercase tracking-wide", className)} {...props} />
);
export const CardContent = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("p-4", className)} {...props} />
);

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "h-10 w-full rounded-sm border border-stone-600 bg-stone-950 px-3 text-sm text-stone-100 outline-none placeholder:text-stone-500 focus:border-amber-400 focus:ring-1 focus:ring-amber-400",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export const Badge = ({
  className,
  tone = "neutral",
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: "neutral" | "good" | "warn" | "bad" }) => (
  <span
    className={cn(
      "inline-flex items-center border px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest",
      {
        "border-stone-600 text-stone-300": tone === "neutral",
        "border-emerald-700 bg-emerald-950/60 text-emerald-300": tone === "good",
        "border-amber-700 bg-amber-950/50 text-amber-300": tone === "warn",
        "border-red-800 bg-red-950/50 text-red-300": tone === "bad",
      },
      className,
    )}
    {...props}
  />
);

export function Dialog({
  open,
  title,
  children,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" role="presentation">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        className="w-full max-w-md border border-stone-600 bg-stone-950 shadow-2xl"
      >
        <div className="flex items-center justify-between border-b border-stone-800 px-4 py-3">
          <h2 id="dialog-title" className="font-display text-lg font-bold uppercase">
            {title}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}