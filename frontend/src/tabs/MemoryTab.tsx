import { useEffect, useState } from "react";
import { RefreshCw, MessageSquareText, Sparkles } from "lucide-react";
import { getJSON, type MemoryData } from "@/lib/api";
import { Badge } from "@/components/Badge";
import { cn } from "@/lib/cn";

export default function MemoryTab({ cid, refreshKey }: { cid: string; refreshKey: number }) {
  const [data, setData] = useState<MemoryData | null>(null);
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    getJSON<MemoryData>(`/api/memory/${cid}`)
      .then(setData)
      .finally(() => setLoading(false));
  };

  useEffect(load, [cid, refreshKey]);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[#e5e8ef] bg-white px-8 py-4">
        <div>
          <h1 className="text-[17px] font-bold text-slate-900">AgentCore Memory</h1>
          <p className="text-[12.5px] text-slate-400">
            좌: short-term 대화 이벤트 · 우: 비동기 추출된 long-term 레코드
            (추출은 대화 후 1~2분 소요)
          </p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-1.5 rounded-lg border border-[#e5e8ef] bg-white px-3 py-1.5 text-[12px] text-slate-600 hover:bg-slate-50"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> 새로고침
        </button>
      </header>

      <div className="grid flex-1 grid-cols-2 gap-6 overflow-hidden p-6">
        {/* short-term */}
        <section className="panel flex flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-[#e5e8ef] px-5 py-3">
            <MessageSquareText size={15} className="text-slate-400" />
            <span className="text-[13px] font-bold text-slate-700">Short-term (대화 이벤트)</span>
            <Badge tone="slate">{data?.shortTerm.length ?? 0}건</Badge>
          </div>
          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {data?.shortTerm.map((e, i) => (
              <div
                key={i}
                className={cn(
                  "rounded-xl px-3.5 py-2.5 text-[12.5px]",
                  e.role === "USER"
                    ? "bg-indigo-50 text-indigo-800"
                    : "bg-slate-50 text-slate-600",
                )}
              >
                <div className="mb-0.5 flex items-center justify-between text-[10.5px] opacity-60">
                  <span className="font-semibold">{e.role}</span>
                  <span className="mono">{e.sessionId} · {String(e.ts).slice(0, 19)}</span>
                </div>
                {e.text}
              </div>
            ))}
            {!data?.shortTerm.length && (
              <div className="pt-8 text-center text-[12.5px] text-slate-400">
                아직 대화 이벤트가 없습니다. 쇼핑 상담 탭에서 대화해보세요.
              </div>
            )}
          </div>
        </section>

        {/* long-term */}
        <section className="panel flex flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-[#e5e8ef] px-5 py-3">
            <Sparkles size={15} className="text-amber-500" />
            <span className="text-[13px] font-bold text-slate-700">
              Long-term (비동기 추출 레코드)
            </span>
            <Badge tone="amber">{data?.longTerm.length ?? 0}건</Badge>
          </div>
          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {data?.longTerm.map((r, i) => (
              <div key={i} className="rounded-xl border border-amber-100 bg-amber-50/50 px-3.5 py-2.5">
                <div className="mb-1 flex items-center justify-between">
                  <Badge tone={r.namespace.includes("preferences") ? "indigo" : "sky"}>
                    {r.namespace.includes("preferences") ? "선호 추출" : "사실 추출"}
                  </Badge>
                  <span className="text-[10.5px] text-slate-400">
                    {String(r.createdAt).slice(0, 19)}
                  </span>
                </div>
                <div className="text-[12.5px] leading-relaxed text-slate-700">{r.text}</div>
              </div>
            ))}
            {!data?.longTerm.length && (
              <div className="pt-8 text-center text-[12.5px] text-slate-400">
                아직 추출된 레코드가 없습니다.
                <br />
                대화 후 1~2분 뒤 AgentCore가 자동으로 취향/사실을 추출합니다.
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
