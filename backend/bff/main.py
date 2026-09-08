"""agentcore-ontology BFF — FastAPI backend for the local demo UI.

Why a BFF: the browser can't hold AWS credentials, so every AWS call
(AgentCore Runtime invoke, Memory data-plane, Neptune openCypher)
fans out from here with boto3.

Endpoints:
  POST /api/chat                 SSE — invoke the shopping agent runtime,
                                 stream chunks + tool events
  GET  /api/customers            demo personas
  GET  /api/profile/{cid}        Neptune graph profile (traits/purchases)
  GET  /api/recommendations/{cid}graph-only candidate list
  GET  /api/graph/{cid}          node/edge payload for the graph viz
  GET  /api/memory/{cid}         short-term events + long-term records
  GET  /api/live/{cid}           SSE — real-time trait discovery feed
                                 (Kinesis → LLM normalize → Neptune MERGE)
  POST /api/sync/{cid}           manual backfill: pull all long-term records
                                 and re-project into the graph (SSE)
  GET  /api/status               resource wiring status

Real-time projection: AgentCore Memory pushes every async-extracted
long-term record to Kinesis (streamDeliveryResources). A background
consumer here normalizes each record into graph facts and MERGEs
Customer→Trait edges with memory_record_id provenance, then broadcasts
to per-customer SSE subscribers so the UI updates mid-conversation.
"""
from __future__ import annotations

import asyncio
import threading
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import base64
import urllib.parse

import boto3
import requests as httpreq
from botocore.config import Config
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("ONTOLO_STATE_DIR", ROOT / "infra" / "state"))
OBO_STATE = Path(os.environ.get("ONTOLO_OBO_STATE_DIR", ROOT / "obo" / "infra" / "state"))
DATA = ROOT / "data"
CUSTOMERS_FILE = Path(os.environ.get("ONTOLO_CUSTOMERS_FILE", DATA / "customers.json"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
WIKI_BASE_URL = os.environ.get("WIKI_BASE_URL", "http://localhost:5182")

MODEL_ID = os.environ.get("ONTOLO_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")


def _state(name: str) -> dict:
    p = STATE / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


MEMORY = _state("memory")
NEPTUNE = _state("neptune")
RUNTIME = _state("runtime")
STREAM = _state("stream")
GATEWAY = _state("gateway")

# OBO 체인 인바운드 IdP (마켓 IdP(Cognito)) — obo 스택과 공유
_OBO_INBOUND_PATH = OBO_STATE / "inbound.json"
INBOUND = json.loads(_OBO_INBOUND_PATH.read_text()) if _OBO_INBOUND_PATH.exists() else {}

app = FastAPI(title="agentcore-ontology BFF")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

_clients: dict = {}
_STREAM_CFG = Config(retries={"max_attempts": 1, "mode": "standard"},
                     read_timeout=180, connect_timeout=10)


def _c(name: str):
    if name not in _clients:
        cfg = _STREAM_CFG if name == "bedrock-agentcore" else None
        _clients[name] = boto3.client(name, region_name=REGION, config=cfg)
    return _clients[name]


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ───────────── Neptune 접근 — 데이터팀 MCP 경유만 (직접 조회 없음) ─────────────
#
# Neptune Analytics는 데이터팀 VPC B 안에서만 접근 가능(publicConnectivity=false).
# 이 BFF(마켓)는 Neptune에 직접 닿지 않고 데이터팀의 Neptune MCP 서버를 통해서만 다룬다:
#   · 사용자 대신 읽기  → 통합 Gateway(사용자 JWT → interceptor X-Tier → Tool IdP 위임 토큰 graph/read)
#   · 시스템 쓰기(MERGE) → Neptune MCP 런타임에 파이프라인 시스템 토큰(client_credentials, graph/write)
# "사용자 일은 사용자 신원, 시스템 일은 시스템 신원."
UNIFIED_GW = _state("gateway_unified")
NEPTUNE_MCP = _state("neptune_mcp")
PIPELINE = _state("pipeline_client")
MCP_PROTOCOL = "2025-11-25"


# MCP 세션 캐시 — key → Mcp-Session-Id. Gateway는 세션이 있을 때만 타깃(Runtime) MCP 세션을 재사용해
# 호출당 콜드스타트를 피하고, Runtime 직접 호출도 Mcp-Session-Id로 microVM이 고정된다.
_mcp_sessions: dict[str, str] = {}
_mcp_lock = threading.Lock()


def _mcp_post(url: str, body: dict, bearer: str, sid: str | None, extra_headers: dict | None, timeout: int):
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL,
               **(extra_headers or {})}
    if sid:
        headers["Mcp-Session-Id"] = sid
    return httpreq.post(url, headers=headers, json=body, timeout=timeout)


def _mcp_ensure_session(url: str, bearer: str, key: str, extra_headers: dict | None, timeout: int) -> str | None:
    with _mcp_lock:
        sid = _mcp_sessions.get(key)
    if sid:
        return sid
    r = _mcp_post(url, {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
                        "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "ontolo-bff", "version": "1"}}},
                  bearer, None, extra_headers, timeout)
    sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
    if r.status_code < 400 and sid:
        _mcp_post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, bearer, sid, extra_headers, timeout)
        with _mcp_lock:
            _mcp_sessions[key] = sid
    return sid


def _mcp_rpc(url: str, tool: str, arguments: dict, bearer: str, extra_headers: dict | None = None,
             timeout: int = 60, session_key: str | None = None) -> dict:
    """MCP tools/call (JSON-RPC over streamable-http) → {"status": ok|consent|error, ...}
    session_key가 있으면 initialize로 받은 Mcp-Session-Id를 캐시·재사용(400/404면 1회 재초기화)."""
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}
    try:
        sid = _mcp_ensure_session(url, bearer, session_key, extra_headers, timeout) if session_key else None
        r = _mcp_post(url, body, bearer, sid, extra_headers, timeout)
        if r.status_code in (400, 404) and session_key and sid:
            with _mcp_lock:
                _mcp_sessions.pop(session_key, None)
            sid = _mcp_ensure_session(url, bearer, session_key, extra_headers, timeout)
            r = _mcp_post(url, body, bearer, sid, extra_headers, timeout)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "text": str(e)[:300]}
    # text/event-stream에 charset이 없어 requests가 latin-1로 디코딩 → UTF-8 바이트 0x85가 NEL이 되어
    # splitlines()가 JSON을 자른다. utf-8 직접 디코딩 + "\n" 분리.
    raw = r.content.decode("utf-8", errors="replace")
    if r.status_code >= 400:
        return {"status": "error", "text": f"HTTP {r.status_code}: {raw[:300]}"}
    payload = raw
    if "text/event-stream" in r.headers.get("Content-Type", "") or "\ndata:" in raw[:200] or raw.startswith("event:"):
        payload = next((l[5:].strip() for l in raw.replace("\r\n", "\n").split("\n") if l.startswith("data:")), raw)
    try:
        rpc = json.loads(payload)
    except ValueError:
        return {"status": "error", "text": f"unparseable: {raw[:200]}"}
    if "error" in rpc:
        err = rpc["error"]
        if err.get("code") == -32042:  # MCP URL elicitation — Tool IdP 3LO 동의 필요
            els = (err.get("data") or {}).get("elicitations") or []
            if els and els[0].get("url"):
                return {"status": "consent", "url": els[0]["url"]}
        return {"status": "error", "text": json.dumps(err, ensure_ascii=False)[:400]}
    result = rpc.get("result", {})
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {"raw": text}
    return {"status": "ok", "result": parsed, "isError": bool(result.get("isError"))}


def _gw_call(tool: str, arguments: dict, jwt: str) -> dict:
    """사용자 명의 읽기 — 통합 Gateway의 neptune___* 툴을 사용자 JWT로 호출(에이전트와 동일 경로)."""
    if not UNIFIED_GW.get("gatewayUrl"):
        return {"status": "error", "text": "통합 Gateway 미설정"}
    # Gateway MCP 세션은 JWT sub에 묶임 → 사용자별 세션 키.
    # 키에 토큰 해시 포함 — sub만 쓰면 revoke 후 재로그인해도 이전 토큰의 Gateway 세션이 재사용된다.
    return _mcp_rpc(UNIFIED_GW["gatewayUrl"], f"neptune___{tool}", arguments, jwt,
                    session_key=f"gw:{_decode_claims(jwt).get('sub', '')}:{jwt[-16:]}")


_machine_tok: dict = {}
_PIPELINE_SESSION = f"memory-pipeline-{uuid.uuid4().hex}"  # 프로세스당 고정(33자+)


def _machine_token() -> str:
    """파이프라인 시스템 토큰(client_credentials · graph/write) — 만료 60초 전 갱신."""
    import time as _t
    if _machine_tok.get("exp", 0) - 60 > _t.time():
        return _machine_tok["token"]
    basic = base64.b64encode(f"{PIPELINE['clientId']}:{PIPELINE['clientSecret']}".encode()).decode()
    r = httpreq.post(PIPELINE["tokenUrl"], headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
                     data={"grant_type": "client_credentials", "scope": PIPELINE["scope"]}, timeout=15)
    r.raise_for_status()
    tok = r.json()
    _machine_tok.update({"token": tok["access_token"], "exp": _t.time() + int(tok.get("expires_in", 3600))})
    return _machine_tok["token"]


def _mcp_write(tool: str, arguments: dict) -> dict:
    """시스템 쓰기 — Neptune MCP 런타임을 파이프라인 토큰으로 직접 호출(Gateway 아님: 사용자 컨텍스트 없음)."""
    if not (PIPELINE and NEPTUNE_MCP.get("endpoint")):
        raise RuntimeError("pipeline_client.json / neptune_mcp.json 미설정")
    # 세션 ID를 BFF 프로세스당 하나로 고정 — 요청마다 새 세션이면 런타임이 매번 재기동된다.
    # Mcp-Session-Id(플랫폼 발급)를 재사용해야 같은 microVM에 붙는다(MCP 런타임은 이 헤더로 라우팅).
    res = _mcp_rpc(NEPTUNE_MCP["endpoint"], tool, arguments, _machine_token(),
                   extra_headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": _PIPELINE_SESSION},
                   session_key="pipeline")
    if res["status"] != "ok" or res.get("isError"):
        raise RuntimeError(f"MCP {tool} 실패: {res.get('text') or res.get('result')}")
    return res["result"]


def _gw_http(res: dict, cid: str):
    """Gateway 호출 결과를 HTTP 응답으로 — 동의 필요(409)·오류(502). 성공이면 None."""
    if res["status"] == "consent":
        return JSONResponse({"consent_required": True, "consent_url": res["url"], "customer_id": cid,
                             "message": "개인화 그래프(graph/read) 접근 동의가 필요합니다"}, status_code=409)
    if res["status"] != "ok":
        return JSONResponse({"error": res.get("text", "gateway error")}, status_code=502)
    return None


# ───────────────────────── Auth (OBO chain) ─────────────────────────

def _decode_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {k: claims.get(k) for k in
                ("sub", "username", "cognito:groups", "client_id", "scope",
                 "iss", "token_use", "exp") if k in claims}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/auth/info")
def auth_info():
    """데모용 — 로그인 폼 프리필 정보. (마켓 IdP 풀은 SAML IdP 연동 지점:
    운영이라면 여기서 기업 IdP로 SAML 리다이렉트가 일어난다.)"""
    if not INBOUND:
        return {"enabled": False}
    return {
        "enabled": True,
        # 비밀번호는 응답에 싣지 않는다 — 로그인은 IdP hosted UI에서만
        "users": [{k: v for k, v in u.items() if k != "password"} for u in INBOUND.get("users", [])],
        "poolId": INBOUND.get("poolId", ""),
        "clientId": INBOUND.get("clientId", ""),
        "issuer": f"https://cognito-idp.{REGION}.amazonaws.com/{INBOUND.get('poolId', '')}",
        # 위키 뷰어 베이스 URL — SPA가 single logout 시 wikiBase + "/logout"으로 보낸다
        "wikiBase": WIKI_BASE_URL,
        # hosted UI SSO — SPA가 authorize/logout URL을 만들 때 쓴다
        "hostedUiBase": INBOUND.get("hostedUiBase", ""),
        "callbackUrl": INBOUND.get("callbackUrl", ""),
        "ssoLogoutUrl": INBOUND.get("ssoLogoutUrl", ""),
        "oauthScopes": INBOUND.get("oauthScopes", ["openid", "profile", "email"]),
    }


@app.post("/api/auth/callback")
def auth_callback(body: dict):
    """① 사용자 로그인(hosted UI Authorization Code + PKCE) — code → 토큰 교환.
    hosted UI 로그인으로 IdP 도메인에 브라우저 세션이 생겨 위키 뷰어 등 다른 앱이 페더레이션 때 같은 사용자를 상속한다(SSO).
    공개 클라이언트(secret 없음)라 PKCE 필수. 반환 형식은 /api/login과 동일."""
    hosted = INBOUND.get("hostedUiBase")
    if not INBOUND or not hosted:
        return JSONResponse({"error": "hosted UI SSO가 설정되지 않았습니다 (inbound 상태 파일의 hostedUiBase 확인)"},
                            status_code=500)
    code, verifier = body.get("code", ""), body.get("code_verifier", "")
    if not code or not verifier:
        return JSONResponse({"error": "code / code_verifier가 없습니다"}, status_code=400)
    r = httpreq.post(f"{hosted}/oauth2/token",
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     data={"grant_type": "authorization_code", "client_id": INBOUND["clientId"],
                           "code": code, "redirect_uri": INBOUND["callbackUrl"],
                           "code_verifier": verifier}, timeout=15)
    if r.status_code != 200:
        return JSONResponse({"error": f"토큰 교환 실패 {r.status_code}: {r.text[:200]}"}, status_code=401)
    tok = r.json()
    token = tok["access_token"]
    return {
        "accessToken": token,
        "refreshToken": tok.get("refresh_token", ""),
        "expiresIn": tok.get("expires_in", 3600),
        "claims": _decode_claims(token),
        "tokenPreview": f"{token[:20]}...{token[-10:]}",
    }


@app.post("/api/login")
def login(body: dict):
    """① 사용자 로그인 — 마켓 IdP(Cognito) (기업 SAML IdP 연동 지점).
    반환된 access token이 이후 모든 홉(BFF→Runtime→Gateway→MCP 툴)을
    그대로 통과한다."""
    if not INBOUND:
        return JSONResponse({"error": "OBO 인바운드 풀이 설정되지 않았습니다"}, status_code=500)
    try:
        r = _c("cognito-idp").initiate_auth(
            ClientId=INBOUND["clientId"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": body.get("username", ""),
                            "PASSWORD": body.get("password", "")},
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=401)
    token = r["AuthenticationResult"]["AccessToken"]
    return {
        "accessToken": token,
        "refreshToken": r["AuthenticationResult"].get("RefreshToken", ""),
        "expiresIn": r["AuthenticationResult"]["ExpiresIn"],
        "claims": _decode_claims(token),
        "tokenPreview": f"{token[:20]}...{token[-10:]}",
    }


@app.post("/api/refresh")
def refresh(body: dict):
    """①' 액세스 토큰 갱신 — refresh token으로 새 access token을 받는다.
    AgentCore Runtime의 JWT authorizer는 만료 60초 전부터 토큰을 거부하므로("Ineffectual token")
    SPA는 exp가 임박하면 호출 직전에 여기서 갱신한다."""
    if not INBOUND:
        return JSONResponse({"error": "OBO 인바운드 풀이 설정되지 않았습니다"}, status_code=500)
    rt = body.get("refreshToken", "")
    if not rt:
        return JSONResponse({"error": "refreshToken이 없습니다"}, status_code=400)
    try:
        r = _c("cognito-idp").initiate_auth(
            ClientId=INBOUND["clientId"],
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": rt},
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=401)
    token = r["AuthenticationResult"]["AccessToken"]
    return {
        "accessToken": token,
        # REFRESH_TOKEN_AUTH는 새 refresh token을 주지 않는다(회전 미설정) → 기존 것 유지
        "refreshToken": r["AuthenticationResult"].get("RefreshToken", rt),
        "expiresIn": r["AuthenticationResult"]["ExpiresIn"],
        "claims": _decode_claims(token),
        "tokenPreview": f"{token[:20]}...{token[-10:]}",
    }


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else ""


# ── JWT 검증 + 고객 바인딩 — 데이터 API(/api/profile|graph|memory|… /{cid}) 가드 ──
# Agent IdP access token을 서명·발급자·client_id까지 검증하고, cid가 토큰 sub에 바인딩된 고객일 때만 응답.
# 매핑 진실원 = customers.json market_user (에이전트·Neptune MCP와 동일).
import jwt as _pyjwt  # PyJWT

_CUSTOMERS = json.loads(CUSTOMERS_FILE.read_text())["customers"]
_CUSTOMER_BY_SUB = {c["market_user"]["sub"]: c for c in _CUSTOMERS if c.get("market_user")}
_ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{INBOUND.get('poolId', '')}"
_JWKS = _pyjwt.PyJWKClient(f"{_ISSUER}/.well-known/jwks.json") if INBOUND else None


def _verify_jwt(token: str) -> dict | None:
    """Agent IdP access token 검증(서명·exp·iss·client_id·token_use) → claims, 실패 None."""
    if not token or not _JWKS:
        return None
    try:
        key = _JWKS.get_signing_key_from_jwt(token).key
        # leeway 60s — 클라이언트 시계 오차로 갓 발급된 토큰이 "not yet valid (iat)"로 거부되는 것을 방지
        claims = _pyjwt.decode(token, key, algorithms=["RS256"], issuer=_ISSUER,
                               options={"verify_aud": False}, leeway=60)
    except Exception:  # noqa: BLE001
        return None
    if claims.get("client_id") != INBOUND.get("clientId") or claims.get("token_use") != "access":
        return None
    # revoke 즉시 실효 — JWKS 오프라인 검증은 GlobalSignOut을 모르고, hosted UI 토큰은 GetUser로 revoke를
    # 판별할 수 없어 interceptor·에이전트와 같은 S3 revoke 목록(iat <= revoked_at)으로 판정.
    if _revoked_by_list(claims):
        return None
    return claims


def _revoked_by_list(claims: dict) -> bool:
    """revoke 목록(S3 revocations/<sub>.json) — 토큰 iat <= revoked_at 이면 True. 마커 없음/오류 → False."""
    rev = _state("revocation")
    sub = claims.get("sub", "")
    if not (rev.get("bucket") and sub):
        return False
    try:
        obj = _c("s3").get_object(Bucket=rev["bucket"], Key=f"{rev.get('prefix', 'revocations/')}{sub}.json")
        return int(claims.get("iat", 0)) <= int(json.loads(obj["Body"].read()).get("revoked_at", 0))
    except Exception:  # noqa: BLE001  — NoSuchKey 등
        return False


def _customer_of(claims: dict) -> dict | None:
    return _CUSTOMER_BY_SUB.get(claims.get("sub", ""))


def _guard(request: Request, cid: str, token_qs: str = ""):
    """cid 데이터 API 가드 — 로그인 필수 + cid == 내 고객. 통과면 None, 아니면 오류 응답.
    (EventSource는 헤더를 못 붙여 SSE는 ?token= 도 허용)"""
    claims = _verify_jwt(_bearer(request) or token_qs)
    if not claims:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    mine = _customer_of(claims)
    if not mine or mine["customer_id"] != cid:
        return JSONResponse({"error": "다른 고객의 데이터입니다 — 로그인한 계정에 바인딩된 고객만 조회할 수 있습니다",
                             "your_customer": mine["customer_id"] if mine else None,
                             "requested": cid}, status_code=403)
    return None


@app.get("/api/me")
def me(request: Request):
    """로그인 사용자 + 바인딩된 고객 프로필(없으면 null). UI는 이걸로 cid를 정한다(선택 불가)."""
    claims = _verify_jwt(_bearer(request))
    if not claims:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    c = _customer_of(claims)
    return {"username": claims.get("username"), "sub": claims.get("sub"),
            "groups": claims.get("cognito:groups") or [],
            "customer": ({k: c[k] for k in ("customer_id", "name", "segment", "bio")} if c else None),
            "binding": "JWT sub → 계정에 바인딩된 고객 프로필 (UI·BFF·에이전트·Neptune MCP 동일 규칙)"}


# Tool IdP(풀 B) — 위키·Neptune 위임 토큰(동의)의 발급자. 동의 초기화에 필요.
_DOWNSTREAM_PATH = OBO_STATE / "downstream.json"
DOWNSTREAM = json.loads(_DOWNSTREAM_PATH.read_text()) if _DOWNSTREAM_PATH.exists() else {}


def _revoke_one(username: str) -> dict:
    """한 계정의 인증 상태를 초기화한다(데모 테스트 리셋용).
    Agent IdP GlobalSignOut(refresh token 무효) + S3 revoke 마커(오프라인 JWKS 검증은 GlobalSignOut을 모르므로)
    + Tool IdP JIT 사용자 `MarketSSO_<마켓 sub>` GlobalSignOut(Token Vault 위임 토큰 무효 → 재동의; 볼트 삭제 API 없음)."""
    idp = _c("cognito-idp")
    out = {"username": username, "agentIdp": None, "toolIdp": None}
    try:
        u = idp.admin_get_user(UserPoolId=INBOUND["poolId"], Username=username)
        sub = next(a["Value"] for a in u["UserAttributes"] if a["Name"] == "sub")
        idp.admin_user_global_sign_out(UserPoolId=INBOUND["poolId"], Username=username)
        out["agentIdp"] = "signed out (refresh token 무효)"
        out["sub"] = sub
    except Exception as e:  # noqa: BLE001
        out["agentIdp"] = f"error: {str(e)[:120]}"
        return out
    # revoke 목록(S3) — VPC 안의 interceptor·에이전트는 Cognito 온라인 확인(GetUser)을 PrivateLink로
    # 호출할 수 없으므로 이 마커를 읽어 즉시 거부한다(token.iat <= revoked_at). 재로그인 토큰은 통과.
    rev = _state("revocation")
    if rev.get("bucket"):
        try:
            _c("s3").put_object(Bucket=rev["bucket"], Key=f"{rev.get('prefix', 'revocations/')}{sub}.json",
                                Body=json.dumps({"revoked_at": int(__import__('time').time()), "username": username}).encode(),
                                ContentType="application/json")
            out["revocationList"] = f"s3://{rev['bucket']}/{rev.get('prefix', 'revocations/')}{sub}.json (interceptor·에이전트 즉시 거부)"
        except Exception as e:  # noqa: BLE001
            out["revocationList"] = f"error: {str(e)[:100]}"
    if DOWNSTREAM.get("poolId"):
        jit = f"MarketSSO_{sub}"
        try:
            idp.admin_user_global_sign_out(UserPoolId=DOWNSTREAM["poolId"], Username=jit)
            out["toolIdp"] = f"{jit} signed out (위임 토큰 무효 → 재동의 필요)"
        except idp.exceptions.UserNotFoundException:
            out["toolIdp"] = f"{jit} 없음 (아직 동의한 적 없음)"
        except Exception as e:  # noqa: BLE001
            out["toolIdp"] = f"error: {str(e)[:120]}"
    return out


@app.get("/api/admin/users")
def admin_users():
    """데모 테스트 도구 — Agent IdP(풀 A)의 전체 계정 목록(초기화 대상)."""
    if not INBOUND:
        return {"users": []}
    r = _c("cognito-idp").list_users(UserPoolId=INBOUND["poolId"], Limit=60)
    return {"users": sorted(u["Username"] for u in r["Users"])}


@app.post("/api/admin/revoke")
def admin_revoke(body: dict):
    """데모 테스트 도구 — 계정 인증 초기화. body.username = "alice" | "*"(전체)."""
    if not INBOUND:
        return JSONResponse({"error": "OBO 인바운드 풀이 설정되지 않았습니다"}, status_code=500)
    target = (body.get("username") or "").strip()
    if not target:
        return JSONResponse({"error": "username이 없습니다"}, status_code=400)
    if target == "*":
        users = [u["Username"] for u in
                 _c("cognito-idp").list_users(UserPoolId=INBOUND["poolId"], Limit=60)["Users"]]
    else:
        users = [target]
    return {"results": [_revoke_one(u) for u in users]}


@app.post("/api/oauth/complete")
def oauth_complete(body: dict, request: Request):
    """❸ 위키 3LO 동의 콜백 완료 — 세션 바인딩.
    위키 IdP 동의 세션을 마켓 IdP 사용자(inbound JWT)에게 명시적으로 묶는다.
    이 바인딩이 있어야 Token Vault가 (워크로드, 사용자) 키로 refresh token을 저장한다."""
    jwt = _bearer(request)
    session_id = body.get("session_id", "")
    if not jwt:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    if not session_id:
        return JSONResponse({"error": "session_id가 없습니다"}, status_code=400)
    try:
        _c("bedrock-agentcore").complete_resource_token_auth(
            sessionUri=session_id,
            userIdentifier={"userToken": jwt})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    return {"ok": True, "boundUser": _decode_claims(jwt).get("username")}


# ───────────────────────── Static demo data ─────────────────────────

@app.get("/api/customers")
def customers(request: Request):
    """로그인 사용자에 바인딩된 고객만 돌려준다."""
    claims = _verify_jwt(_bearer(request))
    if not claims:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    c = _customer_of(claims)
    return {"customers": [c] if c else []}


@app.get("/api/status")
def status():
    return {
        "region": REGION,
        "memoryId": MEMORY.get("memoryId"),
        "graphId": NEPTUNE.get("graphId"),
        "runtimeArn": RUNTIME.get("runtimeArn"),
        "streamName": STREAM.get("streamName"),
        "strategies": MEMORY.get("strategies", []),
    }


# ───────────────────────── Graph endpoints ─────────────────────────

def _limited(r: dict) -> dict | None:
    """MCP가 tier 제한/미바인딩 응답을 주면 UI용 안내 dict, 아니면 None."""
    if r.get("tier_limited") or r.get("error"):
        return {"tier_limited": bool(r.get("tier_limited")),
                "notice": r.get("error") or r.get("hint") or "", "your_tier": r.get("your_tier")}
    return None


def _profile_data(cid: str, jwt: str):
    """사용자 명의로 Neptune MCP get_customer_profile → UI가 기대하는 /api/profile 형식으로 정규화."""
    res = _gw_call("get_customer_profile", {}, jwt)
    if (err := _gw_http(res, cid)):
        return err
    r = res["result"]
    if (lim := _limited(r)):
        return {"customer": {}, "traits": [], "purchases": [], **lim}
    traits = [{"rel": t.get("relation"), "trait": t.get("trait"), "kind": t.get("kind"),
               "confidence": t.get("confidence"), "source": t.get("source"),
               "updated_at": t.get("updated_at")} for t in r.get("traits", [])]
    return {"customer": r.get("customer") or {}, "traits": traits,
            "purchases": r.get("purchases", []), "avoid_allergens": r.get("avoid_allergens", []),
            "via": "통합 Gateway → Neptune MCP(VPC B) · 사용자 위임 토큰 graph/read"}


@app.get("/api/profile/{cid}")
def profile(cid: str, request: Request):
    if (err := _guard(request, cid)):
        return err
    return _profile_data(cid, _bearer(request))


@app.get("/api/recommendations/{cid}")
def recommendations(cid: str, request: Request):
    if (err := _guard(request, cid)):
        return err
    # 에이전트의 recommend_for_me와 같은 툴·같은 점수 — tier에 따라 개인화/인기상품
    res = _gw_call("get_personalized_candidates", {"limit": 12}, _bearer(request))
    if (err := _gw_http(res, cid)):
        return err
    r = res["result"]
    if (lim := _limited(r)):
        return {"candidates": [], **lim}
    return {"candidates": r.get("candidates", []), "personalized": r.get("personalized"),
            "tier": r.get("tier")}


@app.get("/api/graph/{cid}")
def graph(cid: str, request: Request):
    """시각화용 ego graph — 데이터팀 MCP의 get_graph_view(고정 쿼리, 바인딩된 고객만)."""
    if (err := _guard(request, cid)):
        return err
    res = _gw_call("get_graph_view", {}, _bearer(request))
    if (err := _gw_http(res, cid)):
        return err
    r = res["result"]
    if (lim := _limited(r)):
        return {"nodes": [], "edges": [], **lim}
    return {"nodes": r.get("nodes", []), "edges": r.get("edges", [])}


# ────────────────── Persona summary (LLM, cached) ──────────────────

_summary_cache: dict[str, tuple[str, str]] = {}  # cid -> (traits_key, text)

SUMMARY_PROMPT = """당신은 쇼핑몰의 고객 인사이트 분석가다. 아래는 한 고객의
개인화 그래프 데이터다. 이 고객이 어떤 사람이고 무엇을 챙겨줘야 하는지
4~5문장으로 요약하라.

규칙:
- 자연스러운 한국어 서술형으로. 목록/헤더 없이 문장만.
- 식이 제약(알러지 등)이 있으면 반드시 언급하고 "피해야 한다"를 분명히.
- 최근 대화에서 새로 알게 된 성향(source=memory)이 있으면
  "최근 대화에서 ~을 알게 되었다" 식으로 구분해서 언급.
- 데이터에 없는 내용을 지어내지 마라.

[고객]
%s

[성향 (관계, 대상, 신뢰도, 출처, 갱신시각)]
%s

[최근 구매]
%s"""


def _persona_summary(cid: str, jwt: str) -> str:
    profile_data = _profile_data(cid, jwt)
    if isinstance(profile_data, JSONResponse):
        raise RuntimeError(profile_data.body.decode()[:200])
    if profile_data.get("tier_limited") or profile_data.get("notice"):
        return profile_data.get("notice") or "개인화 프로필에 접근할 수 없는 등급입니다."
    traits = profile_data["traits"]
    # traits가 그대로면 캐시 재사용 (LLM 호출 절약)
    key = json.dumps([(t["rel"], t["trait"], t["confidence"]) for t in traits],
                     ensure_ascii=False, sort_keys=True)
    cached = _summary_cache.get(cid)
    if cached and cached[0] == key:
        return cached[1]

    cust = profile_data["customer"]
    trait_lines = "\n".join(
        f"- {t['rel']} {t['trait']} (신뢰도 {t['confidence']}, {t['source']}, {t.get('updated_at') or '-'})"
        for t in traits) or "(없음)"
    buy_lines = "\n".join(
        f"- {p['name']} ({p['date']})" for p in profile_data["purchases"][:8]) or "(없음)"
    resp = _c("bedrock-runtime").converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": SUMMARY_PROMPT % (
            json.dumps(cust, ensure_ascii=False), trait_lines, buy_lines)}]}],
        inferenceConfig={"maxTokens": 600, "temperature": 0.3},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    _summary_cache[cid] = (key, text)
    return text


@app.get("/api/summary/{cid}")
def summary(cid: str, request: Request):
    if (err := _guard(request, cid)):
        return err
    try:
        return {"summary": _persona_summary(cid, _bearer(request))}
    except Exception as e:  # noqa: BLE001
        return {"summary": "", "error": str(e)[:300]}


# ───────────────────────── Memory endpoints ─────────────────────────

def _long_term_records(cid: str) -> list[dict]:
    out = []
    for ns in (f"/preferences/{cid}", f"/facts/{cid}"):
        try:
            r = _c("bedrock-agentcore").list_memory_records(
                memoryId=MEMORY["memoryId"], namespace=ns, maxResults=50)
        except Exception:
            continue
        for rec in r.get("memoryRecordSummaries", []):
            out.append({
                "namespace": ns,
                "recordId": rec.get("memoryRecordId"),
                "text": (rec.get("content") or {}).get("text", ""),
                "createdAt": rec.get("createdAt"),
            })
    return out


@app.get("/api/memory/{cid}")
def memory(cid: str, request: Request, session_id: str = ""):
    if (err := _guard(request, cid)):
        return err
    events = []
    try:
        sessions = _c("bedrock-agentcore").list_sessions(
            memoryId=MEMORY["memoryId"], actorId=cid, maxResults=20).get("sessionSummaries", [])
    except Exception:
        sessions = []
    sids = [session_id] if session_id else [s["sessionId"] for s in sessions]
    for sid in sids:
        try:
            r = _c("bedrock-agentcore").list_events(
                memoryId=MEMORY["memoryId"], actorId=cid, sessionId=sid,
                includePayloads=True, maxResults=50)
        except Exception:
            continue
        for ev in r.get("events", []):
            for blk in ev.get("payload", []):
                conv = blk.get("conversational")
                if conv:
                    events.append({
                        "sessionId": sid, "ts": ev.get("eventTimestamp"),
                        "role": conv.get("role"),
                        "text": (conv.get("content") or {}).get("text", ""),
                    })
                    continue
                # 에이전트 답변은 blob(장기기억 추출 비대상)으로 저장됨 — 이력 표시엔 포함
                blob = blk.get("blob")
                if blob:
                    try:
                        d = json.loads(blob) if isinstance(blob, str) else blob
                        if isinstance(d, dict) and d.get("text"):
                            events.append({"sessionId": sid, "ts": ev.get("eventTimestamp"),
                                           "role": d.get("role", "ASSISTANT"), "text": d["text"],
                                           "ltm_excluded": True})
                    except ValueError:
                        pass
    events.sort(key=lambda e: str(e["ts"]))
    return {"shortTerm": events, "longTerm": _long_term_records(cid)}


# ─────────────── Memory → Neptune sync pipeline (SSE) ───────────────

def _attribute_vocab() -> list[str]:
    """카탈로그의 Attribute 어휘 — 추출 trait를 이 어휘로 정규화해야
    추천 조인(Trait.name = Attribute.name)에 걸린다."""
    try:
        return list(_mcp_write("list_attribute_vocab", {}))
    except Exception:  # noqa: BLE001
        return []


EXTRACT_PROMPT = """다음은 쇼핑 고객에 대해 메모리 시스템이 추출한 기록이다.
이 기록에서 개인화 그래프에 넣을 (관계, 대상) 쌍을 뽑아라.

관계 타입 (이 중에서만):
- PREFERS: 속성/맛/브랜드 선호 (예: 저당, 매운맛, 친환경, 디카페인)
- INTERESTED_IN: 활동/주제 관심사 (예: 캠핑, 홈카페, 필라테스)
- HAS_CONSTRAINT: 식이 제약 (예: 유당불내증, 견과 알러지, 글루텐 민감)
- HAS_LIFESTYLE: 생활 상황 (예: 육아, 1인 가구, 재택근무)
- OWNS_PET: 반려동물 (예: 고양이, 강아지)

PREFERS의 target은 아래 [상품 속성 어휘]에 있는 단어와 의미가 통하면
반드시 그 어휘를 그대로 써라 (예: "식물성 위주" → "비건",
"단백질 챙김" → "고단백"). 어휘에 없는 선호는 짧은 명사로 새로 만들어도 된다.

[상품 속성 어휘]
%s

target은 반드시 짧은 한국어 명사(구)로. 기록에 없는 내용을 지어내지 마라.
JSON 배열만 출력하라. 뽑을 것이 없으면 [].
형식: [{"type": "PREFERS", "target": "저당", "kind": "Preference", "confidence": 0.8}]
kind는 type에 맞춰: Preference|Interest|DietaryConstraint|Lifestyle|Lifestyle

기록:
%s"""

KIND_BY_TYPE = {
    "PREFERS": "Preference", "INTERESTED_IN": "Interest",
    "HAS_CONSTRAINT": "DietaryConstraint", "HAS_LIFESTYLE": "Lifestyle",
    "OWNS_PET": "Lifestyle",
}


def _extract_facts(records: list[dict]) -> list[dict]:
    """LLM pass: memory records → normalized graph facts."""
    if not records:
        return []
    body = "\n".join(f"- [{r['namespace']}] {r['text']}" for r in records)
    vocab = ", ".join(_attribute_vocab())
    resp = _c("bedrock-runtime").converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": EXTRACT_PROMPT % (vocab, body)}]}],
        inferenceConfig={"maxTokens": 1500, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    facts = []
    for f in json.loads(m.group(0)):
        t = f.get("type")
        if t in KIND_BY_TYPE and f.get("target"):
            facts.append({
                "type": t,
                "target": str(f["target"]).strip(),
                "kind": KIND_BY_TYPE[t],
                "confidence": float(f.get("confidence", 0.7)),
            })
    return facts


def _upsert_fact(cid: str, fact: dict, record_id: str = "") -> dict:
    """MERGE one Customer→Trait edge (source=memory) with provenance — 데이터팀 Neptune MCP의
    쓰기 툴을 **시스템 신원**(graph/write)으로 호출. 사용자 토큰을 흉내 내지 않는다."""
    r = _mcp_write("merge_customer_trait", {
        "customer_id": cid, "relation": fact["type"], "target": fact["target"],
        "kind": fact["kind"], "confidence": fact["confidence"], "memory_record_id": record_id})
    return {"existed": bool(r.get("existed")), "previous": r.get("previous")}


# 메모리 유래 취향(선호+관심사)은 고객당 이 개수까지만 유지한다.
# 초과하면 confidence 낮은 순 → 오래된 순으로 정리해서, 대화가 쌓여도
# 그래프가 무한정 늘어나지 않게 한다. (제약/라이프스타일/반려동물은
# 안전·맥락 정보라 제외)
MAX_MEMORY_TRAITS = 10


def _prune_traits(cid: str) -> list[dict]:
    """Keep only the top-N memory-derived PREFERS/INTERESTED_IN edges (MCP 쓰기 툴, 시스템 신원).
    Returns the pruned (removed) traits."""
    return list(_mcp_write("prune_customer_traits", {"customer_id": cid, "keep": MAX_MEMORY_TRAITS}).get("pruned", []))


def _delete_by_record(record_id: str) -> int:
    """Propagate a MemoryRecordDeleted event: remove trait edges whose
    provenance points at the deleted record (MCP 쓰기 툴, 시스템 신원)."""
    return int(_mcp_write("delete_traits_by_record", {"memory_record_id": record_id}).get("removed", 0))


@app.post("/api/sync/{cid}")
async def sync(cid: str, request: Request):
    if (err := _guard(request, cid)):
        return err
    """Stream the memory→Neptune pipeline as SSE:
    read long-term records → LLM-normalize to graph facts → MERGE edges."""
    loop = asyncio.get_event_loop()

    async def gen():
        yield _sse("start", {"customer_id": cid})
        records = await loop.run_in_executor(None, lambda: _long_term_records(cid))
        yield _sse("records", {"count": len(records), "records": records})
        if not records:
            yield _sse("done", {"synced": 0, "message": "장기 기억 레코드가 아직 없습니다. 대화 후 1~2분 뒤 추출됩니다."})
            return
        facts = await loop.run_in_executor(None, lambda: _extract_facts(records))
        yield _sse("facts", {"count": len(facts), "facts": facts})
        synced = 0
        for fact in facts:
            res = await loop.run_in_executor(None, lambda f=fact: _upsert_fact(cid, f))
            synced += 1
            yield _sse("edge", {**fact, **res})
        yield _sse("done", {"synced": synced})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ────────── Real-time projection: Kinesis consumer + live SSE ──────────
#
# AgentCore Memory pushes MemoryRecordCreated/Updated/Deleted events to
# Kinesis the moment async extraction finishes. The consumer below:
#   1. tails all shards (LATEST),
#   2. LLM-normalizes each record text into graph facts,
#   3. MERGEs Customer→Trait edges (provenance = memory_record_id),
#   4. broadcasts every step to per-customer SSE subscribers,
# so the UI shows "지금 이 성향을 파악했어요" while the chat continues.

import time as _time_mod

_live_subs: dict[str, list[asyncio.Queue]] = {}
_seen_records: set[str] = set()  # idempotency vs Kinesis at-least-once
# 같은 (고객, 관계, 대상) 토스트를 짧은 시간 안에 반복 전송하지 않기 위한
# 디듀프 창 — 두 추출 전략이 같은 성향을 각자 레코드로 만들기 때문.
_recent_toasts: dict[tuple, float] = {}
_TOAST_DEDUP_SEC = 300


def _broadcast(cid: str, event: str, data: dict) -> None:
    for q in _live_subs.get(cid, []):
        q.put_nowait((event, data))


def _cid_from_namespaces(namespaces: list[str]) -> str:
    # namespaces look like /facts/cust-yuna or /preferences/cust-yuna
    for ns in namespaces:
        parts = ns.strip("/").split("/")
        if len(parts) >= 2:
            return parts[-1]
    return ""


def _handle_stream_event(loop: asyncio.AbstractEventLoop, ev: dict) -> None:
    etype = ev.get("eventType", "")
    rid = ev.get("memoryRecordId", "")
    cid = _cid_from_namespaces(ev.get("namespaces", []))

    if etype == "MemoryRecordDeleted":
        n = _delete_by_record(rid)
        if cid:
            loop.call_soon_threadsafe(
                _broadcast, cid, "trait_removed", {"record_id": rid, "removed": n})
        return

    if etype not in ("MemoryRecordCreated", "MemoryRecordUpdated"):
        return
    if not cid or not rid or rid in _seen_records:
        return
    _seen_records.add(rid)

    text = ev.get("memoryRecordText", "")
    strategy = ev.get("memoryStrategyType", "")
    loop.call_soon_threadsafe(_broadcast, cid, "record", {
        "record_id": rid, "text": text, "strategy": strategy,
    })

    ns = ev.get("namespaces", [""])[0]
    facts = _extract_facts([{"namespace": ns, "text": text}])
    for fact in facts:
        res = _upsert_fact(cid, fact, record_id=rid)
        # 토스트는 그래프에 없던 새 trait이거나 신뢰도가 크게 오른 경우만. 에이전트가 프로필을 되풀이하면
        # Memory가 같은 사실을 매 턴 재추출하므로 MERGE(provenance 갱신)는 하되 알리지 않는다.
        prev_conf = (res.get("previous") or {}).get("confidence")
        is_new = not res.get("existed")
        bumped = (prev_conf is not None and fact["confidence"] - float(prev_conf) >= 0.15)
        if not (is_new or bumped):
            continue
        key = (cid, fact["type"], fact["target"])
        now = _time_mod.time()
        if now - _recent_toasts.get(key, 0) < _TOAST_DEDUP_SEC:
            continue
        _recent_toasts[key] = now
        loop.call_soon_threadsafe(_broadcast, cid, "trait", {
            **fact, **res, "record_id": rid, "record_text": text,
        })
    if facts:
        pruned = _prune_traits(cid)
        for row in pruned:
            loop.call_soon_threadsafe(_broadcast, cid, "trait_pruned", {
                "type": row["rel"], "target": row["name"],
                "confidence": row["confidence"],
            })


def _consume_kinesis(loop: asyncio.AbstractEventLoop) -> None:
    """Blocking shard-tail loop; runs in a daemon thread."""
    import time as _time
    name = STREAM.get("streamName")
    if not name:
        return
    kin = boto3.client("kinesis", region_name=REGION)
    iterators: dict[str, str] = {}
    while True:
        try:
            if not iterators:
                for sh in kin.list_shards(StreamName=name)["Shards"]:
                    iterators[sh["ShardId"]] = kin.get_shard_iterator(
                        StreamName=name, ShardId=sh["ShardId"],
                        ShardIteratorType="LATEST")["ShardIterator"]
            for sid, it in list(iterators.items()):
                r = kin.get_records(ShardIterator=it, Limit=100)
                iterators[sid] = r["NextShardIterator"]
                for rec in r["Records"]:
                    try:
                        body = json.loads(rec["Data"])
                        ev = body.get("memoryStreamEvent") or body
                        _handle_stream_event(loop, ev)
                    except Exception:  # noqa: BLE001
                        pass
            _time.sleep(2)
        except Exception:  # noqa: BLE001
            iterators.clear()
            _time.sleep(10)


@app.on_event("startup")
async def _start_consumer():
    import threading
    loop = asyncio.get_event_loop()
    threading.Thread(target=_consume_kinesis, args=(loop,), daemon=True).start()


@app.get("/api/live/{cid}")
async def live(cid: str, request: Request, token: str = ""):
    """Per-customer SSE feed of real-time trait discoveries.
    (EventSource는 Authorization 헤더를 못 붙여 ?token= 로 받는다)"""
    if (err := _guard(request, cid, token)):
        return err
    q: asyncio.Queue = asyncio.Queue()
    _live_subs.setdefault(cid, []).append(q)

    async def gen():
        yield _sse("start", {"customer_id": cid})
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse(event, data)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", {})
        finally:
            subs = _live_subs.get(cid, [])
            if q in subs:
                subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ───────────────────────── Chat (SSE) ─────────────────────────

@app.post("/api/chat")
async def chat(body: dict, request: Request):
    query = (body.get("query") or "").strip()
    session_id = body.get("session_id") or f"s-{uuid.uuid4().hex[:10]}"
    jwt = _bearer(request)
    claims = _verify_jwt(jwt)
    if not claims:
        return JSONResponse(
            {"error": "로그인이 필요합니다 — Authorization: Bearer <유효한 JWT>"},
            status_code=401)
    # 고객은 요청 body가 아니라 JWT sub 바인딩으로 결정 (런타임도 같은 규칙으로 재확인)
    mine = _customer_of(claims)
    cid = mine["customer_id"] if mine else f"user-{claims.get('sub', '')[:8]}"

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _invoke():
        """③ Runtime을 사용자 JWT(Bearer)로 직접 호출 — SigV4가 아니라
        사용자의 토큰이 인증 수단이다 (Runtime의 customJWT authorizer가 검증)."""
        try:
            arn = urllib.parse.quote(RUNTIME["runtimeArn"], safe="")
            url = (f"https://bedrock-agentcore.{REGION}.amazonaws.com"
                   f"/runtimes/{arn}/invocations?qualifier=DEFAULT")
            rt_session = f"{cid}-{session_id}".ljust(33, "0")
            with httpreq.post(
                url,
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": rt_session,
                },
                data=json.dumps({"query": query, "customer_id": cid,
                                 "session_id": session_id}),
                stream=True, timeout=(10, 300),
            ) as resp:
                if resp.status_code != 200:
                    loop.call_soon_threadsafe(q.put_nowait, {
                        "type": "error",
                        "message": f"Runtime HTTP {resp.status_code}: {resp.text[:300]}"})
                    return
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    data = raw[len("data:"):].strip()
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        ev = {"type": "chunk", "text": data}
                    if not isinstance(ev, dict):
                        ev = {"type": "chunk", "text": str(ev)}
                    loop.call_soon_threadsafe(q.put_nowait, ev)
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(q.put_nowait,
                                      {"type": "error", "message": str(e)[:500]})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, {"type": "_eof"})

    async def gen():
        yield _sse("start", {"session_id": session_id, "customer_id": cid})
        parts = jwt.split(".")
        yield _sse("debug", {
            "step": "②", "title": "BFF가 사용자 JWT 검증 + 고객 바인딩",
            "detail": "브라우저가 hosted UI(SSO)로 받은 access token을 Authorization 헤더로 보냈습니다. "
                      "BFF는 Agent IdP JWKS로 서명·발급자·client_id를 검증하고, 토큰의 sub로 "
                      f"이 사용자의 고객 프로필({cid})을 결정합니다 — 요청 body의 customer_id는 믿지 않습니다.",
            "claims": _decode_claims(jwt),
            "token_preview": f"{jwt[:20]}...{jwt[-10:]}",
            "data": {
                "수신 헤더": "Authorization: Bearer <JWT>",
                "JWT 구조": f"header({len(parts[0])}) . payload({len(parts[1]) if len(parts) > 1 else 0}) . signature({len(parts[2]) if len(parts) > 2 else 0})",
                "BFF 검증": "JWKS 서명 · iss(Agent IdP) · client_id · token_use=access",
                "고객 바인딩": f"sub {claims.get('sub', '')[:8]}… → 고객 {cid}",
                "토큰 변형": "없음 — 같은 JWT를 Runtime으로 전달",
            },
            "code": "claims = jwt.decode(token, jwks_key, issuer=AGENT_IDP)   # BFF 검증\n"
                    "cid = bound_customer(claims['sub'])                     # body 값 무시",
        })
        yield _sse("debug", {
            "step": "③", "title": "Runtime 호출 (Bearer, SigV4 아님)",
            "detail": "쇼핑 에이전트 Runtime을 사용자 JWT로 호출합니다. "
                      "AWS 자격증명(SigV4)이 아니라 사용자 토큰이 인증 수단입니다. "
                      "payload의 customer_id는 참고값일 뿐 — Runtime도 JWT sub로 고객을 다시 결정합니다.",
            "token_preview": f"{jwt[:20]}...{jwt[-10:]}",
            "data": {
                "요청": "POST /runtimes/{runtimeArn}/invocations?qualifier=DEFAULT",
                "인증 헤더": "Authorization: Bearer <사용자 JWT>",
                "세션 헤더": "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id (33자+)",
                "페이로드": json.dumps({"query": "…", "customer_id": cid,
                                        "session_id": session_id}, ensure_ascii=False),
                "네트워크": "Runtime은 VPC A(networkMode=VPC) — 인바운드는 공용 invocations, egress는 PrivateLink만",
            },
        })
        task = loop.run_in_executor(None, _invoke)
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                yield _sse("heartbeat", {})
                continue
            t = ev.get("type")
            if t == "_eof":
                break
            if t == "chunk":
                yield _sse("chunk", {"text": ev.get("text", "")})
            elif t == "debug":
                yield _sse("debug", ev)
            elif t == "auth_url":
                # 위키(3rd-party) 3LO 동의 URL — UI가 동의 버튼을 렌더
                yield _sse("auth_url", ev)
            elif t == "final":
                yield _sse("final", ev)
            elif t == "error":
                yield _sse("error", ev)
        await task
        yield _sse("done", {"session_id": session_id})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
