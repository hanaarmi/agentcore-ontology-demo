"""온톨로 위키 MCP 서버 — AgentCore Runtime(VPC 모드, streamable-http)으로 실행.

Gateway가 붙인 사용자 명의의 위키 IdP 위임 토큰(Authorization: Bearer)에서 신원(sub)을 읽어
그 사용자의 노트만 S3에 읽고 쓴다. 서명/발급자 검증은 Runtime의 JWT authorizer가 담당한다.
"""
from __future__ import annotations

import base64
import json
import os
import time

import boto3
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("ontolo-wiki")

NOTES_BUCKET = os.environ["NOTES_BUCKET"]
NOTES_PREFIX = os.environ.get("NOTES_PREFIX", "wiki-notes/")
WIKI_ISSUER = os.environ.get("WIKI_ISSUER", "")
WIKI_CLIENT_ID = os.environ.get("WIKI_CLIENT_ID", "")
REQUIRED_SCOPE = os.environ.get("WIKI_REQUIRED_SCOPE", "files/read")

_s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _j(o) -> str:
    return json.dumps(o, ensure_ascii=False)


def _claims(ctx: Context | None) -> dict | None:
    """Authorization: Bearer <위키 IdP access token> → 클레임.
    서명·발급자·client_id는 Runtime authorizer가 이미 검증했고, 여기서는 같은 조건을
    한 번 더 대조(심층 방어)하고 신원을 꺼낸다."""
    try:
        auth = ctx.request_context.request.headers.get("authorization", "")
    except Exception:  # noqa: BLE001
        return None
    if not auth.lower().startswith("bearer "):
        return None
    tok = auth.split(" ", 1)[1]
    try:
        p = tok.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return None
    if c.get("token_use") != "access":
        return None
    if WIKI_ISSUER and c.get("iss") != WIKI_ISSUER:
        return None
    if WIKI_CLIENT_ID and c.get("client_id") != WIKI_CLIENT_ID:
        return None
    if REQUIRED_SCOPE not in (c.get("scope") or "").split():
        return None
    return c


def _deny(reason: str) -> str:
    return _j({"error": "unauthorized", "reason": reason,
               "hint": "이 서버는 위키 IdP가 발급한 사용자 명의 access token(scope files/read)으로만 동작합니다. "
                       "Gateway가 사용자의 위키 위임 토큰을 첨부해야 합니다."})


def _key(sub: str) -> str:
    return f"{NOTES_PREFIX}{sub}.json"


def _load(sub: str) -> list:
    try:
        return json.loads(_s3.get_object(Bucket=NOTES_BUCKET, Key=_key(sub))["Body"].read())
    except _s3.exceptions.NoSuchKey:
        return []


def _save(sub: str, notes: list) -> None:
    _s3.put_object(Bucket=NOTES_BUCKET, Key=_key(sub),
                   Body=json.dumps(notes, ensure_ascii=False).encode(),
                   ContentType="application/json")


@mcp.tool()
def save_note(title: str, content: str, ctx: Context) -> str:
    """온톨로 위키(사내 위키)에 현재 사용자 명의로 노트를 저장한다.
    추천 목록·장보기 메모 등을 기록할 때 사용. 저장 주체는 호출에 실린
    위키 위임 토큰의 사용자(MarketSSO_<마켓 sub>)이며 다른 사용자 노트에는 닿을 수 없다."""
    c = _claims(ctx)
    if not c:
        return _deny("위키 사용자 토큰 없음/불일치")
    sub, user = c["sub"], c.get("username") or c["sub"]
    notes = _load(sub)
    note = {"id": f"n-{int(time.time() * 1000)}", "title": title, "content": content,
            "author": user, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    notes.append(note)
    _save(sub, notes)
    return _j({"saved": note, "total_notes": len(notes), "user": user, "sub": sub,
               "granted_scopes": c.get("scope", "").split(),
               "note": "사용자 위임 토큰의 신원(sub)으로 위키팀 VPC 안 S3에 저장됨"})


@mcp.tool()
def list_notes(ctx: Context) -> str:
    """온톨로 위키에서 현재 사용자가 저장한 노트 목록을 가져온다 (본인 노트만)."""
    c = _claims(ctx)
    if not c:
        return _deny("위키 사용자 토큰 없음/불일치")
    sub, user = c["sub"], c.get("username") or c["sub"]
    return _j({"user": user, "sub": sub, "granted_scopes": c.get("scope", "").split(),
               "notes": _load(sub)})


@mcp.tool()
def whoami(ctx: Context) -> str:
    """이 위키 MCP 서버가 어떤 사용자 토큰으로 호출되었는지 (인증 흐름 디버그)."""
    c = _claims(ctx)
    if not c:
        return _deny("위키 사용자 토큰 없음/불일치")
    return _j({"issuer": c.get("iss"), "client_id": c.get("client_id"), "username": c.get("username"),
               "sub": c.get("sub"), "scope": c.get("scope"),
               "note": "마켓 JWT가 아니라 위키 IdP가 발급한 위임 토큰 — Gateway가 Token Vault에서 꺼내 붙였다"})


if __name__ == "__main__":
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
    mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
    # stateless HTTP: 세션은 Gateway가 관리한다.
    mcp.settings.stateless_http = True
    # Runtime 뒤에서는 Host 헤더가 플랫폼 내부 도메인으로 바뀌어 들어오므로 DNS rebinding 보호를 끈다.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp.run(transport="streamable-http")
