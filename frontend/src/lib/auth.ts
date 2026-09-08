// OBO 체인 인증 — Agent IdP(Cognito) hosted UI 로그인으로 받은 access token을 보관하고,
// 모든 채팅 호출에 Authorization: Bearer로 실어 보낸다.
// (이 토큰이 BFF → Runtime → 통합 Gateway까지 흐르고, 백엔드 MCP엔 위임 토큰으로 스왑)
// hosted UI 리다이렉트 로그인이어야 IdP 도메인에 브라우저 세션이 남아, 위키 뷰어 등
// 다른 앱이 페더레이션 때 로그인 폼 없이 같은 사용자를 상속한다(SSO).
import { useSyncExternalStore } from "react";
import { getJSON } from "@/lib/api";

export interface AuthState {
  token: string;
  refreshToken?: string;
  claims: Record<string, any>;
  tokenPreview: string;
  username: string;
}

interface AuthInfo {
  enabled: boolean;
  clientId: string;
  hostedUiBase: string;
  callbackUrl: string;
  ssoLogoutUrl: string;
  oauthScopes: string[];
  wikiBase?: string; // 위키 뷰어 origin (기본 http://localhost:5182)
  users?: { username: string; tier: string; label: string }[];
}

// AgentCore Runtime의 JWT authorizer는 만료 60초 전부터 토큰을 거부한다
// ("Ineffectual token, will expire within the next minute").
// 그래서 exp가 이 여유(초) 안이면 '곧 만료'로 보고 먼저 갱신한다.
const REFRESH_MARGIN_S = 120;
const PKCE_KEY = "ontolo-pkce"; // sessionStorage: { verifier, state } — 리다이렉트 왕복 동안만

let state: AuthState | null = loadSaved();
const listeners = new Set<() => void>();

function secondsLeft(s: AuthState | null): number {
  const exp = s?.claims?.exp;
  return typeof exp === "number" ? exp - Date.now() / 1000 : Infinity;
}

function loadSaved(): AuthState | null {
  try {
    const raw = localStorage.getItem("ontolo-auth");
    if (!raw) return null;
    const s = JSON.parse(raw) as AuthState;
    // 곧 만료인데 갱신 수단(refresh token)도 없으면 버린다 → 다시 로그인
    if (secondsLeft(s) < REFRESH_MARGIN_S && !s.refreshToken) return null;
    return s;
  } catch {
    return null;
  }
}

function set(next: AuthState | null) {
  state = next;
  if (next) localStorage.setItem("ontolo-auth", JSON.stringify(next));
  else {
    localStorage.removeItem("ontolo-auth");
    localStorage.removeItem("ontolo-chat-session"); // 로그인 단위 Runtime 세션도 함께 종료
  }
  listeners.forEach((l) => l());
}

export function useAuth(): AuthState | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => state,
  );
}

export function getToken(): string {
  return state?.token ?? "";
}

let infoCache: Promise<AuthInfo> | null = null;
export function authInfo(): Promise<AuthInfo> {
  if (!infoCache) infoCache = getJSON<AuthInfo>("/api/auth/info");
  return infoCache;
}

let refreshing: Promise<string> | null = null;

/** 호출 직전에 부른다. 토큰이 2분 이내 만료면 refresh token으로 갱신해서
 *  Runtime의 '만료 1분 전 거부'에 걸리지 않게 한다. 갱신 실패 시 로그아웃. */
export async function ensureFreshToken(): Promise<string> {
  if (!state) return "";
  if (secondsLeft(state) >= REFRESH_MARGIN_S) return state.token;
  if (!state.refreshToken) {
    set(null);
    throw new Error("로그인이 만료되었습니다 — 다시 로그인해 주세요");
  }
  if (!refreshing) {
    const rt = state.refreshToken;
    refreshing = (async () => {
      try {
        const r = await fetch("/api/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refreshToken: rt }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        const next: AuthState = {
          token: d.accessToken,
          refreshToken: d.refreshToken || rt,
          claims: d.claims ?? {},
          tokenPreview: d.tokenPreview ?? "",
          username: d.claims?.username ?? state?.username ?? "",
        };
        set(next);
        return next.token;
      } catch (e) {
        set(null);
        throw new Error("로그인이 만료되었습니다 — 다시 로그인해 주세요");
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

// ── PKCE 유틸 (공개 클라이언트라 code 가로채기 방어에 필수) ──
function randomString(len: number): string {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}
function b64url(buf: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** ① 로그인 시작 — Agent IdP hosted UI로 전체 페이지 리다이렉트.
 *  IdP에 이미 세션이 있으면(다른 앱에서 로그인함) 폼 없이 즉시 code가 돌아온다. */
export async function startLogin(): Promise<void> {
  const info = await authInfo();
  if (!info.hostedUiBase) throw new Error("hosted UI SSO가 설정되지 않았습니다");
  const verifier = randomString(64);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const st = randomString(24);
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state: st }));
  const q = new URLSearchParams({
    client_id: info.clientId,
    response_type: "code",
    scope: (info.oauthScopes || ["openid", "profile", "email"]).join(" "),
    redirect_uri: info.callbackUrl,
    state: st,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  window.location.assign(`${info.hostedUiBase}/oauth2/authorize?${q}`);
}

/** ① 로그인 완료 — /auth/callback 에서 code를 BFF를 통해 토큰으로 교환. */
export async function finishLogin(code: string, returnedState: string): Promise<AuthState> {
  const raw = sessionStorage.getItem(PKCE_KEY);
  sessionStorage.removeItem(PKCE_KEY);
  if (!raw) throw new Error("PKCE 상태가 없습니다 — 로그인을 다시 시작해 주세요");
  const { verifier, state: st } = JSON.parse(raw);
  if (st !== returnedState) throw new Error("state 불일치 — 로그인을 다시 시작해 주세요");
  const r = await fetch("/api/auth/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, code_verifier: verifier }),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  const s: AuthState = {
    token: d.accessToken,
    refreshToken: d.refreshToken || undefined,
    claims: d.claims ?? {},
    tokenPreview: d.tokenPreview ?? "",
    username: d.claims?.username ?? "",
  };
  set(s);
  return s;
}

/** 로컬 세션만 지운다 (IdP 세션은 유지) — SSO 로그아웃 복귀·revoke 도구용. */
export function localLogout() {
  set(null);
}

/** 로그아웃 = IdP 세션까지 끊는다(single logout). Agent IdP /logout → /sso-logout 복귀
 *  → 로컬 삭제. IdP 세션이 끊기므로 위키 뷰어 등 다른 앱도 다음 접근 때 다시 로그인. */
export async function logout(): Promise<void> {
  const info = await authInfo().catch(() => null);
  set(null);
  if (info?.hostedUiBase && info.ssoLogoutUrl) {
    const q = new URLSearchParams({ client_id: info.clientId, logout_uri: info.ssoLogoutUrl });
    window.location.assign(`${info.hostedUiBase}/logout?${q}`);
  }
}
