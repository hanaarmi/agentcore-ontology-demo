// Live trait-discovery feed — subscribes to the BFF's per-customer SSE
// channel (/api/live/{cid}) fed by the Kinesis consumer. Components use
// this to show real-time "방금 파악한 성향" toasts and refresh the graph.
import { useEffect, useRef, useState } from "react";
import { authHeader } from "@/lib/api";

export interface LiveTrait {
  id: number;
  type: string;
  target: string;
  kind: string;
  confidence: number;
  existed: boolean;
  record_id: string;
  record_text: string;
  ts: number;
}

export interface LiveRecord {
  id: number;
  record_id: string;
  text: string;
  strategy: string;
  ts: number;
}

export type LiveItem =
  | ({ type: "record" } & LiveRecord)
  | ({ type: "trait" } & { trait: LiveTrait });

let seq = 0;

export function useLiveFeed(
  cid: string,
  onTrait?: (t: LiveTrait) => void,
): { traits: LiveTrait[]; records: LiveRecord[]; connected: boolean } {
  const [traits, setTraits] = useState<LiveTrait[]>([]);
  const [records, setRecords] = useState<LiveRecord[]>([]);
  const [connected, setConnected] = useState(false);
  const onTraitRef = useRef(onTrait);
  onTraitRef.current = onTrait;

  useEffect(() => {
    setTraits([]);
    setRecords([]);
    if (!cid) return; // 바인딩된 고객이 없으면 피드 없음
    // EventSource는 헤더를 못 붙여 토큰을 쿼리로 — BFF가 JWT 검증 + 내 고객 가드
    const token = authHeader().Authorization?.slice("Bearer ".length) ?? "";
    const es = new EventSource(`/api/live/${cid}?token=${encodeURIComponent(token)}`);
    es.addEventListener("start", () => setConnected(true));
    es.addEventListener("record", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setRecords((r) => [...r, { id: ++seq, ts: Date.now(), ...d }].slice(-50));
    });
    es.addEventListener("trait", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      const t: LiveTrait = { id: ++seq, ts: Date.now(), ...d };
      setTraits((prev) => [...prev, t].slice(-50));
      onTraitRef.current?.(t);
    });
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, [cid]);

  return { traits, records, connected };
}
