import { useEffect, useRef, useState } from "react";
import {
  Bug, ChevronDown, ChevronUp, KeyRound, Trash2, X,
  Monitor, Server, Bot, DoorOpen, Wrench, Database, ArrowRight, BookOpen,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { useDebugEvents, clearDebug, type AuthDebugEvent } from "@/lib/debug";
import RevokeTools from "@/components/RevokeTools";

// 체인 노드 — step 배지가 어떤 홉에서 발생했는지 시각화.
// 사용자 JWT는 통합 Gateway까지만. Gateway가 interceptor로 LDAP을 조회해 X-Tier를 주입하고,
// 백엔드(데이터팀 VPC B의 Neptune MCP)엔 Tool IdP 위임 토큰으로 스왑해 호출한다(심층 방어).
const CHAIN = [
  { icon: Monitor, label: "브라우저", sub: "① SSO 로그인 (hosted UI)" },
  { icon: Server, label: "BFF", sub: "② JWT 검증·고객 바인딩" },
  { icon: Bot, label: "Runtime", sub: "③④ 검증·Identity · VPC A" },
  { icon: DoorOpen, label: "통합 Gateway", sub: "⑤ interceptor·위임토큰" },
  { icon: Wrench, label: "Neptune MCP", sub: "⑥ X-Tier 데이터차등 · VPC B" },
  { icon: Database, label: "Neptune", sub: "개인화 그래프(사설)" },
  // 마지막 홉 — 3rd-party 위키(위키팀 VPC C). 마켓 JWT가 아니라 사용자 명의의
  // Tool IdP 위임 토큰(❶~❺)으로만 도달
  { icon: BookOpen, label: "온톨로 위키", sub: "❶~❺ 위임 · VPC C", wiki: true },
];

// ⑥(MCP 툴이 사용자 신원 확인) = 툴이 실제로 Neptune을 조회해 결과를
// 반환한 시점이므로 Neptune(마지막 노드)까지 밝힌다.
const STEP_CHAIN_INDEX: Record<string, number> = {
  "①": 0, "②": 1, "③": 2, "④": 2, "⑤": 3, "⑥": 5,
};

// 위임 서브체인 (3rd-party 위임 흐름) — ❶~❺가 여기에 매핑.
// 위키(VPC C)와 Neptune(VPC B) 모두 이 골격을 공유한다: Gateway가 Token Vault의
// Tool IdP 위임 토큰(scope files/read 또는 graph/read)을 붙여 내부 MCP를 호출.
const WIKI_CHAIN = [
  { icon: DoorOpen, label: "통합 Gateway", sub: "❶ 볼트 조회" },
  { icon: KeyRound, label: "Token Vault", sub: "❹ 위임 토큰" },
  { icon: Monitor, label: "Tool IdP", sub: "❷❸ SSO 동의·바인딩" },
  { icon: BookOpen, label: "내부 MCP", sub: "❺ 사용자 명의 작업" },
];
const WIKI_STEP_INDEX: Record<string, number> = {
  "❶": 1, "❷": 2, "❸": 2, "❹": 1, "❺": 3,
};

// JWT 클레임 한 줄 설명 — 청중이 JSON을 펼쳤을 때 바로 이해하도록
const CLAIM_HELP: Record<string, string> = {
  sub: "사용자 영구 키 — 계정 생성 시 Cognito가 부여한 불변 UUID. 고객 프로필은 이 값에 바인딩되며(계정 → 고객), 요청이 보낸 customer_id는 무시된다",
  username: "로그인에 사용한 아이디",
  "cognito:groups": "권한 힌트 — 에이전트가 프롬프트에 개인화 컨텍스트를 미리 넣을지만 정함. 실제 tier 진실원은 별도 VPC의 LDAP이고, Gateway interceptor가 LDAP을 조회해 X-Tier로 강제(토큰만으로는 데이터 못 바꿈)",
  client_id: "사용자가 아닌 \"앱\"의 신원 — authorizer의 allowedClients가 검사하는 값",
  scope: "권한 범위 — Tool IdP 위임 토큰은 리소스 서버 scope로 검사 (Neptune 읽기=graph/read, 위키=files/read). 그래프 쓰기 graph/write는 사용자 토큰엔 없고 메모리 파이프라인의 시스템 토큰(client_credentials)만 가짐. Agent IdP 사용자 토큰의 scope(openid profile email)는 인가에 쓰지 않음",
  iss: "발급자 — 어느 리전·어느 사용자 풀이 서명했는지. authorizer가 여기서 공개키를 받아 검증",
  token_use: "access/id 토큰 구분 표시 — 이 체인은 access만 사용 (id 토큰은 client_id가 없어 어차피 거부됨)",
  exp: "만료 시각 (발급 + 1시간) — Runtime은 만료 1분 전부터 거부하므로 SPA가 2분 전에 refresh token으로 자동 갱신",
};

function EventRow({ ev, refToken, count = 1 }: {
  ev: AuthDebugEvent; refToken: string; count?: number;
}) {
  const [open, setOpen] = useState(false);       // 1단: 클레임 JSON
  const [showHelp, setShowHelp] = useState(false); // 2단: 클레임 설명
  // 최초 발급 토큰(refToken)과 같으면 본체 대신 "일치" 배지만 표시
  const sameAsLogin = !!ev.token_preview && ev.token_preview === refToken;
  const isFirstShow = !!ev.token_preview && (ev.step === "①" || ev.step === "②");
  const isWiki = "❶❷❸❹❺".includes(ev.step);
  const warn = ev.level === "warn";
  return (
    <div
      className={cn(
        "rounded-xl border px-3.5 py-2.5 pop-in",
        warn ? "border-amber-200 bg-amber-50/60" : "border-[#e5e8ef] bg-white",
      )}
    >
      <button
        className="flex w-full items-start gap-2.5 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        <span
          className={cn(
            "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
            warn ? "bg-amber-200 text-amber-800"
              : isWiki ? "bg-violet-100 text-violet-700"
              : "bg-indigo-100 text-indigo-700",
          )}
        >
          {ev.step.length <= 1 ? ev.step : "·"}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-[12.5px] font-bold text-slate-800">
              {ev.step.length > 1 && (
                <span className="mr-1.5 text-[10.5px] font-semibold text-slate-400">
                  [{ev.step}]
                </span>
              )}
              {ev.title}
              {ev.tool && (
                <span className="mono ml-1.5 text-[10.5px] font-normal text-sky-600">
                  {ev.tool}
                </span>
              )}
              {ev.args_preview && (
                <span className="mono ml-1 text-[10px] font-normal text-slate-400">
                  {ev.args_preview}
                </span>
              )}
              {count > 1 && (
                <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">
                  ×{count}
                </span>
              )}
            </span>
            <span className="shrink-0 text-[10px] text-slate-400">
              {new Date(ev.ts).toLocaleTimeString("ko-KR", { hour12: false })}
            </span>
          </span>
          <span className="mt-0.5 block text-[11.5px] leading-relaxed text-slate-500">
            {ev.detail}
          </span>
          {ev.token_preview && (
            sameAsLogin && !isFirstShow ? (
              <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10.5px] font-semibold text-emerald-700">
                <KeyRound size={10} className="shrink-0" />
                Bearer 일치 ✓ — 로그인 때 발급된 토큰과 동일 (재발급 없음)
              </span>
            ) : (
              <span className={cn(
                "mono mt-1 flex items-center gap-1 text-[10.5px]",
                sameAsLogin ? "text-emerald-700" : "text-violet-700",
              )}>
                <KeyRound size={10} className="shrink-0" /> {ev.token_preview}
                {!sameAsLogin && refToken && (
                  <span className="font-sans font-semibold"> ← 다른 토큰! (별도 발급)</span>
                )}
              </span>
            )
          )}
        </span>
        {(ev.claims || ev.data || ev.code) && (
          open ? <ChevronUp size={14} className="mt-1 shrink-0 text-slate-300" />
               : <ChevronDown size={14} className="mt-1 shrink-0 text-slate-300" />
        )}
      </button>
      {open && (ev.data || ev.code) && (
        <>
          {/* 이 단계의 데이터 */}
          {ev.data && (
            <div className="mono mt-2 space-y-0.5 rounded-lg bg-slate-50 px-3 py-2 text-[10.5px] leading-relaxed">
              {Object.entries(ev.data).map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <span className="shrink-0 font-bold text-slate-500">{k}</span>
                  <span className="break-all text-slate-700">
                    {typeof v === "string" ? v : JSON.stringify(v)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {/* 이 단계에서 실행되는 실제 코드 */}
          {ev.code && (
            <pre className="mono mt-1.5 overflow-x-auto rounded-lg bg-slate-800 px-3 py-2 text-[10.5px] leading-relaxed text-emerald-200">
              {ev.code}
            </pre>
          )}
        </>
      )}
      {open && ev.claims && (
        <>
          {/* JWT 클레임 원문 */}
          <pre className="mono mt-2 overflow-x-auto rounded-lg bg-slate-50 px-3 py-2 text-[10.5px] leading-relaxed text-slate-600">
            {JSON.stringify(ev.claims, null, 2)}
          </pre>
          {/* 2단 확장: 클레임별 한 줄 설명 */}
          <button
            onClick={() => setShowHelp((h) => !h)}
            className="mt-1.5 flex items-center gap-1 rounded-lg border border-indigo-100 bg-indigo-50/60 px-2.5 py-1 text-[10.5px] font-semibold text-indigo-600 hover:bg-indigo-100"
          >
            {showHelp ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            각 항목이 무슨 뜻인가요?
          </button>
          {showHelp && (
            <div className="mt-1.5 space-y-0.5 rounded-lg bg-indigo-50/50 px-3 py-2">
              {Object.keys(ev.claims)
                .filter((k) => CLAIM_HELP[k])
                .map((k) => (
                  <div key={k} className="flex gap-1.5 text-[10.5px] leading-relaxed">
                    <span className="mono shrink-0 font-bold text-indigo-600">{k}</span>
                    <span className="text-slate-500">
                      {CLAIM_HELP[k]}
                      {k === "exp" && typeof ev.claims![k] === "number" && (
                        <span className="text-slate-400">
                          {" "}· {new Date(ev.claims![k] * 1000).toLocaleString("ko-KR", { hour12: false })} 만료
                        </span>
                      )}
                    </span>
                  </div>
                ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function DebugPanel({
  open,
  onToggle,
}: {
  open: boolean;
  onToggle: () => void;
}) {
  const events = useDebugEvents();
  const listRef = useRef<HTMLDivElement>(null);
  // 로그인 때 발급된 사용자 JWT의 미리보기 — 이후 홉에서 같으면 "일치"로 축약
  const refToken =
    events.find((e) => e.token_preview && (e.step === "①" || e.step === "②"))
      ?.token_preview ??
    events.find((e) => e.token_preview)?.token_preview ?? "";

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [events.length]);

  // 최근 이벤트가 도달한 체인 단계 하이라이트
  const activeIdx = events.length
    ? Math.max(...events.slice(-6).map((e) => STEP_CHAIN_INDEX[e.step] ?? -1))
    : -1;
  // 위키 이벤트가 있으면 서브체인 표시
  const wikiEvents = events.filter((e) => "❶❷❸❹❺".includes(e.step));
  const wikiActiveIdx = wikiEvents.length
    ? Math.max(0, ...wikiEvents.slice(-5).map((e) => WIKI_STEP_INDEX[e.step] ?? -1))
    : -1;

  return (
    <>
      {/* 우측 토글 버튼 */}
      <button
        onClick={onToggle}
        className={cn(
          "fixed right-0 top-1/2 z-50 -translate-y-1/2 rounded-l-xl border border-r-0 px-2 py-4 shadow-md transition",
          open
            ? "border-indigo-600 bg-indigo-600 text-white"
            : "border-[#e5e8ef] bg-white text-slate-500 hover:text-indigo-600",
        )}
        title="인증 흐름 디버그 패널"
      >
        <span className="flex flex-col items-center gap-1.5">
          <Bug size={16} />
          <span className="text-[10px] font-bold [writing-mode:vertical-rl]">
            인증 흐름
          </span>
        </span>
      </button>

      {/* 상단 디버그 패널 — 문서 흐름에 포함되어 내용만큼 늘어나고
          본문을 아래로 민다 (최대 40vh ≈ 화면의 2/5, 그 이상만 내부 스크롤) */}
      {open && (
        <div className="z-10 flex max-h-[40vh] shrink-0 flex-col border-b-2 border-indigo-200 bg-white shadow-md">
          <div className="flex shrink-0 items-center justify-between border-b border-[#eef0f5] px-6 py-2.5">
            <div className="flex items-center gap-2 text-[13px] font-bold text-slate-800">
              <Bug size={14} className="text-indigo-500" />
              인증 흐름 디버그 — 로그인한 사용자의 JWT가 어디로 흘러가는지 실시간으로 보여줍니다
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={clearDebug}
                className="flex items-center gap-1 rounded-lg border border-[#e5e8ef] px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-50"
              >
                <Trash2 size={11} /> 지우기
              </button>
              <button
                onClick={onToggle}
                className="rounded-lg border border-[#e5e8ef] p-1 text-slate-400 hover:bg-slate-50"
              >
                <X size={13} />
              </button>
            </div>
          </div>

          {/* 체인 다이어그램 */}
          <div className="flex shrink-0 items-center gap-1 overflow-x-auto px-6 py-3">
            {CHAIN.map((n, i) => {
              const Icon = n.icon;
              // 위키 노드는 마켓 JWT 체인이 아니라 위임 이벤트(❶~❺)가 있을 때 켜진다
              const active = n.wiki ? wikiEvents.length > 0 : i <= activeIdx;
              const tone = n.wiki
                ? { box: "border-violet-300 bg-violet-50", icon: "text-violet-600", text: "text-violet-700" }
                : { box: "border-indigo-300 bg-indigo-50", icon: "text-indigo-600", text: "text-indigo-700" };
              return (
                <div key={i} className="flex items-center gap-1">
                  {n.wiki && (
                    <span className="mx-1 shrink-0 text-[9px] font-bold leading-tight text-violet-500">
                      다른<br />토큰
                    </span>
                  )}
                  <div
                    className={cn(
                      "flex min-w-[92px] flex-col items-center rounded-xl border px-2.5 py-1.5 transition",
                      n.wiki && "border-dashed",
                      active ? tone.box : "border-[#eef0f5] bg-white",
                    )}
                  >
                    <Icon size={15} className={active ? tone.icon : "text-slate-300"} />
                    <span className={cn("text-[11px] font-bold", active ? tone.text : "text-slate-400")}>
                      {n.label}
                    </span>
                    <span className="text-[9.5px] text-slate-400">{n.sub}</span>
                  </div>
                  {i < CHAIN.length - 1 && (
                    <ArrowRight
                      size={13}
                      className={cn(
                        CHAIN[i + 1].wiki
                          ? (wikiEvents.length > 0 ? "text-violet-400" : "text-slate-200")
                          : (i < activeIdx ? "text-indigo-400" : "text-slate-200"),
                      )}
                    />
                  )}
                </div>
              );
            })}
            <div className="ml-3 shrink-0 rounded-lg bg-emerald-50 px-2.5 py-1.5 text-[10px] leading-tight text-emerald-700">
              사용자 JWT는 Gateway까지 —
              <br />백엔드엔 Tool IdP 위임 토큰 🔑
            </div>
          </div>

          {/* 위키 OBO 서브체인 — ❶~❺ 이벤트가 있을 때만 */}
          {wikiEvents.length > 0 && (
            <div className="flex items-center gap-1 overflow-x-auto border-t border-violet-100 bg-violet-50/40 px-6 py-2.5">
              <span className="mr-2 shrink-0 text-[10px] font-bold text-violet-600">
                Tool IdP 위임
                <br />(위키·Neptune)
              </span>
              {WIKI_CHAIN.map((n, i) => {
                const Icon = n.icon;
                const active = i <= wikiActiveIdx;
                return (
                  <div key={i} className="flex items-center gap-1">
                    <div className={cn(
                      "flex min-w-[92px] flex-col items-center rounded-xl border px-2.5 py-1 transition",
                      active ? "border-violet-300 bg-violet-100/70" : "border-[#eef0f5] bg-white",
                    )}>
                      <Icon size={13} className={active ? "text-violet-600" : "text-slate-300"} />
                      <span className={cn("text-[10.5px] font-bold", active ? "text-violet-700" : "text-slate-400")}>
                        {n.label}
                      </span>
                      <span className="text-[9px] text-slate-400">{n.sub}</span>
                    </div>
                    {i < WIKI_CHAIN.length - 1 && (
                      <ArrowRight size={12} className={i < wikiActiveIdx ? "text-violet-400" : "text-slate-200"} />
                    )}
                  </div>
                );
              })}
              <div className="ml-3 shrink-0 rounded-lg bg-violet-100/60 px-2.5 py-1 text-[10px] leading-tight text-violet-700">
                별도 발급자의 2번째 토큰
                <br />(마켓 SSO로만 로그인) 🔐
              </div>
            </div>
          )}

          {/* 테스트 도구 — 계정 인증 초기화(revoke). 동의 리셋·재로그인 테스트용 */}
          <div className="shrink-0 border-t border-[#eef0f5] px-6 py-2">
            <RevokeTools compact />
          </div>

          {/* 이벤트 타임라인 — 패널이 70vh에 닿기 전까지는 스크롤 없이 확장 */}
          <div ref={listRef} className="min-h-0 flex-1 space-y-1.5 overflow-y-auto bg-[#fafbfd] px-6 py-3">
            {events.length === 0 && (
              <div className="py-4 text-center text-[12px] text-slate-400">
                아직 이벤트가 없습니다 — 쇼핑 상담에서 메시지를 보내면 인증
                흐름이 여기에 실시간으로 기록됩니다.
              </div>
            )}
            {(() => {
              // 완전히 동일한 이벤트가 연속되면 한 카드로 접고 ×N 표시
              const grouped: { ev: (typeof events)[number]; count: number }[] = [];
              for (const ev of events) {
                const last = grouped[grouped.length - 1];
                if (last && last.ev.step === ev.step && last.ev.title === ev.title
                    && last.ev.tool === ev.tool
                    && last.ev.args_preview === ev.args_preview
                    && JSON.stringify(last.ev.data) === JSON.stringify(ev.data)) {
                  last.count += 1;
                } else {
                  grouped.push({ ev, count: 1 });
                }
              }
              // 첫 위키 이벤트 앞에 구분선 — 여기부터 마켓 JWT가 아닌 위임 토큰 흐름
              const firstWiki = grouped.findIndex((g) => "❶❷❸❹❺".includes(g.ev.step));
              return grouped.map(({ ev, count }, idx) => (
                <div key={ev.id} className="space-y-1.5">
                  {idx === firstWiki && (
                    <div className="flex items-center gap-2 pt-1">
                      <span className="h-px flex-1 bg-violet-200" />
                      <span className="rounded-full border border-violet-200 bg-violet-50 px-2.5 py-0.5 text-[10.5px] font-bold text-violet-700">
                        ▼ 여기부터 Tool IdP 위임 — 마켓 JWT는 백엔드에 못 쓰고, 사용자 명의의 위임 토큰이 필요 (위키·Neptune 공통)
                      </span>
                      <span className="h-px flex-1 bg-violet-200" />
                    </div>
                  )}
                  <EventRow ev={ev} refToken={refToken} count={count} />
                </div>
              ));
            })()}
          </div>
        </div>
      )}
    </>
  );
}
