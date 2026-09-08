import { cn } from "@/lib/cn";

const tones = {
  indigo: "bg-indigo-50 text-indigo-700 border-indigo-200",
  emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
  amber: "bg-amber-50 text-amber-700 border-amber-200",
  rose: "bg-rose-50 text-rose-700 border-rose-200",
  sky: "bg-sky-50 text-sky-700 border-sky-200",
  slate: "bg-slate-100 text-slate-600 border-slate-200",
} as const;

export type Tone = keyof typeof tones;

export function Badge({
  tone = "slate",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export const REL_TONE: Record<string, Tone> = {
  PREFERS: "indigo",
  INTERESTED_IN: "sky",
  HAS_CONSTRAINT: "rose",
  HAS_LIFESTYLE: "emerald",
  OWNS_PET: "amber",
  PURCHASED: "slate",
};

export const REL_LABEL: Record<string, string> = {
  PREFERS: "선호",
  INTERESTED_IN: "관심사",
  HAS_CONSTRAINT: "제약",
  HAS_LIFESTYLE: "라이프스타일",
  OWNS_PET: "반려동물",
  PURCHASED: "구매",
  SHARED_BY: "같은 관심",
};
