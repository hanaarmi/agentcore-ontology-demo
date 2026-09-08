"""통합 Gateway MCP 호출 — 세션 유지 클라이언트 (neptune_gateway / wiki_gateway 공용).
Gateway는 `Mcp-Session-Id`가 있을 때만 MCP 서버 타깃의 세션을 재사용한다(없으면 툴 호출마다 새 런타임 세션).
사용자별로 initialize 1회 → 세션 ID 캐시 → 이후 호출에 실어 보내고, 만료/유실(400·404) 시 한 번 재초기화한다.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
import uuid

UNIFIED_GATEWAY_URL = os.environ.get("UNIFIED_GATEWAY_URL", "")
PROTOCOL_VERSION = "2025-11-25"

_sessions: dict[str, str] = {}   # jwt sub → Mcp-Session-Id
_lock = threading.Lock()


def available() -> bool:
    return bool(UNIFIED_GATEWAY_URL)


def _sub(jwt: str) -> str:
    """세션 캐시 키 — sub + 토큰 꼬리. 재로그인(새 토큰) 시 이전 토큰으로 만든 Gateway 세션을
    재사용하지 않고 새 세션을 받도록 토큰을 키에 포함한다."""
    try:
        p = jwt.split(".")[1]
        sub = str(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("sub", ""))
        return f"{sub}:{jwt[-16:]}"
    except Exception:  # noqa: BLE001
        return jwt[-16:]


def _post(body: dict, jwt: str, sid: str | None, timeout: int):
    """→ (status_code, headers, raw_text). HTTPError도 같은 형태로 돌려준다."""
    headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(UNIFIED_GATEWAY_URL, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", errors="replace")


def _parse(raw: str, ctype: str) -> dict | None:
    payload = raw
    if "text/event-stream" in ctype or raw.lstrip().startswith("event:") or "\ndata:" in raw[:200]:
        payload = next((l[5:].strip() for l in raw.replace("\r\n", "\n").split("\n") if l.startswith("data:")), raw)
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _ensure_session(jwt: str, timeout: int) -> str | None:
    key = _sub(jwt)
    with _lock:
        sid = _sessions.get(key)
    if sid:
        return sid
    status, headers, _ = _post({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
                                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "ontolo-shopping-agent", "version": "1"}}},
                               jwt, None, timeout)
    sid = next((v for k, v in headers.items() if k.lower() == "mcp-session-id"), None)
    if status < 400 and sid:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, jwt, sid, timeout)
        with _lock:
            _sessions[key] = sid
    return sid


def call(tool: str, arguments: dict, jwt: str, timeout: int = 75) -> dict:
    """통합 Gateway로 MCP tools/call (세션 유지).

    반환:
      {"status": "ok", "result": <dict>, "isError": <bool>}   성공
      {"status": "consent", "url": <str>}                     3LO 동의 필요(-32042)
      {"status": "error", "text": <str>}                      기타 오류
    """
    if not UNIFIED_GATEWAY_URL:
        return {"status": "error", "text": "UNIFIED_GATEWAY_URL 미설정"}
    sid = _ensure_session(jwt, timeout)
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    status, headers, raw = _post(body, jwt, sid, timeout)
    if status in (400, 404) and sid:
        # 세션 만료/유실(타깃 재시작 등) → 재초기화 후 1회 재시도
        with _lock:
            _sessions.pop(_sub(jwt), None)
        sid = _ensure_session(jwt, timeout)
        status, headers, raw = _post(body, jwt, sid, timeout)
    if status >= 400:
        return {"status": "error", "text": f"HTTP {status}: {raw[:400]}"}
    rpc = _parse(raw, str(headers.get("Content-Type", "")))
    if rpc is None:
        return {"status": "error", "text": f"unparseable response: {raw[:300]}"}
    if "error" in rpc:
        err = rpc["error"]
        if err.get("code") == -32042:
            els = (err.get("data") or {}).get("elicitations") or []
            url = els[0].get("url") if els else ""
            if url:
                return {"status": "consent", "url": url}
        return {"status": "error", "text": json.dumps(err, ensure_ascii=False)[:400]}
    result = rpc.get("result", {})
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {"raw": text}
    return {"status": "ok", "result": parsed, "isError": bool(result.get("isError", False)), "text": text}
