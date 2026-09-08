// BFF API client — plain fetch + a manual SSE reader (chat/sync are POST
// streams, so EventSource can't be used).

// 로그인 토큰 — BFF 데이터 API는 JWT 검증 + "내 고객만" 가드가 있어 모든 호출에 붙인다.
// (auth.ts가 이 파일을 import 하므로 순환을 피해 localStorage에서 직접 읽는다)
export function authHeader(): Record<string, string> {
  try {
    const t = JSON.parse(localStorage.getItem("ontolo-auth") || "null")?.token;
    return t ? { Authorization: `Bearer ${t}` } : {};
  } catch {
    return {};
  }
}

export async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url, { headers: authHeader() });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

export type SSEHandler = (event: string, data: any) => void;

export function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  headers?: Record<string, string>,
): () => void {
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeader(), ...(headers ?? {}) },
        body: body == null ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!r.ok || !r.body) {
        onEvent("error", { message: `HTTP ${r.status}`, status: r.status });
        return;
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        for (;;) {
          const i = buf.indexOf("\n\n");
          if (i < 0) break;
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let ev = "message";
          let data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) ev = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (data) {
            try {
              onEvent(ev, JSON.parse(data));
            } catch {
              onEvent(ev, { raw: data });
            }
          }
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError") onEvent("error", { message: String(e) });
    }
  })();
  return () => ctrl.abort();
}

// ── types ──────────────────────────────────────────────────────────

export interface Customer {
  customer_id: string;
  name: string;
  segment: string;
  bio: string;
}

export interface Trait {
  rel: string;
  trait: string;
  kind: string;
  confidence: number;
  source: string;
  updated_at?: string;
}

export interface Purchase {
  sku: string;
  name: string;
  price?: number;
  category?: string;
  date: string;
  qty: number;
}

export interface Profile {
  customer: { name?: string; segment?: string };
  traits: Trait[];
  purchases: Purchase[];
}

export interface Candidate {
  sku: string;
  name: string;
  price: number;
  category?: string;
  brand?: string;
  matched_traits: string[];
  score: number;
}

export interface GraphNode {
  id: string;
  label: "Customer" | "Trait" | "Product" | "ProductCandidate" | "SharedInterest";
  name: string;
  kind?: string;
  segment?: string;
  sku?: string;
  count?: number;
}

export interface GraphEdge {
  from: string;
  to: string;
  rel: string;
  confidence?: number;
  source?: string;
  date?: string;
  updated_at?: string;
}

export interface MemoryData {
  shortTerm: { sessionId: string; ts: string; role: string; text: string }[];
  longTerm: { namespace: string; recordId: string; text: string; createdAt: string }[];
}
