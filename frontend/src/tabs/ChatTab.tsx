import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import { Send, Loader2, Wrench, BookOpenText } from "lucide-react";
import { cn } from "@/lib/cn";
import { streamSSE, type Customer } from "@/lib/api";
import { ensureFreshToken, getToken, logout } from "@/lib/auth";
import { pushDebug } from "@/lib/debug";
import { Badge } from "@/components/Badge";

interface Msg {
  role: "user" | "assistant";
  text: string;
  streaming?: boolean;
  toolEvents?: { tool: string; args: any }[];
  memories?: { namespace: string; text: string }[];
  consent?: { url: string; service: string; message: string; done?: boolean };
}

const SUGGESTIONS: Record<string, string[]> = {
  "cust-yuna": [
    "요즘 아침에 먹을만한 거 추천해줘",
    "손님 오는데 디저트랑 커피 좀 골라줘",
    "나 요즘 필라테스 시작해서 단백질 챙기려고 해",
  ],
  "cust-minjun": [
    "이번 주말 캠핑 가는데 뭐 챙기면 좋을까?",
    "치즈(고양이) 간식 떨어졌어",
    "나 요즘 혼술에 빠졌어, 안주 추천해줘",
  ],
  "cust-seoyeon": [
    "아기 목욕용품 추천해줘",
    "주방세제 다 떨어졌는데 리필로",
    "우리 애가 요즘 블록놀이를 좋아해",
  ],
};

export default function ChatTab({
  cid,
  customer,
  onDataChanged,
}: {
  cid: string;
  customer?: Customer;
  onDataChanged: () => void;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // 세션 ID는 로그인 단위로 유지 — 새로고침해도 같은 Runtime 세션(microVM)을 이어 쓴다.
  // (매번 새 세션이면 VPC 모드 런타임이 다시 떠 첫 응답 ~5초. 로그아웃 시 auth.ts가 지운다)
  const [sessionId] = useState(() => {
    const key = "ontolo-chat-session";
    const saved = localStorage.getItem(key);
    if (saved) return saved;
    const fresh = `s-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(key, fresh);
    return fresh;
  });
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMsgs([]);
  }, [cid]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [msgs]);

  // 동의 팝업(/oauth/callback)이 CompleteResourceTokenAuth 성공 후 보내는 신호 → 카드를 "연결 완료"로.
  // 카드의 동의 URL은 1회용이라 성공 뒤 다시 누르면 "Invalid request"가 난다.
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (e.origin !== window.location.origin || e.data?.type !== "ontolo-consent-done") return;
      setMsgs((m) => m.map((x) => (x.consent ? { ...x, consent: { ...x.consent, done: true } } : x)));
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  const send = (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setInput("");
    setMsgs((m) => [
      ...m,
      { role: "user", text },
      { role: "assistant", text: "", streaming: true },
    ]);

    // 호출 직전 토큰 갱신 — Runtime은 만료 1분 전부터 JWT를 거부한다("Ineffectual token")
    ensureFreshToken().then((tok) =>
    streamSSE("/api/chat", { query: text, customer_id: cid, session_id: sessionId }, (ev, data) => {
      if (ev === "debug") {
        pushDebug({
          step: data.step ?? "·",
          title: data.title ?? "",
          detail: data.detail ?? "",
          token_preview: data.token_preview,
          claims: data.claims ?? null,
          data: data.data ?? null,
          code: data.code,
          tool: data.tool,
          args_preview: data.args_preview,
          level: data.level,
        });
      } else if (ev === "auth_url") {
        // 위키(3rd-party) 동의 요청 — 스트리밍 말풍선 앞에 동의 카드 삽입
        setMsgs((m) => {
          const out = [...m];
          const consentMsg: Msg = {
            role: "assistant", text: "",
            consent: { url: data.url, service: data.service ?? "외부 서비스",
                       message: data.message ?? "" },
          };
          out.splice(out.length - 1, 0, consentMsg);
          return out;
        });
      } else if (ev === "chunk") {
        setMsgs((m) => {
          const out = [...m];
          const last = out[out.length - 1];
          out[out.length - 1] = { ...last, text: last.text + (data.text ?? "") };
          return out;
        });
      } else if (ev === "final") {
        setMsgs((m) => {
          const out = [...m];
          const last = out[out.length - 1];
          out[out.length - 1] = {
            ...last,
            text: data.answer || last.text,
            streaming: false,
            toolEvents: data.tool_events ?? [],
            memories: data.memories_used ?? [],
          };
          return out;
        });
      } else if (ev === "error") {
        // 세션 revoke/만료(BFF 401 또는 에이전트의 "세션 종료" 오류) → 로그인 화면으로
        const revoked = data.status === 401 || /세션이 종료|revoke/i.test(String(data.message ?? ""));
        setMsgs((m) => {
          const out = [...m];
          out[out.length - 1] = {
            role: "assistant",
            text: revoked
              ? "🔒 로그인 세션이 종료되었습니다(revoke). 다시 로그인해 주세요 — 잠시 후 로그인 화면으로 이동합니다."
              : `⚠️ 오류: ${data.message}`,
            streaming: false,
          };
          return out;
        });
        setBusy(false);
        // 로컬만 지우면 IdP(hosted UI) 쿠키가 남아 다음 "SSO 로그인"이 비번 없이 통과한다 → IdP /logout까지
        if (revoked) setTimeout(() => void logout(), 2500);
      } else if (ev === "done") {
        setBusy(false);
        onDataChanged();
      }
    }, { Authorization: `Bearer ${tok || getToken()}` }),
    ).catch((e: Error) => {
      setMsgs((m) => {
        const out = [...m];
        out[out.length - 1] = { role: "assistant", text: `⚠️ ${e.message}`, streaming: false };
        return out;
      });
      setBusy(false);
    });
  };

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-[#e5e8ef] bg-white px-8 py-4">
        <h1 className="text-[17px] font-bold text-slate-900">쇼핑 상담</h1>
        <p className="text-[12.5px] text-slate-400">
          {customer ? `${customer.name} · ${customer.bio}` : "연결된 고객 프로필 없음 — 개인화 없이 상담"}
        </p>
      </header>

      <div className="flex-1 overflow-y-auto px-8 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          {msgs.length === 0 && (
            <div className="panel mx-auto mt-16 max-w-lg p-6 text-center">
              <div className="text-[15px] font-semibold text-slate-700">
                무엇을 찾아드릴까요?
              </div>
              <p className="mt-1 text-[12.5px] text-slate-400">
                대화 내용은 AgentCore Memory에 쌓이고, 비동기 추출된 취향이
                Neptune 그래프로 흘러가 다음 추천에 반영됩니다.
              </p>
              <div className="mt-4 flex flex-col gap-2">
                {(SUGGESTIONS[cid] ?? []).map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="rounded-xl border border-indigo-100 bg-indigo-50/50 px-4 py-2.5 text-[13px] text-indigo-700 transition hover:bg-indigo-100"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {msgs.map((m, i) => (
            <div key={i} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[85%] rounded-2xl px-4 py-3 text-[13.5px] leading-relaxed pop-in",
                  m.role === "user"
                    ? "bg-indigo-600 text-white"
                    : "panel text-slate-700",
                )}
              >
                {m.role === "assistant" && m.consent ? (
                  <div className="min-w-[320px]">
                    <div className="flex items-center gap-1.5 text-[12px] font-bold text-violet-700">
                      🔗 {m.consent.service} 연결 필요 (최초 1회)
                    </div>
                    <p className="mt-1 text-[12px] leading-relaxed text-slate-500">
                      {m.consent.message} — {m.consent.service}는 마켓 IdP와 SSO로 연동되어
                      있어 <b>마켓 계정으로 로그인</b>하게 됩니다 (별도 비밀번호 없음).
                      위키·데이터팀은 서로 다른 리소스(scope)라 각각 한 번씩 동의합니다.
                      동의하면 위임 토큰이 Token Vault에 저장되어 다음부터는 이 과정이 생략됩니다.
                    </p>
                    {m.consent.done ? (
                      <div className="mt-2 inline-flex items-center gap-1.5 rounded-xl bg-emerald-50 px-3 py-1.5 text-[12px] font-semibold text-emerald-700">
                        ✓ 연결 완료 — 위임 토큰이 Token Vault에 저장됨. 에이전트가 이어서 진행합니다
                      </div>
                    ) : (
                      <button
                        onClick={() => window.open(m.consent!.url, "_blank",
                          "width=520,height=680")}
                        className="mt-2 rounded-xl bg-violet-600 px-4 py-2 text-[12.5px] font-semibold text-white hover:bg-violet-700"
                      >
                        마켓 계정으로 SSO 로그인하고 위임하기
                      </button>
                    )}
                  </div>
                ) : m.role === "assistant" ? (
                  <>
                    {m.memories && m.memories.length > 0 && (
                      <div className="mb-2 rounded-lg bg-amber-50 px-3 py-2 text-[11.5px] text-amber-700">
                        <div className="mb-1 flex items-center gap-1 font-semibold">
                          <BookOpenText size={12} /> 장기 기억 {m.memories.length}건 사용
                        </div>
                        {m.memories.slice(0, 3).map((mem, j) => (
                          <div key={j} className="truncate">· {mem.text}</div>
                        ))}
                      </div>
                    )}
                    <div className="md">
                      <Markdown>{m.text || (m.streaming ? "..." : "")}</Markdown>
                    </div>
                    {m.streaming && (
                      <Loader2 size={14} className="mt-1 animate-spin text-indigo-400" />
                    )}
                    {m.toolEvents && m.toolEvents.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5 border-t border-slate-100 pt-2">
                        {m.toolEvents.map((t, j) => (
                          <Badge key={j} tone="sky">
                            <Wrench size={10} /> {t.tool}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </>
                ) : (
                  m.text
                )}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>

      <footer className="border-t border-[#e5e8ef] bg-white px-8 py-4">
        <form
          className="mx-auto flex max-w-3xl gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            send(input);
          }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="메시지를 입력하세요…"
            className="flex-1 rounded-xl border border-[#e5e8ef] bg-[#f7f8fb] px-4 py-2.5 text-[13.5px] outline-none transition focus:border-indigo-300 focus:bg-white"
          />
          <button
            type="submit"
            disabled={busy || !input.trim()}
            className="flex items-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2.5 text-[13px] font-semibold text-white transition hover:bg-indigo-700 disabled:opacity-40"
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
            보내기
          </button>
        </form>
      </footer>
    </div>
  );
}
