import { useEffect, useRef, useState } from "react";
import { Loader2, ShieldCheck, XCircle } from "lucide-react";
import { finishLogin, startLogin } from "@/lib/auth";
import { pushDebug } from "@/lib/debug";

/** ① Agent IdP hosted UI 로그인 복귀 페이지 — /auth/callback?code=…&state=…
 *
 *  BFF를 통해 code를 토큰으로 교환하고(PKCE), 디버그 패널에 ① 이벤트를 남긴 뒤
 *  메인 화면으로 돌아간다. 이 시점에 IdP 도메인에는 브라우저 세션이 생겼다 —
 *  위키 뷰어가 페더레이션할 때 폼 없이 같은 사용자를 상속하는 근거.
 */
export default function AuthCallbackPage() {
  const [error, setError] = useState("");
  // React StrictMode는 effect를 두 번 실행한다 — 첫 실행이 PKCE 상태(sessionStorage)를
  // 소비하므로 ref로 1회만.
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const qs = new URLSearchParams(window.location.search);
    const code = qs.get("code") || "";
    const st = qs.get("state") || "";
    if (qs.get("error")) {
      setError(`${qs.get("error")}: ${qs.get("error_description") || ""}`);
      return;
    }
    if (!code) {
      setError("code 파라미터가 없습니다.");
      return;
    }
    finishLogin(code, st)
      .then((s) => {
        let header: any = {};
        try {
          header = JSON.parse(atob(s.token.split(".")[0]));
        } catch { /* 표시용 */ }
        pushDebug({
          step: "①",
          title: "사용자 로그인 성공 (Agent IdP hosted UI · SSO 세션 생성)",
          detail:
            "사용자가 IdP의 hosted UI에서 인증하고 access token(JWT)을 받았습니다. " +
            "이 토큰이 BFF → Runtime → 통합 Gateway까지 흐릅니다. 동시에 IdP 도메인에 " +
            "브라우저 세션이 생겨, 위키 뷰어 등 다른 앱은 로그인 폼 없이 같은 사용자를 " +
            "상속합니다(진짜 SSO) — 다른 사람이 되려면 IdP 로그아웃이 필요합니다.",
          token_preview: s.tokenPreview,
          claims: s.claims,
          data: {
            "인증 방식": "hosted UI Authorization Code + PKCE (S256)",
            "토큰 헤더 kid": header.kid ?? "?",
            "서명 알고리즘": header.alg ?? "?",
            "만료": "발급 + 1시간 (refresh token으로 자동 갱신)",
          },
          code:
            "# 브라우저 → Agent IdP hosted UI (세션 쿠키 생성)\n" +
            "GET https://<hosted-ui-domain>/oauth2/authorize\n" +
            "    ?client_id=…&response_type=code&scope=openid profile email\n" +
            `    &redirect_uri=${window.location.origin}/auth/callback&code_challenge=…\n` +
            "# 복귀 후 BFF가 code → 토큰 교환\n" +
            "POST /oauth2/token grant_type=authorization_code&code=…&code_verifier=…",
        });
        window.history.replaceState({}, "", "/");
        window.location.assign("/");
      })
      .catch((e) => setError(String(e.message || e)));
  }, []);

  return (
    <div className="flex h-screen items-center justify-center bg-[#f7f8fb]">
      <div className="panel w-[420px] p-8 text-center">
        {!error ? (
          <>
            <Loader2 size={28} className="mx-auto animate-spin text-indigo-500" />
            <div className="mt-3 text-[14px] font-bold text-slate-800">
              로그인 처리 중 — 토큰 교환
            </div>
            <p className="mt-1.5 flex items-center justify-center gap-1 text-[12px] text-slate-500">
              <ShieldCheck size={13} className="text-emerald-500" /> Agent IdP 세션이 만들어졌습니다
            </p>
          </>
        ) : (
          <>
            <XCircle size={30} className="mx-auto text-rose-500" />
            <div className="mt-3 text-[14px] font-bold text-slate-800">로그인 실패</div>
            <p className="mt-1.5 text-[12.5px] text-rose-600">{error}</p>
            <button
              onClick={() => startLogin()}
              className="mt-4 rounded-xl bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-700"
            >
              다시 로그인
            </button>
          </>
        )}
      </div>
    </div>
  );
}
