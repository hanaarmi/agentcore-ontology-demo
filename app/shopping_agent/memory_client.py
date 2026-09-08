"""AgentCore Memory helper for the shopping agent.
Short-term: 매 대화 턴을 (actorId=customer_id, sessionId) 이벤트로 기록하고, AgentCore의 비동기 장기 전략
(userPreference + semantic)이 /preferences/{customer_id}, /facts/{customer_id}로 추출한다. Long-term: retrieve_memories()가 이를 의미 검색한다.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = os.environ.get("MEMORY_ID", "")

_cfg = Config(retries={"max_attempts": 2, "mode": "standard"},
              read_timeout=20, connect_timeout=5)
_client = None


def _ac():
    global _client
    if _client is None:
        # 공용 세션(aws_session)에서 생성 — 별도 Session을 만들면 자격증명을 다시 해석한다
        import aws_session
        _client = aws_session.SESSION.client("bedrock-agentcore", region_name=REGION, config=_cfg)
    return _client


def save_turn(customer_id: str, session_id: str,
              user_text: str, assistant_text: str) -> None:
    """Persist one chat turn to short-term memory. Best-effort.

    사용자 발화만 `conversational`(장기기억 추출 대상)로, 에이전트 답변은 `blob`(이력용, 추출 비대상)으로 저장한다.
    답변은 프로필을 되풀이하므로 conversational로 넣으면 같은 사실이 매 턴 새 레코드로 추출된다."""
    if not MEMORY_ID:
        return
    try:
        _ac().create_event(
            memoryId=MEMORY_ID,
            actorId=customer_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(tz=timezone.utc),
            payload=[
                {"conversational": {"content": {"text": user_text}, "role": "USER"}},
                {"blob": json.dumps({"role": "ASSISTANT", "text": assistant_text}, ensure_ascii=False)},
            ],
            clientToken=uuid.uuid4().hex,
        )
    except Exception:  # noqa: BLE001
        pass


def _payload_turns(payload: list) -> list[dict]:
    """이벤트 payload → [{role, text}] — conversational(사용자) + blob(에이전트 답변) 모두."""
    out = []
    for blk in payload or []:
        conv = blk.get("conversational")
        if conv:
            out.append({"role": conv.get("role", ""), "text": (conv.get("content") or {}).get("text", "")})
            continue
        blob = blk.get("blob")
        if blob:
            try:
                d = json.loads(blob) if isinstance(blob, str) else blob
                if isinstance(d, dict) and d.get("text"):
                    out.append({"role": d.get("role", "ASSISTANT"), "text": d["text"]})
            except ValueError:
                pass
    return out


def recent_turns(customer_id: str, session_id: str, limit: int = 10) -> list[dict]:
    """Short-term: recent conversation turns in this session (oldest first)."""
    if not MEMORY_ID:
        return []
    try:
        r = _ac().list_events(
            memoryId=MEMORY_ID,
            actorId=customer_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=limit,
        )
    except Exception:  # noqa: BLE001
        return []
    turns = []
    for ev in r.get("events", []):
        turns.extend(_payload_turns(ev.get("payload", [])))
    return turns


def retrieve_memories(customer_id: str, query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Long-term: semantic search over async-extracted records
    (/preferences/{actorId} + /facts/{actorId}).

    지금 메시지와 관련 있는 기억만 쓴다 — 두 네임스페이스 결과를 합쳐
    유사도 점수순 상위 top_k만 반환하고, 최고점 대비 크게 떨어지는
    꼬리는 잘라서 "과거 기억 전부 나열"이 되지 않게 한다."""
    if not MEMORY_ID:
        return []
    out: list[dict] = []
    for ns in (f"/preferences/{customer_id}", f"/facts/{customer_id}"):
        try:
            r = _ac().retrieve_memory_records(
                memoryId=MEMORY_ID,
                namespace=ns,
                searchCriteria={"searchQuery": query, "topK": top_k * 2},
            )
        except Exception:  # noqa: BLE001
            continue
        for rec in r.get("memoryRecordSummaries", []):
            content = (rec.get("content") or {}).get("text", "")
            out.append({
                "namespace": ns,
                "text": content,
                "score": rec.get("score") or 0.0,
            })
    out.sort(key=lambda m: m["score"], reverse=True)
    out = out[:top_k]
    if out and out[0]["score"] > 0:
        cutoff = out[0]["score"] * 0.6
        out = [m for m in out if m["score"] >= cutoff]
    return out
