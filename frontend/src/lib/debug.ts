// 인증 흐름 디버그 이벤트 스토어 — 로그인/채팅 과정에서 발생하는 각 홉의
// 실제 데이터(JWT 클레임, 토큰 미리보기)를 모아 DebugPanel이 렌더한다.
import { useSyncExternalStore } from "react";

export interface AuthDebugEvent {
  id: number;
  ts: number;
  step: string; // ①~⑥ (쇼핑 체인) / ❶~❺ (위키 OBO) / 라벨
  title: string;
  detail: string;
  token_preview?: string;
  claims?: Record<string, any> | null;
  data?: Record<string, any> | null; // 그 단계의 데이터 (kid, iss, 판정값…)
  code?: string; // 그 단계에서 실제 실행되는 코드
  tool?: string;
  args_preview?: string; // 툴 호출 인자 (같은 툴 반복 호출 구분용)
  level?: "info" | "warn";
}

let events: AuthDebugEvent[] = [];
let seq = 0;
const listeners = new Set<() => void>();

export function pushDebug(ev: Omit<AuthDebugEvent, "id" | "ts">) {
  events = [...events, { ...ev, id: ++seq, ts: Date.now() }].slice(-80);
  listeners.forEach((l) => l());
}

export function clearDebug() {
  events = [];
  listeners.forEach((l) => l());
}

export function useDebugEvents(): AuthDebugEvent[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => events,
  );
}

// ── 크로스탭 채널 — 위키 동의는 별도 탭(콜백 페이지)에서 완료되므로,
//    거기서 발생한 ❸ 이벤트를 메인 탭의 debug 패널로 전달한다.
const channel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("ontolo-debug") : null;

export function broadcastDebug(ev: Omit<AuthDebugEvent, "id" | "ts">) {
  channel?.postMessage(ev);
}

channel?.addEventListener("message", (e) => {
  pushDebug(e.data as Omit<AuthDebugEvent, "id" | "ts">);
});
