import { useEffect, useState } from "react";
import { Loader2, LogIn, ShieldCheck, ShoppingBasket } from "lucide-react";
import { authInfo, startLogin } from "@/lib/auth";
import RevokeTools from "@/components/RevokeTools";

interface DemoUser {
  username: string;
  tier: string;
  label: string;
}

/** 로그인 게이트 — Agent IdP hosted UI로 리다이렉트(SSO).
 *  hosted UI 리다이렉트 로그인이어야 IdP 브라우저 세션이 남아, 위키 뷰어 등 다른 앱이
 *  페더레이션 때 로그인 폼 없이 같은 사용자를 상속한다.
 */
export default function LoginGate() {
  const [users, setUsers] = useState<DemoUser[]>([]);
  const [hosted, setHosted] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    authInfo()
      .then((d) => {
        if (d.enabled) {
          setUsers(d.users || []);
          setHosted(d.hostedUiBase || "");
        }
      })
      .catch(() => {});
  }, []);

  const go = async () => {
    setBusy(true);
    setError("");
    try {
      await startLogin(); // 페이지가 IdP로 떠난다
    } catch (err: any) {
      setError(String(err.message || err));
      setBusy(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-[#f7f8fb]">
      <div className="panel w-96 p-8">
        <div className="flex items-center gap-2.5">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600 text-white">
            <ShoppingBasket size={20} />
          </div>
          <div>
            <div className="text-[16px] font-bold text-slate-900">온톨로 마켓</div>
            <div className="text-[11.5px] text-slate-400">
              AgentCore × Neptune 초개인화 데모
            </div>
          </div>
        </div>

        <div className="mt-5 rounded-xl border border-indigo-100 bg-indigo-50/60 px-4 py-3 text-[12px] leading-relaxed text-indigo-700">
          <div className="mb-1 flex items-center gap-1.5 font-bold">
            <ShieldCheck size={14} /> SSO 로그인 (Agent IdP)
          </div>
          회사 IdP의 로그인 화면으로 이동합니다. 로그인하면 <b>IdP 세션</b>이 생겨
          이 마켓뿐 아니라 온톨로 위키 등 연동된 시스템에도 <b>같은 사용자</b>로
          자동 인식됩니다 — 다른 사람이 되려면 로그아웃해야 합니다.
        </div>

        <button
          type="button"
          onClick={go}
          disabled={busy || !hosted}
          className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 py-2.5 text-[13.5px] font-semibold text-white transition hover:bg-indigo-700 disabled:opacity-40"
        >
          {busy ? <Loader2 size={15} className="animate-spin" /> : <LogIn size={15} />}
          회사 계정으로 로그인 (SSO)
        </button>
        {error && (
          <div className="mt-2 rounded-lg bg-rose-50 px-3 py-2 text-[12px] text-rose-600">
            {error}
          </div>
        )}

        {users.length > 0 && (
          <div className="mt-4 rounded-xl border border-[#e5e8ef] bg-slate-50/60 px-3.5 py-2.5">
            <div className="mb-1 text-[11px] font-semibold text-slate-500">
              데모 계정 (IdP 화면에서 입력)
            </div>
            <div className="flex flex-col gap-0.5 text-[12px] text-slate-600">
              {users.map((u) => (
                <div key={u.username}>
                  {u.tier === "premium" ? "⭐" : "👤"} <b>{u.username}</b>
                  <span className="text-slate-400"> — {u.label.replace(/^\S+\s*/, "")}</span>
                </div>
              ))}
              <div className="pt-1 text-[11px] text-slate-400">계정마다 고객 프로필이 고정됩니다 — 다른 고객 데이터는 볼 수 없습니다</div>
            </div>
          </div>
        )}

        <div className="mt-4 text-center text-[11px] text-slate-400">
          Cognito hosted UI · Authorization Code + PKCE
        </div>

        <div className="mt-4">
          <RevokeTools />
        </div>
      </div>
    </div>
  );
}
