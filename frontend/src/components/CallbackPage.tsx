import { useEffect, useRef, useState } from "react";
import { BookOpenCheck, Loader2, XCircle } from "lucide-react";
import { ensureFreshToken, getToken } from "@/lib/auth";
import { broadcastDebug } from "@/lib/debug";

/** 위키(가상 3rd-party) 3LO 동의 후 복귀 페이지 — /oauth/callback
 *
 * 위키 IdP 로그인 → AgentCore Identity 콜백 → 이 페이지로 session_id가
 * 도착한다. 여기서 BFF를 통해 CompleteResourceTokenAuth를 호출해
 * "이 동의 = 마켓 IdP의 현재 사용자"를 명시적으로 바인딩한다(❸).
 */
export default function CallbackPage() {
  const [state, setState] = useState<"working" | "done" | "error">("working");
  const [detail, setDetail] = useState("");
  // React StrictMode는 effect를 두 번 실행한다 — 두 번째 CompleteResourceTokenAuth는
  // "Invalid or expired session"으로 실패하므로 ref로 1회만.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const qs = new URLSearchParams(window.location.search);
    const sessionId = qs.get("session_id") || qs.get("sessionId") || "";
    if (!sessionId) {
      setState("error");
      setDetail("session_id 파라미터가 없습니다.");
      return;
    }
    ensureFreshToken()
      .then((tok) =>
        fetch("/api/oauth/complete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${tok || getToken()}`,
          },
          body: JSON.stringify({ session_id: sessionId }),
        }),
      )
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        setState("done");
        setDetail(`위키 동의가 '${d.boundUser}' 계정에 연결되었습니다.`);
        broadcastDebug({
          step: "❸",
          title: "동의 완료 — 사용자 바인딩 (CompleteResourceTokenAuth)",
          detail:
            "여기가 '같은 사용자 매칭' 로직입니다. 토큰이 2개(우리 IdP의 " +
            "사용자 토큰 + 위키 IdP의 위임 토큰)가 되는 순간이라, 위키에서 " +
            `받은 동의 세션을 마켓 IdP 사용자(${d.boundUser})의 토큰으로 명시적 ` +
            "으로 묶었습니다. 이 바인딩 덕분에 볼트가 사용자별로 격리됩니다.",
          tool: "wiki",
          data: {
            "동의 세션(sessionUri)": sessionId.slice(0, 44) + "…",
            "바인딩된 사용자": d.boundUser,
            "바인딩 근거": "userIdentifier = { userToken: <마켓 IdP JWT> }",
          },
          code:
            "# backend/bff/main.py — 같은-사용자 매칭의 실제 코드\n" +
            "bedrock_agentcore.complete_resource_token_auth(\n" +
            "    sessionUri=session_id,             # 위키 동의 세션\n" +
            "    userIdentifier={'userToken': jwt}) # 마켓 사용자 토큰",
        });
        // 부모(채팅) 탭에 완료를 알려 동의 카드를 "연결 완료"로 바꾼다 — 카드의 URL은 1회용이라
        // 다시 누르면 AgentCore가 "Invalid request"를 낸다
        try { window.opener?.postMessage({ type: "ontolo-consent-done", boundUser: d.boundUser }, window.location.origin); } catch { /* noop */ }
        setTimeout(() => window.close(), 2500);
      })
      .catch((e) => {
        setState("error");
        setDetail(String(e.message || e));
      });
  }, []);

  return (
    <div className="flex h-screen items-center justify-center bg-[#f7f8fb]">
      <div className="panel w-[420px] p-8 text-center">
        {state === "working" && (
          <>
            <Loader2 size={28} className="mx-auto animate-spin text-indigo-500" />
            <div className="mt-3 text-[14px] font-bold text-slate-800">
              동의를 사용자 계정에 연결하는 중…
            </div>
          </>
        )}
        {state === "done" && (
          <>
            <BookOpenCheck size={30} className="mx-auto text-emerald-500" />
            <div className="mt-3 text-[14px] font-bold text-slate-800">
              온톨로 위키 연결 완료
            </div>
            <p className="mt-1.5 text-[12.5px] leading-relaxed text-slate-500">
              {detail}
              <br />
              refresh token이 Token Vault에 저장되어 다음부터는 로그인 없이
              에이전트가 대신 접근합니다. 이 창은 곧 닫힙니다 — 채팅 화면으로
              돌아가세요.
            </p>
          </>
        )}
        {state === "error" && (
          <>
            <XCircle size={30} className="mx-auto text-rose-500" />
            <div className="mt-3 text-[14px] font-bold text-slate-800">연결 실패</div>
            <p className="mt-1.5 text-[12.5px] text-rose-600">{detail}</p>
          </>
        )}
      </div>
    </div>
  );
}
