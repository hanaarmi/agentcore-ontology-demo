import { useState } from "react";
import {
  Play, Loader2, ArrowRight, Brain, Waypoints, CheckCircle2,
  Radio, MessageCircle, Sparkles,
} from "lucide-react";
import { streamSSE } from "@/lib/api";
import type { LiveTrait, LiveRecord } from "@/lib/live";
import { Badge, REL_TONE, REL_LABEL } from "@/components/Badge";
import { cn } from "@/lib/cn";

interface LogItem {
  kind: "records" | "facts" | "edge" | "done" | "error";
  data: any;
}

export default function PipelineTab({
  cid,
  liveTraits,
  liveRecords,
  onDataChanged,
}: {
  cid: string;
  liveTraits: LiveTrait[];
  liveRecords: LiveRecord[];
  onDataChanged: () => void;
}) {
  const [running, setRunning] = useState(false);
  const [log, setLog] = useState<LogItem[]>([]);

  // 실시간 피드: record(추출됨)와 trait(그래프 반영)를 시간순으로 병합
  const feed = [
    ...liveRecords.map((r) => ({ ts: r.ts, kind: "record" as const, r })),
    ...liveTraits.map((t) => ({ ts: t.ts, kind: "trait" as const, t })),
  ].sort((a, b) => b.ts - a.ts);

  const runBackfill = () => {
    if (running) return;
    setRunning(true);
    setLog([]);
    streamSSE(`/api/sync/${cid}`, null, (ev, data) => {
      if (ev === "records") setLog((l) => [...l, { kind: "records", data }]);
      else if (ev === "facts") setLog((l) => [...l, { kind: "facts", data }]);
      else if (ev === "edge") setLog((l) => [...l, { kind: "edge", data }]);
      else if (ev === "done") {
        setLog((l) => [...l, { kind: "done", data }]);
        setRunning(false);
        onDataChanged();
      } else if (ev === "error") {
        setLog((l) => [...l, { kind: "error", data }]);
        setRunning(false);
      }
    });
  };

  const stages = [
    { icon: MessageCircle, label: "대화", desc: "short-term 이벤트 저장" },
    { icon: Brain, label: "비동기 추출", desc: "AgentCore 장기 기억 생성" },
    { icon: Radio, label: "Kinesis 스트림", desc: "record 이벤트 push" },
    { icon: Waypoints, label: "Neptune MERGE", desc: "LLM 정규화 → Trait 엣지" },
  ];

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <header className="border-b border-[#e5e8ef] bg-white px-8 py-4">
        <h1 className="text-[17px] font-bold text-slate-900">실시간 성향 파이프라인</h1>
        <p className="text-[12.5px] text-slate-400">
          대화 → AgentCore 비동기 추출 → Kinesis → Neptune 투영이 자동으로
          흐릅니다. 아래 피드는 이 고객의 실시간 반영 내역입니다.
        </p>
      </header>

      <div className="mx-auto w-full max-w-3xl flex-1 p-8">
        {/* pipeline diagram */}
        <div className="flex items-center justify-between gap-2">
          {stages.map((s, i) => {
            const Icon = s.icon;
            return (
              <div key={i} className="flex flex-1 items-center gap-2">
                <div className="panel flex flex-1 flex-col items-center gap-1.5 px-3 py-4 text-center">
                  <Icon size={20} className="text-indigo-400" />
                  <div className="text-[12px] font-semibold text-slate-700">{s.label}</div>
                  <div className="text-[10.5px] text-slate-400">{s.desc}</div>
                </div>
                {i < stages.length - 1 && (
                  <ArrowRight size={16} className="shrink-0 text-slate-300" />
                )}
              </div>
            );
          })}
        </div>

        {/* live feed */}
        <div className="mt-6 flex items-center justify-between">
          <h2 className="flex items-center gap-1.5 text-[13.5px] font-bold text-slate-700">
            <Radio size={14} className="text-emerald-500 pulse-soft" />
            실시간 반영 피드
          </h2>
          <button
            onClick={runBackfill}
            disabled={running}
            className="flex items-center gap-1.5 rounded-lg border border-[#e5e8ef] bg-white px-3 py-1.5 text-[12px] text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
            title="기존 장기 기억 전체를 다시 그래프에 투영합니다"
          >
            {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
            전체 백필 실행
          </button>
        </div>

        <div className="mt-3 space-y-2">
          {feed.length === 0 && log.length === 0 && (
            <div className="panel p-6 text-center text-[12.5px] text-slate-400">
              아직 실시간 이벤트가 없습니다. 쇼핑 상담 탭에서 취향이 드러나는
              대화를 해보세요 — 추출이 끝나는 순간(대화 후 1~2분) 여기와
              그래프에 자동 반영됩니다.
            </div>
          )}

          {feed.map((item) =>
            item.kind === "record" ? (
              <div key={`r${item.r.id}`} className="panel pop-in flex items-start gap-2.5 p-3.5">
                <Brain size={15} className="mt-0.5 shrink-0 text-amber-500" />
                <div className="min-w-0">
                  <div className="text-[11px] font-semibold text-amber-600">
                    장기 기억 추출됨 · {item.r.strategy === "USER_PREFERENCE" ? "선호" : "사실"}
                  </div>
                  <div className="text-[12.5px] text-slate-600">{item.r.text}</div>
                </div>
              </div>
            ) : (
              <div key={`t${item.t.id}`} className="panel pop-in flex items-start gap-2.5 border-indigo-200 p-3.5">
                <Sparkles size={15} className="mt-0.5 shrink-0 text-indigo-500" />
                <div className="min-w-0">
                  <div className="text-[11px] font-semibold text-indigo-500">
                    그래프 반영 {item.t.existed ? "(갱신)" : "(신규 엣지)"}
                  </div>
                  <div className="mono text-[12px] text-slate-600">
                    (고객)-[:{item.t.type} {"{"}confidence: {item.t.confidence}{"}"}]→(:{item.t.target})
                  </div>
                  <Badge tone={REL_TONE[item.t.type] ?? "slate"} className="mt-1">
                    {REL_LABEL[item.t.type] ?? item.t.type} · {item.t.target}
                  </Badge>
                </div>
              </div>
            ),
          )}
        </div>

        {/* backfill log */}
        {log.length > 0 && (
          <div className="mt-6 space-y-2.5">
            <h2 className="text-[13px] font-bold text-slate-500">백필 로그</h2>
            {log.map((item, i) => (
              <div key={i} className="panel p-4 pop-in">
                {item.kind === "records" && (
                  <div className="text-[12.5px] text-slate-600">
                    ① Memory 레코드 <b>{item.data.count}건</b> 읽음
                  </div>
                )}
                {item.kind === "facts" && (
                  <>
                    <div className="mb-2 text-[12.5px] font-bold text-slate-700">
                      ② 정규화된 그래프 팩트 {item.data.count}건
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {item.data.facts?.map((f: any, j: number) => (
                        <Badge key={j} tone={REL_TONE[f.type] ?? "slate"}>
                          {REL_LABEL[f.type] ?? f.type} · {f.target} ({f.confidence})
                        </Badge>
                      ))}
                    </div>
                  </>
                )}
                {item.kind === "edge" && (
                  <div className="flex items-center gap-2 text-[12.5px] text-slate-600">
                    <Waypoints size={14} className="text-amber-500" />
                    <span className="mono">
                      (고객)-[:{item.data.type}]→(:{item.data.target})
                    </span>
                    <Badge tone={item.data.existed ? "amber" : "emerald"}>
                      {item.data.existed ? "갱신" : "신규"}
                    </Badge>
                  </div>
                )}
                {item.kind === "done" && (
                  <div className={cn("flex items-center gap-2 text-[13px] font-semibold text-emerald-600")}>
                    <CheckCircle2 size={16} />
                    백필 완료 — 엣지 {item.data.synced}건
                    {item.data.message && (
                      <span className="font-normal text-slate-500">· {item.data.message}</span>
                    )}
                  </div>
                )}
                {item.kind === "error" && (
                  <div className="text-[13px] text-rose-600">⚠️ {item.data.message}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
