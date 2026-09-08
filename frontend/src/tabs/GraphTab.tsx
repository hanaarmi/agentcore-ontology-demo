import { useEffect, useMemo, useState } from "react";
import { RefreshCw, UserRound, Flag } from "lucide-react";
import { getJSON, authHeader, type GraphNode, type GraphEdge, type Profile, type Candidate } from "@/lib/api";
import { Badge, REL_TONE, REL_LABEL } from "@/components/Badge";
import { pushDebug } from "@/lib/debug";

// 간단한 방사형 레이아웃: 고객 중심으로 Trait 링, 그 바깥에 Product 링,
// SharedInterest("외 N명")는 자신의 trait 노드 바로 바깥에 붙인다.
function layout(nodes: GraphNode[], W: number, H: number) {
  const cx = W / 2, cy = H / 2;
  const pos: Record<string, { x: number; y: number }> = {};
  const traits = nodes.filter((n) => n.label === "Trait");
  const bought = nodes.filter((n) => n.label === "Product");
  const cands = nodes.filter((n) => n.label === "ProductCandidate");
  nodes.forEach((n) => {
    if (n.label === "Customer") pos[n.id] = { x: cx, y: cy };
  });
  traits.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(traits.length, 1) - Math.PI / 2;
    pos[n.id] = { x: cx + 150 * Math.cos(a), y: cy + 130 * Math.sin(a) };
    // 대응하는 shared 노드는 같은 각도의 바깥 링에
    const sid = `shared:${n.name}`;
    if (nodes.some((m) => m.id === sid)) {
      pos[sid] = { x: cx + 228 * Math.cos(a), y: cy + 198 * Math.sin(a) };
    }
  });
  bought.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(bought.length, 1) - Math.PI / 2 + 0.25;
    pos[n.id] = { x: cx + 285 * Math.cos(a), y: cy + 240 * Math.sin(a) };
  });
  cands.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(cands.length, 1) - Math.PI / 2 + 0.12;
    pos[n.id] = { x: cx + 360 * Math.cos(a), y: cy + 295 * Math.sin(a) };
  });
  return pos;
}

const NODE_STYLE: Record<string, { fill: string; stroke: string; text: string }> = {
  Customer: { fill: "#4f46e5", stroke: "#4338ca", text: "#ffffff" },
  Trait: { fill: "#fef3c7", stroke: "#f59e0b", text: "#92400e" },
  Product: { fill: "#e0e7ff", stroke: "#818cf8", text: "#3730a3" },
  ProductCandidate: { fill: "#d1fae5", stroke: "#34d399", text: "#065f46" },
  SharedInterest: { fill: "#e0f2fe", stroke: "#38bdf8", text: "#075985" },
};

// 이 시간(ms) 안에 갱신된 trait 노드는 "방금 반영됨" 글로우를 받는다.
const RECENT_MS = 10 * 60 * 1000;

export default function GraphTab({ cid, refreshKey }: { cid: string; refreshKey: number }) {
  const [data, setData] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({ nodes: [], edges: [] });
  const [profile, setProfile] = useState<Profile | null>(null);
  const [recs, setRecs] = useState<Candidate[]>([]);
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  // 데이터팀 MCP 경유 결과가 "동의 필요(409)" / "tier 제한" / "오류"면 빈 화면 대신 배너로 설명
  const [notice, setNotice] = useState<{ kind: "consent" | "tier" | "error"; text: string; url?: string } | null>(null);

  // BFF는 Neptune을 직접 읽지 않고 통합 Gateway → Neptune MCP를 사용자 명의로 호출한다.
  // 그래서 에이전트와 똑같이 graph/read 동의(409 + consent_url)나 tier 제한이 여기서도 나타난다.
  const fetchJSON = async (url: string) => {
    const r = await fetch(url, { headers: authHeader() });
    const body = await r.json().catch(() => ({}));
    if (r.status === 409 && body.consent_url) throw { kind: "consent", text: body.message, url: body.consent_url };
    if (!r.ok) throw { kind: "error", text: body.error || `HTTP ${r.status}` };
    return body;
  };

  const load = () => {
    setLoading(true);
    setNotice(null);
    Promise.all([
      fetchJSON(`/api/graph/${cid}`),
      fetchJSON(`/api/profile/${cid}`),
      fetchJSON(`/api/recommendations/${cid}`),
    ])
      .then(([g, p, r]) => {
        setData({ nodes: g.nodes ?? [], edges: g.edges ?? [] });
        setProfile(p);
        setRecs(r.candidates ?? []);
        const tierLimited = !!(g.tier_limited || p.tier_limited);
        if (tierLimited) {
          setNotice({ kind: "tier", text: g.notice || p.notice || "basic 등급 — 개인화 그래프는 premium 전용입니다 (tier 진실원: LDAP → X-Tier)" });
        }
        // 인증 흐름 패널에도 남긴다 — 그래프 탭은 Neptune을 직접 읽지 않고 에이전트와 같은 경로를 탄다
        pushDebug({
          step: "⑤",
          title: tierLimited
            ? "그래프 탭 — 데이터팀 MCP가 tier 제한 (basic → 개인화 그래프 비공개)"
            : "그래프 탭 — 사용자 명의로 데이터팀 Neptune MCP 읽기 (Gateway 경유)",
          detail:
            "BFF는 Neptune(VPC B, 사설)에 직접 닿지 않습니다. 브라우저의 사용자 JWT를 그대로 통합 Gateway에 " +
            "보내고, Gateway가 interceptor로 X-Tier를 주입한 뒤 Tool IdP 위임 토큰(graph/read)으로 " +
            "Neptune MCP의 get_graph_view · get_customer_profile · get_personalized_candidates를 호출했습니다.",
          tool: "neptune___get_graph_view",
          level: tierLimited ? "warn" : undefined,
          data: {
            "경로": "브라우저 → BFF → 통합 Gateway → Neptune MCP(VPC B) → Neptune(private endpoint)",
            "결과": tierLimited
              ? `tier 제한 (${p.your_tier ?? "basic"})`
              : `노드 ${g.nodes?.length ?? 0} · 엣지 ${g.edges?.length ?? 0} · 추천 ${r.candidates?.length ?? 0} (personalized=${r.personalized})`,
            "쓰기 경로(별개)": "Kinesis 컨슈머 → Neptune MCP 직접 · 시스템 토큰 graph/write (사용자 토큰으로는 불가)",
          },
        });
      })
      .catch((e: any) => {
        setData({ nodes: [], edges: [] });
        setNotice(e?.kind ? e : { kind: "error", text: String(e?.message || e) });
        if (e?.kind === "consent") {
          pushDebug({
            step: "❷",
            title: "그래프 탭 — 개인화 위임 토큰 없음 → 데이터팀 SSO 동의 필요",
            detail:
              "이 사용자는 아직 Neptune(graph/read) 동의를 하지 않아 Token Vault에 위임 토큰이 없습니다. " +
              "Gateway가 MCP URL elicitation(-32042)으로 동의 URL을 돌려줬고, BFF가 409로 전달했습니다. " +
              "에이전트의 추천 도구와 동일한 최초 1회 동의입니다.",
            tool: "neptune",
            level: "warn",
            data: { "HTTP": "409 consent_required", "동의 URL": String(e.url).slice(0, 70) + "…" },
          });
        }
      })
      .finally(() => setLoading(false));
    // 요약은 LLM 호출이라 그래프 로딩과 분리 (캐시되어 있으면 즉시)
    getJSON<{ summary: string }>(`/api/summary/${cid}`)
      .then((d) => setSummary(d.summary))
      .catch(() => {});
  };

  const openConsent = (url: string) => {
    // 에이전트의 위키/Neptune 동의와 같은 팝업 — 마켓 SSO 세션이 있으면 폼 없이 동의만
    window.open(url, "ontolo-consent", "width=520,height=720");
  };

  useEffect(load, [cid, refreshKey]);

  const W = 860, H = 680;
  const pos = useMemo(() => layout(data.nodes, W, H), [data.nodes]);

  // 최근 반영된 trait 노드 집합 (엣지 updated_at 기준)
  const recentTraits = useMemo(() => {
    const now = Date.now();
    const s = new Set<string>();
    for (const e of data.edges) {
      if (e.source === "memory" && e.updated_at) {
        const t = Date.parse(e.updated_at);
        if (!Number.isNaN(t) && now - t < RECENT_MS) s.add(e.to);
      }
    }
    return s;
  }, [data.edges]);

  return (
    <div className="flex h-full">
      {/* graph canvas + summary */}
      <div className="relative flex flex-1 flex-col overflow-hidden">
        <header className="absolute left-0 right-0 top-0 z-10 flex items-center justify-between border-b border-[#e5e8ef] bg-white/85 px-8 py-4 backdrop-blur">
          <div>
            <h1 className="text-[17px] font-bold text-slate-900">개인화 그래프</h1>
            <p className="text-[12.5px] text-slate-400">
              Neptune Analytics 온톨로지 — 노드 {data.nodes.length} · 엣지 {data.edges.length}
            </p>
          </div>
          <button
            onClick={load}
            className="flex items-center gap-1.5 rounded-lg border border-[#e5e8ef] bg-white px-3 py-1.5 text-[12px] text-slate-600 hover:bg-slate-50"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> 새로고침
          </button>
        </header>

        {notice && (
          <div
            className={
              "absolute left-8 right-8 top-[76px] z-10 flex items-center justify-between gap-3 rounded-xl border px-4 py-2.5 text-[12.5px] " +
              (notice.kind === "consent"
                ? "border-violet-200 bg-violet-50 text-violet-800"
                : notice.kind === "tier"
                  ? "border-amber-200 bg-amber-50 text-amber-800"
                  : "border-rose-200 bg-rose-50 text-rose-700")
            }
          >
            <span>
              {notice.kind === "consent" && <b className="mr-1.5">그래프 접근 동의 필요 —</b>}
              {notice.kind === "tier" && <b className="mr-1.5">등급 제한 —</b>}
              {notice.kind === "error" && <b className="mr-1.5">데이터팀 MCP 호출 실패 —</b>}
              {notice.text}
              {notice.kind === "consent" && (
                <span className="ml-1 text-[11.5px] opacity-80">
                  (이 화면도 에이전트처럼 사용자 명의 위임 토큰으로 Neptune MCP를 호출합니다 · 동의 후 새로고침)
                </span>
              )}
            </span>
            {notice.kind === "consent" && notice.url && (
              <button
                onClick={() => openConsent(notice.url!)}
                className="shrink-0 rounded-lg bg-violet-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-violet-700"
              >
                동의하기 (SSO)
              </button>
            )}
          </div>
        )}

        <svg viewBox={`0 0 ${W} ${H}`} className="min-h-0 w-full flex-1 pt-16">
          {data.edges.map((e, i) => {
            const a = pos[e.from], b = pos[e.to];
            if (!a || !b) return null;
            const fromMemory = e.source === "memory";
            return (
              <g key={i}>
                <line
                  x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                  stroke={fromMemory ? "#f59e0b" : e.rel === "MATCHES" ? "#a7f3d0"
                    : e.rel === "SHARED_BY" ? "#7dd3fc" : "#cbd5e1"}
                  strokeWidth={fromMemory ? 2.2 : 1.2}
                  strokeDasharray={e.rel === "MATCHES" || e.rel === "SHARED_BY" ? "4 4" : undefined}
                />
                <text
                  x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4}
                  textAnchor="middle" fontSize="8.5"
                  fill={fromMemory ? "#b45309" : "#94a3b8"}
                >
                  {REL_LABEL[e.rel] ?? e.rel}
                  {e.confidence != null ? ` ${Number(e.confidence).toFixed(1)}` : ""}
                </text>
              </g>
            );
          })}
          {data.nodes.map((n) => {
            const p = pos[n.id];
            if (!p) return null;
            const s = NODE_STYLE[n.label];
            const r = n.label === "Customer" ? 34 : n.label === "Trait" ? 26 : 20;
            const glowing = recentTraits.has(n.id);
            return (
              <g
                key={n.id}
                className="cursor-pointer"
                onClick={() => setSelected(n)}
                transform={`translate(${p.x},${p.y})`}
              >
                <circle r={r} fill={s.fill} stroke={glowing ? "#f59e0b" : s.stroke}
                        strokeWidth={glowing ? 2.5 : 1.5} />
                <text
                  textAnchor="middle" dy={n.label === "Customer" ? 4 : 3}
                  fontSize={n.label === "Customer" ? 12 : 9}
                  fontWeight={n.label === "Customer" ? 700 : 500}
                  fill={s.text}
                >
                  {n.name.length > 8 ? n.name.slice(0, 7) + "…" : n.name}
                </text>
              </g>
            );
          })}
        </svg>

        {/* 방금 반영 플래그 (우측 상단) */}
        {recentTraits.size > 0 && (
          <div className="absolute right-6 top-20 w-56 rounded-xl border border-amber-200 bg-amber-50/95 p-3 backdrop-blur pop-in">
            <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-700">
              <Flag size={12} /> 방금 반영된 성향 ({recentTraits.size})
            </div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {[...recentTraits].map((id) => (
                <span key={id}
                  className="rounded-full border border-amber-300 bg-white px-2 py-0.5 text-[11px] font-medium text-amber-800">
                  {id.replace(/^trait:/, "")}
                </span>
              ))}
            </div>
            <div className="mt-1.5 text-[10px] text-amber-600/70">
              그래프에서 주황 테두리로 표시 · 10분 후 일반 표시로 전환
            </div>
          </div>
        )}

        {/* legend */}
        <div className="absolute right-6 bottom-[168px] flex gap-3 rounded-xl border border-[#e5e8ef] bg-white/90 px-4 py-2 text-[11px] text-slate-500 backdrop-blur">
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-indigo-600" /> 고객</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-amber-300" /> 취향/제약</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-indigo-200" /> 구매 상품</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-emerald-300" /> 추천 후보</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-sky-300" /> 비슷한 고객</span>
          <span className="flex items-center gap-1"><span className="h-0.5 w-4 bg-amber-500" /> 메모리 유래</span>
          <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full border-2 border-amber-500 bg-amber-100" /> 방금 반영</span>
        </div>

        {/* persona summary */}
        <div className="shrink-0 border-t border-[#e5e8ef] bg-white px-8 py-4">
          <div className="flex items-center gap-1.5 text-[12px] font-bold text-slate-700">
            <UserRound size={13} className="text-indigo-500" />
            이 고객은 어떤 사람인가요?
            <span className="text-[10.5px] font-normal text-slate-400">
              — 그래프 기반 AI 요약, 성향이 바뀌면 자동 갱신
            </span>
          </div>
          <p className="mt-1.5 max-h-28 overflow-y-auto text-[12.5px] leading-relaxed text-slate-600">
            {summary || "요약 생성 중…"}
          </p>
        </div>

        {selected && (
          <div className="absolute left-6 top-24 w-56 rounded-xl border border-[#e5e8ef] bg-white p-4 shadow-lg pop-in">
            <div className="text-[13px] font-bold text-slate-800">{selected.name}</div>
            <div className="mt-1 text-[11px] text-slate-400">
              {selected.label} {selected.kind ? `· ${selected.kind}` : ""}
              {selected.sku ? ` · ${selected.sku}` : ""}
            </div>
            <button
              onClick={() => setSelected(null)}
              className="mt-3 text-[11px] text-indigo-500 hover:underline"
            >
              닫기
            </button>
          </div>
        )}
      </div>

      {/* right panel: traits + recommendations */}
      <aside className="w-80 shrink-0 overflow-y-auto border-l border-[#e5e8ef] bg-white p-5">
        <h2 className="text-[13px] font-bold text-slate-700">취향 · 제약 요약</h2>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {profile?.traits.map((t, i) => (
            <Badge key={i} tone={REL_TONE[t.rel] ?? "slate"}>
              {REL_LABEL[t.rel] ?? t.rel} · {t.trait}
              <span className="opacity-60">{Number(t.confidence).toFixed(1)}</span>
              {t.source === "memory" && <span title="메모리에서 학습됨">🧠</span>}
            </Badge>
          ))}
          {!profile?.traits.length && (
            <span className="text-[12px] text-slate-400">아직 없음</span>
          )}
        </div>

        <h2 className="mt-6 text-[13px] font-bold text-slate-700">그래프 기반 추천 후보</h2>
        <div className="mt-2 flex flex-col gap-2">
          {recs.map((r) => (
            <div key={r.sku} className="rounded-xl border border-[#e5e8ef] p-3">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[12.5px] font-semibold text-slate-700">{r.name}</span>
                <span className="shrink-0 text-[12px] font-bold text-indigo-600">
                  {r.price?.toLocaleString()}원
                </span>
              </div>
              <div className="mt-1 text-[11px] text-slate-400">
                {r.brand} · {r.category}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {r.matched_traits.map((t) => (
                  <Badge key={t} tone="emerald">{t}</Badge>
                ))}
                <Badge tone="slate">score {Number(r.score).toFixed(1)}</Badge>
              </div>
            </div>
          ))}
          {!recs.length && <span className="text-[12px] text-slate-400">추천 후보 없음</span>}
        </div>
      </aside>
    </div>
  );
}
