"""통합 Gateway REQUEST interceptor — tier(X-Tier) 주입.

인바운드 Agent-IdP JWT의 sub를 꺼내 엔타이틀먼트 디렉터리(LDAP, 별도 VPC)에서
tier를 조회하고 `X-Tier` 헤더를 주입한다. 클라이언트가 보낸 X-Tier는 읽지 않고
**무조건 덮어쓴다** — tier는 신뢰된 디렉터리에서만 결정되므로 스푸핑이 불가능하다.

입력: event["mcp"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer <원본 JWT>"
출력 계약: {"interceptorOutputVersion":"1.0",
            "mcp":{"transformedGatewayRequest":{"body":<원본 body>, "headers":{"X-Tier":...}}}}

X-Tier가 실제 MCP 타깃 컨테이너에 닿으려면 그 타깃의 allowedRequestHeaders와
Runtime requestHeaderAllowlist에 "X-Tier"가 등록돼 있어야 한다(gateway 전파 규칙).
"""
from __future__ import annotations

import base64
import json
import logging
import os

from ldap3 import Connection, Server

log = logging.getLogger()
log.setLevel(logging.INFO)

LDAP_HOST = os.environ["LDAP_HOST"]
LDAP_PORT = int(os.environ.get("LDAP_PORT", "389"))
BASE_DN = os.environ["LDAP_BASE_DN"]
BIND_DN = os.environ["LDAP_BIND_DN"]
BIND_PW = os.environ["LDAP_BIND_PW"]
TIER_ATTR = os.environ.get("LDAP_TIER_ATTR", "tier")
DEFAULT_TIER = os.environ.get("DEFAULT_TIER", "basic")  # fail-closed: 조회 실패 시 최소 권한


def _headers(event: dict) -> dict:
    try:
        return event["mcp"]["gatewayRequest"]["headers"] or {}
    except Exception:  # noqa: BLE001
        return {}


def _bearer(headers: dict) -> str | None:
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str) and v.lower().startswith("bearer "):
            return v.split(" ", 1)[1]
    return None


def _sub(jwt: str) -> str | None:
    try:
        p = jwt.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        return c.get("sub")
    except Exception:  # noqa: BLE001
        return None


def _lookup_tier(sub: str) -> str:
    srv = Server(LDAP_HOST, port=LDAP_PORT, connect_timeout=3)
    conn = Connection(srv, user=BIND_DN, password=BIND_PW, receive_timeout=3, auto_bind=True)
    try:
        conn.search(BASE_DN, f"(marketsub={sub})", attributes=[TIER_ATTR])
        if conn.entries:
            vals = conn.entries[0][TIER_ATTR].values
            if vals:
                return str(vals[0])
    finally:
        conn.unbind()
    return DEFAULT_TIER


REVOCATION_BUCKET = os.environ.get("REVOCATION_BUCKET", "")
REVOCATION_PREFIX = os.environ.get("REVOCATION_PREFIX", "revocations/")
_s3 = None


def _revoked(token: str) -> bool:
    """마켓(Agent IdP) access 토큰이 revoke 목록에 있는지 확인한다 — 즉시 실효.
    JWT authorizer는 JWKS 오프라인 검증만 하므로 GlobalSignOut을 모르고, Cognito GetUser는 PrivateLink 미지원이라 VPC 안에서
    못 쓴다 → S3 `<prefix><sub>.json`의 revoked_at >= 토큰 iat 이면 거부. 마커 없음 → 통과, 그 외 오류 → fail-open(경고)."""
    global _s3
    if not REVOCATION_BUCKET:
        return False
    try:
        p = token.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        sub, iat = c.get("sub"), int(c.get("iat", 0))
        if not sub:
            return False
        if _s3 is None:
            import boto3
            _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        try:
            obj = _s3.get_object(Bucket=REVOCATION_BUCKET, Key=f"{REVOCATION_PREFIX}{sub}.json")
        except _s3.exceptions.NoSuchKey:
            return False
        marker = json.loads(obj["Body"].read())
        return iat <= int(marker.get("revoked_at", 0))
    except Exception as e:  # noqa: BLE001
        log.warning("revocation check skipped (%s)", str(e)[:120])
        return False


def _reject(body, message: str) -> dict:
    """타깃으로 보내지 않고 즉시 JSON-RPC 오류로 응답(short-circuit)."""
    req_id = (body or {}).get("id") if isinstance(body, dict) else None
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {
                "statusCode": 401,
                "body": {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32001, "message": message,
                                   "data": {"reason": "token_revoked", "action": "re-login"}}}}}}


def lambda_handler(event, context):
    body = event.get("mcp", {}).get("gatewayRequest", {}).get("body")
    headers = _headers(event)
    tier = DEFAULT_TIER
    sub = None
    tok = _bearer(headers)
    if tok:
        sub = _sub(tok)
        if _revoked(tok):
            try:
                _iat = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "==")).get("iat")
            except Exception:  # noqa: BLE001
                _iat = None
            log.info("REVOKED sub=%s token_iat=%s — request rejected at gateway", sub, _iat)
            return _reject(body, "마켓 세션이 종료(revoke)되었습니다 — 다시 로그인해 주세요")
    if sub:
        try:
            tier = _lookup_tier(sub)
        except Exception as e:  # noqa: BLE001  — 디렉터리 장애 시 fail-closed(basic)
            log.warning("LDAP lookup failed for sub=%s: %s", sub, e)
            tier = DEFAULT_TIER
    log.info("TIER_INJECT sub=%s tier=%s", sub, tier)

    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body, "headers": {"X-Tier": tier}}}}
