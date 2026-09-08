import { useEffect, useState } from "react";
import {
  MessageCircle,
  Waypoints,
  Brain,
  GitBranch,
  ShoppingBasket,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { getJSON, type Customer } from "@/lib/api";
import { useLiveFeed, type LiveTrait } from "@/lib/live";
import { useAuth, logout, localLogout } from "@/lib/auth";
import { REL_LABEL } from "@/components/Badge";
import LoginGate from "@/components/LoginGate";
import CallbackPage from "@/components/CallbackPage";
import AuthCallbackPage from "@/components/AuthCallbackPage";
import DebugPanel from "@/components/DebugPanel";
import ChatTab from "@/tabs/ChatTab";
import GraphTab from "@/tabs/GraphTab";
import MemoryTab from "@/tabs/MemoryTab";
import PipelineTab from "@/tabs/PipelineTab";

const TABS = [
  { key: "chat", label: "쇼핑 상담", hint: "개인화 에이전트와 대화", icon: MessageCircle },
  { key: "graph", label: "개인화 그래프", hint: "Neptune 온톨로지 뷰", icon: Waypoints },
  { key: "memory", label: "메모리", hint: "short / long-term 기록", icon: Brain },
  { key: "pipeline", label: "동기화 파이프라인", hint: "Memory → Neptune 스트리밍", icon: GitBranch },
] as const;

type TabKey = (typeof TABS)[number]["key"];

// 로그인 사용자에 바인딩된 고객 — /api/me. 고객은 선택하는 게 아니라 계정에서 결정된다
// (alice→김유나, bob→박민준, dave→이서연). UI·BFF·에이전트·Neptune MCP가 같은 매핑.
interface Me {
  username: string;
  sub: string;
  groups: string[];
  customer: Customer | null;
}

export default function App() {
  const auth = useAuth();
  const [tab, setTab] = useState<TabKey>("chat");
  const [me, setMe] = useState<Me | null>(null);
  const cid = me?.customer?.customer_id ?? "";
  const [debugOpen, setDebugOpen] = useState(false);
  const [status, setStatus] = useState<any>(null);
  // GraphTab 등이 최신 데이터를 다시 불러오게 하는 신호
  const [refreshKey, setRefreshKey] = useState(0);
  // 실시간 성향 발견 토스트 (Kinesis → BFF SSE)
  const [toasts, setToasts] = useState<LiveTrait[]>([]);
  const live = useLiveFeed(cid, (t) => {
    setToasts((prev) => [...prev, t].slice(-4));
    setRefreshKey((k) => k + 1);
    setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== t.id)), 8000);
  });

  useEffect(() => {
    if (!auth) return;
    // /api/me 401 = 토큰이 revoke·만료됨(BFF가 Cognito에 온라인 확인) → IdP 세션(hosted UI 쿠키)까지
    // 끊고 로그인 화면으로. 로컬만 지우면 다음 SSO 로그인이 비번 없이 통과한다(revoke 무력화).
    getJSON<Me>("/api/me").then(setMe).catch((e) => {
      setMe(null);
      if (String(e?.message ?? "").startsWith("401")) void logout();
    });
    getJSON<any>("/api/status").then(setStatus).catch(() => {});
  }, [auth?.token]);

  const current = me?.customer ?? undefined;

  // 위키 3LO 동의 복귀 라우트 (팝업 탭)
  if (window.location.pathname === "/oauth/callback") return <CallbackPage />;
  // Agent IdP hosted UI 로그인 복귀 (SSO)
  if (window.location.pathname === "/auth/callback") return <AuthCallbackPage />;
  // IdP 로그아웃 복귀(single logout) — 마켓 로그아웃 또는 위키 뷰어 로그아웃 연쇄로
  // IdP 세션이 끊겼다 → 로컬 세션도 지우고 로그인 화면으로
  if (window.location.pathname === "/sso-logout") {
    localLogout();
    window.location.replace("/");
    return null;
  }

  if (!auth) return <LoginGate />;

  return (
    <div className="flex h-screen">
      {/* ── sidebar ── */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-[#e5e8ef] bg-white">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-600 text-white">
            <ShoppingBasket size={18} />
          </div>
          <div>
            <div className="text-[15px] font-bold text-slate-900">온톨로 마켓</div>
            <div className="text-[11px] text-slate-400">AgentCore × Neptune 초개인화</div>
          </div>
        </div>

        {/* 로그인 사용자 (OBO 체인의 주체) */}
        <div className="mx-4 mb-3 flex items-center justify-between rounded-xl border border-emerald-100 bg-emerald-50/60 px-3 py-2">
          <div className="min-w-0">
            <div className="text-[12px] font-bold text-emerald-800">
              🔐 {auth.username}
              <span className={cn(
                "ml-1.5 rounded-full px-1.5 py-0.5 text-[9.5px] font-bold",
                (auth.claims?.["cognito:groups"] ?? []).includes("premium")
                  ? "bg-amber-100 text-amber-700"
                  : "bg-slate-200 text-slate-600",
              )}>
                {(auth.claims?.["cognito:groups"] ?? []).includes("premium")
                  ? "⭐ premium" : "basic"}
              </span>
            </div>
            <div className="mono truncate text-[9.5px] text-emerald-600/70">
              JWT {auth.tokenPreview}
            </div>
          </div>
          <button
            onClick={logout}
            className="shrink-0 rounded-lg px-2 py-1 text-[10.5px] text-emerald-700 hover:bg-emerald-100"
          >
            로그아웃
          </button>
        </div>

        {/* 바인딩된 고객 프로필 — 선택 불가. 계정(JWT sub)이 곧 고객이다 */}
        <div className="mx-4 mb-4 rounded-xl border border-indigo-100 bg-indigo-50/60 p-3">
          <div className="mb-1.5 flex items-center gap-1 text-[11px] font-semibold text-indigo-500">
            <Sparkles size={12} /> 내 고객 프로필
          </div>
          {current ? (
            <div className="rounded-lg bg-white px-3 py-2 text-[13px] font-semibold text-indigo-700 shadow-sm ring-1 ring-indigo-200">
              <div>{current.name}</div>
              <div className="text-[11px] font-normal text-slate-400">{current.segment}</div>
              <div className="mono mt-1 text-[9.5px] font-normal text-indigo-400">
                {current.customer_id} ← {auth.username} (계정 바인딩)
              </div>
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-slate-300 bg-white/70 px-3 py-2 text-[11.5px] text-slate-500">
              {me ? "이 계정에 연결된 고객 프로필이 없습니다 — 개인화 없이 상담만 가능" : "불러오는 중…"}
            </div>
          )}
        </div>

        <nav className="flex flex-col gap-1 px-3">
          {TABS.map(({ key, label, hint, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={cn(
                "flex items-start gap-3 rounded-xl px-3 py-2.5 text-left transition",
                tab === key
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "text-slate-600 hover:bg-slate-100",
              )}
            >
              <Icon size={17} className="mt-0.5 shrink-0" />
              <span>
                <span className="block text-[13px] font-semibold">{label}</span>
                <span
                  className={cn(
                    "block text-[11px]",
                    tab === key ? "text-indigo-200" : "text-slate-400",
                  )}
                >
                  {hint}
                </span>
              </span>
            </button>
          ))}
        </nav>

        <div className="mt-auto space-y-1 border-t border-[#e5e8ef] px-5 py-4 text-[10.5px] text-slate-400">
          <div className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            Runtime · Strands Shopping Agent
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                live.connected ? "bg-emerald-500 pulse-soft" : "bg-slate-300",
              )}
            />
            Kinesis 실시간 스트림 {live.connected ? "연결됨" : "대기"}
          </div>
          <div className="mono truncate">memory: {status?.memoryId ?? "…"}</div>
          <div className="mono truncate">graph: {status?.graphId ?? "…"}</div>
        </div>
      </aside>

      {/* ── main ── debug 패널이 문서 흐름에 있어 열리면 본문이 자연스럽게 밀림 */}
      <main className="relative flex flex-1 flex-col overflow-hidden">
        {/* 인증 흐름 디버그 패널 + 토글 */}
        <DebugPanel open={debugOpen} onToggle={() => setDebugOpen((o) => !o)} />

        <div className="relative min-h-0 flex-1">
          {tab === "chat" && (
            <ChatTab cid={cid} customer={current} onDataChanged={() => setRefreshKey((k) => k + 1)} />
          )}
          {tab === "graph" && <GraphTab cid={cid} refreshKey={refreshKey} />}
          {tab === "memory" && <MemoryTab cid={cid} refreshKey={refreshKey} />}
          {tab === "pipeline" && (
            <PipelineTab cid={cid} liveTraits={live.traits} liveRecords={live.records}
              onDataChanged={() => setRefreshKey((k) => k + 1)} />
          )}
        </div>

        {/* 실시간 성향 발견 토스트 */}
        <div className="pointer-events-none absolute right-6 top-6 z-50 flex w-80 flex-col gap-2">
          {toasts.map((t) => (
            <div
              key={t.id}
              className="pop-in rounded-xl border border-indigo-200 bg-white/95 p-3.5 shadow-lg backdrop-blur"
            >
              <div className="flex items-center gap-1.5 text-[11px] font-bold text-indigo-500">
                <Sparkles size={12} className="pulse-soft" />
                지금 파악한 성향 · 그래프에 반영됨
              </div>
              <div className="mt-1 text-[13px] font-semibold text-slate-800">
                {REL_LABEL[t.type] ?? t.type} · {t.target}
                <span className="ml-1.5 text-[11px] font-normal text-slate-400">
                  신뢰도 {t.confidence} {t.existed ? "· 갱신" : "· 신규"}
                </span>
              </div>
              {t.record_text && (
                <div className="mt-1 truncate text-[11px] text-slate-400">
                  근거: {t.record_text}
                </div>
              )}
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
