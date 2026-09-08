import { useEffect, useState } from "react";
import { ExternalLink, Loader2, RotateCcw } from "lucide-react";
import { cn } from "@/lib/cn";
import { getJSON } from "@/lib/api";
import { useAuth, authInfo, logout } from "@/lib/auth";

// 데모 테스트 도구 — 계정 인증 초기화(revoke).
// 서버(BFF)가 ① Agent IdP refresh 무효 + ② Tool IdP JIT 사용자 sign-out(위임 토큰
// 무효 → 재동의)을 하고, 여기서는 ③ 브라우저 로컬 세션을 지운다(현재 사용자일 때).
// access JWT는 만료까지 유효하므로 ③ 없이는 계속 들어갈 수 있다 — 반드시 같이.
const WIKI_BASE_DEFAULT = "http://localhost:5182";

interface RevokeResult {
  username: string;
  agentIdp: string | null;
  toolIdp: string | null;
}

export default function RevokeTools({ compact = false }: { compact?: boolean }) {
  const auth = useAuth();
  const [users, setUsers] = useState<string[]>([]);
  const [busy, setBusy] = useState<string>("");
  const [results, setResults] = useState<RevokeResult[]>([]);
  const [error, setError] = useState("");
  // 위키 뷰어 로그아웃 URL — BFF auth info의 wikiBase에서 파생
  const [wikiLogout, setWikiLogout] = useState(WIKI_BASE_DEFAULT + "/logout");

  useEffect(() => {
    getJSON<{ users: string[] }>("/api/admin/users")
      .then((d) => setUsers(d.users || []))
      .catch(() => {});
    authInfo()
      .then((info) => setWikiLogout((info.wikiBase ?? WIKI_BASE_DEFAULT) + "/logout"))
      .catch(() => {});
  }, []);

  const revoke = async (target: string) => {
    setBusy(target);
    setError("");
    setResults([]);
    try {
      const r = await fetch("/api/admin/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: target }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      setResults(d.results || []);
      // 현재 로그인 사용자가 포함되면 IdP /logout까지 태운다 — GlobalSignOut은 토큰만 무효화하고
      // hosted UI 브라우저 쿠키는 남기므로, 로컬만 지우면 다음 SSO 로그인이 비번 없이 통과한다.
      const me = auth?.username;
      if (me && (target === "*" || target === me)) {
        setTimeout(() => void logout(), 1200);
      }
    } catch (e: any) {
      setError(String(e.message || e));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className={cn("rounded-xl border border-dashed border-rose-200 bg-rose-50/40", compact ? "px-3 py-2" : "px-4 py-3")}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-[11px] font-bold text-rose-700">
          <RotateCcw size={12} /> 테스트 도구 · 계정 인증 초기화
        </div>
        <a
          href={wikiLogout}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-1 text-[10.5px] font-semibold text-rose-600 hover:underline"
          title="위키 뷰어는 별도 origin이라 여기서 못 지움 — 뷰어의 로그아웃을 연다"
        >
          위키 뷰어 로그아웃 <ExternalLink size={10} />
        </a>
      </div>
      {!compact && (
        <div className="mt-1 text-[10.5px] leading-relaxed text-rose-600/80">
          Agent IdP GlobalSignOut + <b>revoke 목록(S3) 기록</b> → BFF·Gateway interceptor·에이전트가 그 사용자의 기존 토큰을
          <b>즉시</b> 거부(재로그인 필요). Tool IdP 위임 refresh도 무효(위키·Neptune <b>재동의 필요</b>). 현재 사용자면 브라우저 세션 삭제
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {users.map((u) => (
          <button
            key={u}
            type="button"
            disabled={!!busy}
            onClick={() => revoke(u)}
            className={cn(
              "rounded-lg border px-2.5 py-1 text-[11px] font-semibold transition disabled:opacity-40",
              auth?.username === u
                ? "border-rose-300 bg-white text-rose-700 hover:bg-rose-100"
                : "border-rose-200 bg-white text-slate-600 hover:bg-rose-100",
            )}
          >
            {busy === u ? <Loader2 size={11} className="inline animate-spin" /> : null} {u}
            {auth?.username === u && <span className="ml-1 text-[9px] text-rose-400">(나)</span>}
          </button>
        ))}
        <button
          type="button"
          disabled={!!busy || users.length === 0}
          onClick={() => revoke("*")}
          className="rounded-lg bg-rose-600 px-2.5 py-1 text-[11px] font-bold text-white transition hover:bg-rose-700 disabled:opacity-40"
        >
          {busy === "*" ? <Loader2 size={11} className="inline animate-spin" /> : null} 전체 초기화
        </button>
      </div>
      {error && <div className="mt-2 text-[11px] text-rose-700">{error}</div>}
      {results.length > 0 && (
        <div className="mono mt-2 space-y-0.5 rounded-lg bg-white/80 px-2.5 py-1.5 text-[10px] leading-relaxed text-slate-600">
          {results.map((r) => (
            <div key={r.username}>
              <b className="text-slate-800">{r.username}</b> · A: {r.agentIdp} · B: {r.toolIdp ?? "-"}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
