"""ShoppingAgent — AgentCore Runtime entrypoint (Strands). 인바운드 사용자 JWT로 통합 Gateway의
Neptune/위키 MCP 툴을 호출하고(OBO 체인), 대화를 AgentCore Memory에 기록한다.
요청 {"query", "customer_id", "session_id"} → SSE 스트림 {"type": chunk | debug | auth_url | error | final}.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.request

from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from strands import Agent, tool
from strands.models import BedrockModel

import aws_session  # 프로세스 공용 boto3 Session — VPC 모드에서는 자격증명 해석이 느려 1회만 해석
import memory_client
import neptune_gateway
import wiki_gateway

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [ShoppingAgent] %(levelname)s %(message)s")
LOG = logging.getLogger("ShoppingAgent")

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID",
                          "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

app = BedrockAgentCoreApp()

# per-invocation state (runtime 컨테이너는 세션당 단일 실행)
_current = {"customer_id": "", "jwt": ""}
_consent_shown: dict[str, str] = {}   # 턴당 서비스별 동의 카드 1장 — {"neptune": url, "wiki": url}

# 고객 바인딩 {마켓 sub: customer_id} — 어느 고객의 기억·그래프를 쓰는지는 클라이언트가 보낸
# customer_id가 아니라 JWT의 sub로 결정한다(위키·Neptune MCP와 같은 원칙). deploy_runtime.py가 env로 주입.
CUSTOMER_MAP: dict[str, str] = json.loads(os.environ.get("CUSTOMER_MAP", "{}"))
_tool_events: list[dict] = []
_debug_events: list[dict] = []
_debug_lock = threading.Lock()


def _push_debug(step: str, title: str, detail: str, **extra) -> None:
    with _debug_lock:
        _debug_events.append({"type": "debug", "step": step,
                              "title": title, "detail": detail, **extra})


def _push_event(ev: dict) -> None:
    """debug 외의 스트림 이벤트(예: auth_url)도 같은 큐로 내보낸다."""
    with _debug_lock:
        _debug_events.append(ev)


def _drain_debug() -> list[dict]:
    with _debug_lock:
        out = list(_debug_events)
        _debug_events.clear()
    return out


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {k: claims.get(k) for k in
                ("sub", "username", "cognito:groups", "client_id", "scope",
                 "iss", "token_use")
                if k in claims}
    except Exception:  # noqa: BLE001
        return {}


def _jwt_header(token: str) -> dict:
    """JWT 헤더(kid, alg) — JWKS에서 어떤 공개키를 고를지 정하는 실데이터."""
    try:
        h = token.split(".")[0]
        h += "=" * (-len(h) % 4)
        return json.loads(base64.urlsafe_b64decode(h))
    except Exception:  # noqa: BLE001
        return {}


_jwks_cache: dict = {}
_jwks_lock = threading.Lock()
_jwks_inflight: set = set()


def _jwks_fetch_bg(iss: str) -> None:
    """JWKS를 백그라운드에서 1회 가져와 캐시한다 — 호출 경로를 막지 않는다.
    VPC 모드에서는 인터넷이 없어 공용 JWKS 조회가 실패할 수 있으므로 짧은 timeout으로 시도하고 실패도 캐시한다."""
    try:
        with urllib.request.urlopen(f"{iss}/.well-known/jwks.json", timeout=3) as r:
            data = json.loads(r.read())
        with _jwks_lock:
            _jwks_cache[iss] = data
    except Exception as e:  # noqa: BLE001
        with _jwks_lock:
            _jwks_cache[iss] = {"_error": str(e)[:120]}
    finally:
        with _jwks_lock:
            _jwks_inflight.discard(iss)


def _jwks_match(token: str) -> dict:
    """authorizer의 검증 과정을 표시용으로 재구성: jwks_uri → 토큰 헤더의 kid와 일치하는
    공개키를 찾는다. (캐시된 결과만 사용, 네트워크는 백그라운드)"""
    iss = _jwt_claims(token).get("iss", "")
    kid = _jwt_header(token).get("kid", "")
    if not iss or not kid:
        return {}
    with _jwks_lock:
        cached = _jwks_cache.get(iss)
        if cached is None and iss not in _jwks_inflight:
            _jwks_inflight.add(iss)
            threading.Thread(target=_jwks_fetch_bg, args=(iss,), daemon=True).start()
    out = {"jwks_uri": f"{iss}/.well-known/jwks.json", "token_kid": kid}
    if cached is None:
        out["JWKS 조회"] = "백그라운드 진행 중 (다음 호출에 반영) — authorizer는 이미 검증 완료"
    elif "_error" in cached:
        out["JWKS 조회"] = f"생략 — VPC 격리로 공용 조회 불가({cached['_error'][:40]}) · authorizer가 검증"
    else:
        for key in cached.get("keys", []):
            if key.get("kid") == kid:
                out["matched_key"] = {"kty": key.get("kty"), "alg": key.get("alg"),
                                      "use": key.get("use"),
                                      "n(공개키 모듈러스)": (key.get("n") or "")[:24] + "…"}
    return out


REVOCATION_BUCKET = os.environ.get("REVOCATION_BUCKET", "")
REVOCATION_PREFIX = os.environ.get("REVOCATION_PREFIX", "revocations/")
_s3_client = None


def _jwt_revoked(token: str) -> bool:
    """마켓 access 토큰이 revoke 목록(S3 `revocations/<sub>.json`)에 있고 iat <= revoked_at 이면 True.
    PrivateLink에서 Cognito GetUser는 호출 불가 → S3 revoke 마커로 판정. 마커 없음/그 외 오류 → False(fail-open)."""
    global _s3_client
    if not REVOCATION_BUCKET:
        return False
    try:
        c = _jwt_claims(token)
        sub, iat = c.get("sub"), int(_jwt_claims_raw(token).get("iat", 0))
        if not sub:
            return False
        if _s3_client is None:
            _s3_client = aws_session.client("s3")
        try:
            obj = _s3_client.get_object(Bucket=REVOCATION_BUCKET, Key=f"{REVOCATION_PREFIX}{sub}.json")
        except _s3_client.exceptions.NoSuchKey:
            return False
        return iat <= int(json.loads(obj["Body"].read()).get("revoked_at", 0))
    except Exception as e:  # noqa: BLE001
        LOG.warning("revocation check skipped: %s", str(e)[:120])
        return False


def _jwt_claims_raw(token: str) -> dict:
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def _token_preview(token: str) -> str:
    return f"{token[:20]}...{token[-10:]}" if len(token) > 40 else token


def _record(name: str, args: dict, result_preview: str) -> None:
    _tool_events.append({"tool": name, "args": args,
                         "preview": result_preview[:300]})


NEPTUNE_SCOPES = os.environ.get("NEPTUNE_SCOPES", "openid graph/read").split()


def _emit_neptune_consent(url: str) -> None:
    _push_debug("❷", "개인화 위임 토큰 없음 → 데이터팀 SSO 동의 필요 (최초 1회)",
                "통합 Gateway가 Token Vault에서 이 사용자의 Neptune(데이터팀) "
                "위임 토큰을 찾지 못해, MCP URL elicitation(-32042)으로 동의 URL을 "
                "돌려줬습니다. 위키와 같은 단일 Tool IdP를 쓰지만 scope가 다르므로"
                "(graph/read) 개인화 접근에도 최초 1회 동의가 필요합니다 — 로그인은 "
                "마켓 계정(SSO)으로 진행되어, 마켓의 bob은 데이터팀에서도 반드시 "
                "bob입니다. 이후엔 볼트의 refresh로 무동의.",
                tool="neptune",
                data={
                    "동의 URL 호스트": url.split("/identities")[0].split("//")[-1][:50],
                    "OAuth2 프로바이더": "OboDownstreamProvider (단일 Tool IdP)",
                    "요청 scope": " ".join(NEPTUNE_SCOPES),
                    "elicitation": "MCP 2025-11-25 URL elicitation (JSON-RPC -32042)",
                    "로그인 방식": "마켓 IdP SSO (OIDC 페더레이션, 로컬 계정 차단)",
                })
    _push_event({"type": "auth_url", "url": url,
                 "service": "온톨로 개인화(데이터팀)",
                 "message": "개인화 데이터 접근 위임에 동의해 주세요 — 로그인은 마켓 계정(SSO)으로 진행됩니다 (최초 1회)"})


def _neptune_await_consent(jwt: str, timeout: int = 150) -> bool:
    """동의 URL 발행 후, 볼트에 위임 토큰이 들어올 때까지 whoami로 폴링."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        probe = neptune_gateway.call("neptune___whoami", {}, jwt)
        if probe["status"] == "ok":
            return True
        if probe["status"] == "error":
            return False
    return False


def _emit_neptune_result_debug(tool_name: str, body) -> None:
    """MCP 툴이 본 신원 + tier(=LDAP/X-Tier) 판정을 디버그로 노출."""
    if not isinstance(body, dict):
        return
    caller = body.get("_caller") or {}
    tier_info = caller.get("tier") or {}
    tier = tier_info.get("value") or body.get("tier") or "?"
    if body.get("tier_limited"):
        _push_debug("⑥", "MCP 툴 — tier 데이터 차등 (basic → 제한)",
                    "MCP 서버가 게이트웨이 interceptor가 주입한 X-Tier 헤더를 "
                    "확인했습니다. tier의 진실원은 토큰이 아니라 별도 VPC의 LDAP "
                    "엔타이틀먼트 디렉터리입니다 — 이 사용자는 basic 등급이라 "
                    "개인화 프로필/취향 기반 추천 대신 제한된 데이터만 받습니다. "
                    "정책엔진(Cedar)은 그래프 '행'을 볼 수 없어 이 데이터 차등은 "
                    "데이터 옆(MCP)에서 결정됩니다.",
                    tool=tool_name, level="warn",
                    data={
                        "your_tier": body.get("your_tier", tier),
                        "required_tier": body.get("required_tier", "premium"),
                        "tier 진실원": body.get("tier_source", "X-Tier (LDAP)"),
                    })
        return
    _push_debug("⑥", f"MCP 툴 — 위임 신원 검증 + tier 데이터 차등 ({tier})",
                "Neptune MCP(데이터팀 VPC B)가 Tool IdP 위임 토큰을 검증하고"
                "(iss=Tool IdP, scope=graph/read), 게이트웨이 interceptor가 LDAP "
                "조회로 주입한 X-Tier로 데이터를 결정했습니다. 원본 마켓 토큰이 "
                "아니라 사용자 명의로 교환된 위임 토큰이 백엔드까지 도달합니다.",
                claims=caller.get("claims"),
                token_preview=caller.get("token_preview"),
                tool=tool_name,
                data={
                    "tier": tier,
                    "tier 진실원": tier_info.get("source", "X-Tier (LDAP)"),
                    "personalized": body.get("personalized"),
                    "툴이 본 사용자(위임)": (caller.get("claims") or {}).get("username"),
                    "granted scope": (caller.get("claims") or {}).get("scope"),
                    "판정": "premium → 개인화 데이터" if tier == "premium" else "basic → 비개인화(인기) 데이터",
                },
                code=("# mcp_server/server.py — tier는 토큰이 아니라 X-Tier(LDAP)\n"
                      "tier = _http_headers(ctx)['X-Tier']  # interceptor가 LDAP 조회 후 주입\n"
                      "if tier != 'premium':\n"
                      "    return generic_popular()  # 비개인화 데이터"))


def _neptune_op(tool_key: str, args: dict) -> str | None:
    """통합 Gateway(MCP) 경유 Neptune 개인화 툴 호출 — 위임 토큰(3LO) + X-Tier.
    불가하면 None (실행 role IAM 직접 조회 폴백). 동의 시간초과는 에러 JSON 반환
    (개인화를 IAM 폴백으로 우회 제공하지 않음)."""
    jwt = _current["jwt"]
    if not (neptune_gateway.available() and jwt):
        return None
    full = f"neptune___{tool_key}"
    _push_debug("⑤", "통합 Gateway 호출 — interceptor(X-Tier) + 위임 토큰 부착",
                f"에이전트가 사용자 JWT로 통합 Gateway의 Neptune MCP 툴 "
                f"'{full}'을 호출합니다. ① Gateway가 Agent IdP JWKS로 재검증, "
                f"② REQUEST interceptor가 sub로 LDAP을 조회해 X-Tier 주입(스푸핑 "
                f"불가), ③ Token Vault의 Tool IdP 위임 토큰(scope graph/read)을 "
                f"Authorization으로 붙여 데이터팀 VPC(B)의 MCP를 호출합니다. "
                f"어느 고객의 데이터인지는 인자가 아니라 위임 토큰의 신원(MarketSSO_<sub>)으로 MCP가 결정합니다.",
                token_preview=_token_preview(jwt), tool=tool_key,
                data={
                    "호출 인자": json.dumps(args, ensure_ascii=False)[:120] or "{} (customer_id 인자 없음)",
                    "MCP 메서드": f"tools/call → {full}",
                    "tier 주입": "interceptor Lambda → LDAP 조회 → X-Tier",
                    "고객 결정": "MCP가 위임 토큰 username(MarketSSO_<마켓 sub>) → 고객 매핑 (호출자 지정 불가)",
                    "타깃 자격증명": "OAUTH AUTHORIZATION_CODE (Tool IdP 위임 토큰 · graph/read)",
                    "서버 위치": "데이터팀 VPC(B) · Neptune private endpoint · NAT 없음",
                })
    r = neptune_gateway.call(full, args, jwt)
    if r["status"] == "consent":
        # 한 턴에 Neptune 툴이 여러 번 불려도 동의 카드는 한 장만
        if not _consent_shown.get("neptune"):
            _consent_shown["neptune"] = r["url"]
            _emit_neptune_consent(r["url"])
        if not _neptune_await_consent(jwt):
            _push_debug("❺", "개인화 동의 대기 시간 초과", tool=tool_key, level="warn",
                        detail="사용자가 제한 시간 내에 데이터팀 SSO 동의를 완료하지 "
                               "않았습니다. 화면의 동의 버튼으로 로그인 후 다시 요청해 주세요.")
            return json.dumps({"error": "개인화 동의 대기 시간 초과 — 화면의 동의 버튼으로 "
                                        "로그인한 뒤 다시 요청해 주세요."}, ensure_ascii=False)
        r = neptune_gateway.call(full, args, jwt)  # 동의 완료 후 재호출
    if r["status"] == "error":
        _push_debug("⑤", "Gateway 호출 실패 — 직접 조회 경로 없음 (오류로 응답)",
                    "Neptune은 데이터팀 VPC(B) 사설이라 Gateway 외의 경로가 없습니다. "
                    "IAM/서비스 자격증명으로 우회하지 않고 사용자에게 일시 오류로 안내합니다. "
                    + r.get("text", "")[:160], tool=tool_key, level="warn")
        LOG.warning("neptune gateway call failed: %s", r.get("text", "")[:300])
        return None
    try:
        _emit_neptune_result_debug(tool_key, r.get("result"))
    except Exception:  # noqa: BLE001
        pass
    return r.get("text")


@tool
def search_products(keyword: str) -> str:
    """상품 카탈로그에서 키워드로 상품을 검색한다. 상품명·설명·카테고리·
    브랜드·속성에 대해 부분 일치로 찾는다.

    Args:
        keyword: 검색어 (예: "커피", "비건", "캠핑")
    """
    out = _neptune_op("search_products", {"keyword": keyword, "limit": 10})
    if out is None:
        # Neptune은 데이터팀 VPC(B) private이라 직접 조회 경로가 없다 —
        # 통합 Gateway 경유만 유효. 여기 오면 Gateway가 일시적으로 불가한 것.
        out = json.dumps({"error": "상품 검색 서비스에 일시적으로 연결할 수 없습니다. "
                                   "잠시 후 다시 시도해 주세요."}, ensure_ascii=False)
    _record("search_products", {"keyword": keyword}, out)
    return out


@tool
def recommend_for_me() -> str:
    """현재 고객의 초개인화 그래프(취향·제약·구매이력)를 기반으로 추천
    후보 상품을 가져온다. 각 상품에 매칭된 취향(matched_traits)과 점수가
    포함된다. 알러지/식이 제약에 걸리는 상품과 이미 구매한 상품은 제외됨."""
    cid = _current["customer_id"]
    # customer_id 인자 없음 — MCP가 위임 토큰의 신원으로 고객을 결정한다
    out = _neptune_op("get_personalized_candidates", {"limit": 10})
    if out is None:
        out = json.dumps({"error": "개인화 추천 서비스에 일시적으로 연결할 수 없습니다. "
                                   "잠시 후 다시 시도해 주세요."}, ensure_ascii=False)
    _record("recommend_for_me", {"customer_id": cid}, out)
    return out


@tool
def check_dietary_safety(skus: list[str]) -> str:
    """상품 SKU 목록이 현재 고객의 식이 제약(알러지·유당불내증 등)에
    위배되는지 그래프에서 확인한다. 위배 상품 목록을 반환한다(빈 목록이면 안전).

    Args:
        skus: 확인할 상품 SKU 목록 (예: ["GR-002", "GR-014"])
    """
    out = _neptune_op("check_dietary_safety", {"skus": skus})
    if out is None:
        out = json.dumps({"error": "식이 안전성 확인 서비스에 일시적으로 연결할 수 없습니다. "
                                   "잠시 후 다시 시도해 주세요."}, ensure_ascii=False)
    _record("check_dietary_safety", {"skus": skus}, out)
    return out


# ─────────── 온톨로 위키 (가상 3rd-party, 별도 IdP · 위키팀 VPC C) ───────────
# 에이전트는 위키 API를 직접 부르지 않고 통합 Gateway의 위키 MCP 툴만 사용자 JWT로 호출한다.
# Gateway가 Token Vault의 위키 위임 토큰(3LO)을 붙이며, 없으면 URL elicitation(-32042)으로 동의 URL을 준다.
WIKI_PROVIDER = os.environ.get("OBO_PROVIDER_NAME", "OboDownstreamProvider")
WIKI_SCOPES = os.environ.get("OBO_SCOPES", "openid files/read").split()

_WIKI_TOOL = {("POST", "/notes"): "wiki___save_note",
              ("GET", "/notes"): "wiki___list_notes"}


def _wiki_market_identity() -> tuple[str, str]:
    m = _jwt_claims(_current["jwt"])
    return m.get("sub", ""), m.get("username", "?")


def _emit_wiki_consent(url: str) -> None:
    _push_debug("❷", "위임 토큰 없음 → 위키 SSO 동의 필요 (최초 1회)",
                "통합 Gateway가 Token Vault에서 이 사용자의 위키 위임 토큰을 "
                "찾지 못해, MCP URL elicitation(-32042)으로 동의 URL을 "
                "돌려줬습니다. 위키 IdP는 마켓 IdP와 SSO 페더레이션되어 있어 "
                "별도 위키 비밀번호가 없습니다 — 동의 화면에서 마켓 계정으로 "
                "로그인하면 위키가 그 신원을 그대로 신뢰합니다(JIT). "
                "따라서 마켓의 bob은 위키에서도 반드시 bob입니다.",
                tool="wiki",
                data={
                    "동의 URL 호스트": url.split("/identities")[0].split("//")[-1][:50],
                    "OAuth2 프로바이더": WIKI_PROVIDER,
                    "요청 scope": " ".join(WIKI_SCOPES),
                    "elicitation": "MCP 2025-11-25 URL elicitation (JSON-RPC -32042)",
                    "위키 로그인 방식": "마켓 IdP SSO (OIDC 페더레이션, 로컬 계정 차단)",
                },
                code=("# app/shopping_agent/wiki_gateway.py — 동의 URL 추출\n"
                      "if err['code'] == -32042:\n"
                      "    url = err['data']['elicitations'][0]['url']  # Gateway 3LO"))
    _push_event({"type": "auth_url", "url": url,
                 "service": "온톨로 위키",
                 "message": "온톨로 위키 접근 위임에 동의해 주세요 — 로그인은 마켓 계정(SSO)으로 진행됩니다 (최초 1회)"})


async def _wiki_await_consent(jwt: str, timeout: int = 150) -> dict | None:
    """동의 URL 발행 후, 볼트에 토큰이 들어올 때까지 whoami로 폴링."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        probe = wiki_gateway.call("wiki___whoami", {}, jwt)
        if probe["status"] == "ok":
            return probe
        if probe["status"] == "error":
            return None
    return None


async def _wiki_op(method: str, path: str, body: dict | None = None) -> dict:
    """통합 Gateway 경유 위키 MCP 호출 (신원 가드 포함).
    ① whoami로 볼트 토큰 확인(없으면 동의 URL 발행 후 대기) → ② 위키가 본 사용자(MarketSSO_<마켓 sub>)가
    지금 마켓 사용자와 일치하는지 가드 → ③ 실제 저장/조회 툴 호출."""
    jwt = _current["jwt"]
    if not (wiki_gateway.available() and jwt):
        return {"status": 503, "body": {"error": "위키 Gateway 미배선 또는 로그인 필요"}}
    market_sub, market_user = _wiki_market_identity()
    tool = _WIKI_TOOL[(method, path)]

    _push_debug("❶", "위키 툴 호출 — 통합 Gateway 경유 (내부 MCP)",
                "온톨로 위키는 위키팀 VPC 안의 내부 MCP 서버입니다. 에이전트는 "
                "위키 API를 직접 부르지 않고, 사용자 JWT로 통합 Gateway의 위키 "
                "MCP 툴만 호출합니다. Gateway가 Token Vault에서 이 사용자의 위키 "
                "위임 토큰을 꺼내 붙여 위키 MCP 서버를 호출합니다 — 인터넷 왕복 "
                "없이 AWS 백본/VPC로만 흐릅니다.", tool="wiki",
                data={
                    "볼트 조회 키": f"workload=통합 Gateway × user={market_user}",
                    "MCP 툴": tool,
                    "프로토콜": "MCP 2025-11-25 (tools/call, URL elicitation)",
                    "위키 서버 위치": "위키팀 VPC(C) · AgentCore Runtime(VPC 모드)",
                })

    who = wiki_gateway.call("wiki___whoami", {}, jwt)
    if who["status"] == "consent":
        if not _consent_shown.get("wiki"):  # 턴당 카드 1장
            _consent_shown["wiki"] = who["url"]
            _emit_wiki_consent(who["url"])
        who = await _wiki_await_consent(jwt)
        if who is None:
            _push_debug("❺", "위키 동의 대기 시간 초과",
                        "사용자가 제한 시간 내에 위키 로그인/동의를 완료하지 "
                        "않았습니다. 화면의 동의 버튼으로 로그인 후 다시 "
                        "요청해 주세요.", tool="wiki", level="warn")
            return {"status": 408, "body": {"error": "동의 대기 시간 초과 — 위키 로그인 동의 후 다시 요청해 주세요."}}
    if who["status"] == "error":
        return {"status": 502, "body": {"error": who["text"]}}

    # 신원 가드 — 위키가 본 사용자(MarketSSO_<마켓 sub>)가 지금 마켓 사용자여야 한다.
    # 에이전트는 위키 토큰 자체를 보지 못하므로(Gateway가 보유) 툴 응답의 위키 사용자명으로 대조한다.
    wiki_user = str((who.get("result") or {}).get("username") or "")
    identity_ok = bool(market_sub) and market_sub in wiki_user
    if not identity_ok:
        _push_debug("❹", "위임 토큰 신원 불일치 — 사용 거부 (차단)",
                    "Gateway 볼트에서 나온 위키 토큰의 주인이 지금 마켓에 "
                    "로그인한 사용자와 다릅니다. 다른 마켓 계정으로 동의했거나 "
                    "레거시 토큰인 경우입니다. 에이전트가 토큰을 사용하지 않고 "
                    "위키 작업을 거부합니다 — 남의 노트 영역에 접근하지 못합니다.",
                    tool="wiki", level="warn",
                    data={"마켓 사용자 sub": market_sub,
                          "위키 토큰 username": wiki_user,
                          "기대 형식": f"MarketSSO_{market_sub}"},
                    code=("# app/shopping_agent/main.py — 신원 일치 가드\n"
                          "wiki_user = whoami_result['username']  # 위키가 본 사용자\n"
                          "if market_sub not in wiki_user: 작업 거부 (차단)"))
        return {"status": 403, "body": {"error": "위키 토큰 신원 불일치 — 차단됨(다른 사용자 토큰)."}}

    our_iss = _jwt_claims(_current["jwt"]).get("iss", "?")
    _push_debug("❹", "OBO 위임 토큰 확보 + 신원 일치 확인 (위키 IdP 발급)",
                "Gateway가 볼트의 위키 위임 토큰(위키 IdP 발급, 마켓 JWT와 "
                "발급자가 다름)을 사용합니다. whoami로 위키가 본 사용자가 지금 "
                "마켓 사용자와 같음을 확인했습니다 — 토큰 2개 체제(OBO)이지만 "
                "신원은 하나로 묶여 있습니다.", tool="wiki",
                data={
                    "마켓 IdP iss": our_iss,
                    "위키 토큰 사용자": wiki_user,
                    "granted scope": (who.get("result") or {}).get("scope", "?"),
                    "신원 일치": f"market_sub({market_sub[:8]}…) ∈ {wiki_user}",
                    "갱신 수단": "볼트의 refresh token (Gateway가 자동 갱신)",
                })

    r = wiki_gateway.call(tool, body or {}, jwt)
    if r["status"] == "consent":
        # 방금 whoami는 통과했는데 실제 툴에서 다시 동의를 요구하는 비정상 상태.
        _emit_wiki_consent(r["url"])
        return {"status": 428, "body": {"error": "위키 재동의 필요 — 화면의 버튼으로 로그인 후 다시 요청해 주세요."}}
    if r["status"] == "error":
        return {"status": 502, "body": {"error": r["text"]}}

    resp = r["result"]
    result_bits = {}
    if isinstance(resp, dict):
        if resp.get("saved"):
            result_bits = {
                "저장된 노트": f"'{resp['saved'].get('title')}' (id {resp['saved'].get('id')})",
                "작성자(위키가 기록)": resp["saved"].get("author"),
                "이 사용자의 총 노트": resp.get("total_notes"),
            }
        elif "notes" in resp:
            result_bits = {"조회된 노트 수": len(resp.get("notes") or []),
                           "소유자": resp.get("user")}
    ok = not r.get("isError")
    _push_debug("❺", f"위키 MCP {'저장/조회 완료' if ok else '오류'} — 위키팀 VPC 내부",
                "위키 MCP 서버(위키팀 VPC)가 자기 기준으로 다시 검증하고 "
                "(위키 IdP JWKS·token_use=access·client_id·scope=files/read), "
                "토큰 속 사용자(sub) 소유 데이터(S3 wiki-notes/{sub}.json)에만 "
                "접근을 허용했습니다.", tool="wiki",
                level=None if ok else "warn",
                data={
                    "요청": f"{tool}",
                    **result_bits,
                    "경로": "통합 Gateway → 위키 MCP(VPC C) → S3",
                    "데이터 격리": "wiki-notes/{sub}.json — 본인 것만",
                },
                code=("# wiki_mcp/server.py — 위키(3rd party) 쪽 검증 코드\n"
                      "c = _claims(ctx)  # 위키 IdP access token 검증\n"
                      "if 'files/read' not in c['scope'].split(): return _deny()\n"
                      "notes = _load(c['sub'])  # 사용자별 S3 격리"))
    return {"status": 200 if ok else 502, "body": resp}


def _run_wiki(method: str, path: str, body: dict | None = None) -> str:
    try:
        out = asyncio.run(_wiki_op(method, path, body))
    except Exception as e:  # noqa: BLE001
        out = {"status": 500, "body": {"error": str(e)[:300]}}
    return json.dumps(out, ensure_ascii=False)


@tool
def save_to_wiki(title: str, content: str) -> str:
    """온톨로 위키(사내 위키, 별도 로그인이 필요한 외부 서비스)에 사용자
    명의로 노트를 저장한다. 추천 목록·장보기 메모 등을 기록할 때 사용.
    최초 1회는 사용자의 위키 로그인/동의가 필요할 수 있다 — 그 경우
    도구가 동의 완료까지 대기하므로 그대로 기다리면 된다.

    Args:
        title: 노트 제목
        content: 노트 본문 (마크다운 가능)
    """
    out = _run_wiki("POST", "/notes", {"title": title, "content": content})
    _record("save_to_wiki", {"title": title}, out)
    return out


@tool
def list_wiki_notes() -> str:
    """온톨로 위키에서 이 사용자가 저장한 노트 목록을 가져온다.
    (사용자 위임 토큰으로 접근 — 본인 노트만 보임)"""
    out = _run_wiki("GET", "/notes")
    _record("list_wiki_notes", {}, out)
    return out


SYSTEM_PROMPT = """당신은 식료품·리빙 온라인 마켓 "온톨로 마켓"의 퍼스널 쇼퍼입니다.

당신에게는 이 고객에 대한 두 가지 컨텍스트가 주어집니다:
1. [장기 기억] — 과거 대화에서 추출된 취향/사실
2. [개인화 그래프] — Neptune 지식그래프의 고객 프로필(취향 trait, 제약, 구매이력)

원칙:
- 추천 전에는 반드시 recommend_for_me 또는 search_products 도구로 실제
  카탈로그를 조회하세요. 카탈로그에 없는 상품을 지어내지 마세요.
- 고객의 식이 제약(HAS_CONSTRAINT)은 절대 어기지 마세요. 애매하면
  check_dietary_safety로 확인하세요.
- 추천할 때 "왜 이 상품인지"를 고객의 취향/이력과 연결해 한 줄로 설명하세요.
  (예: "유당불내증이 있으셔서 락토프리로 골랐어요")
- 한국어로, 친근하지만 간결하게. 추천은 2~4개면 충분합니다.
- 가격은 원화로 표기하세요.
- 등급(tier)에 따라 도구가 주는 데이터가 다릅니다(등급은 별도 디렉터리에서
  결정되며 토큰으로 바꿀 수 없습니다). recommend_for_me가 basic 등급이라
  개인화 대신 "인기 상품"(personalized=false)을 반환하면, 개인화 추천이
  아니라 인기 상품임을 솔직히 알리고 그대로 안내하세요. 프로필/취향 도구가
  "premium 전용"으로 제한되면 지금 등급으로는 접근할 수 없다고 정중히
  안내하세요. 개인화 데이터를 지어내서 대체하지 마세요.
- 개인화 데이터 접근에는 최초 1회 데이터팀 SSO 동의가 필요할 수 있습니다.
  도구가 대기하는 동안 화면에 동의 버튼이 뜨며, 동의 시간 초과로 실패하면
  화면의 버튼으로 로그인을 마친 뒤 다시 요청해 달라고 안내하세요.
- 사용자가 "위키에 저장/기록해줘"라고 하면 save_to_wiki를, "위키에 뭐
  저장했었지"라고 하면 list_wiki_notes를 쓰세요. 위키는 별도 로그인이
  필요한 외부 서비스라 최초 1회 동의가 필요할 수 있고, 도구가 대기하는
  동안 화면에 동의 버튼이 표시됩니다. 동의 시간 초과(408)로 실패하면
  화면의 동의 버튼을 눌러 위키 로그인을 마친 뒤 다시 요청해 달라고
  안내하세요.
"""


@app.entrypoint
async def invoke(payload: dict, context):
    query = (payload.get("query") or "").strip()
    requested_cid = payload.get("customer_id") or ""   # 참고용 — 신뢰하지 않음(아래 바인딩)
    session_id = payload.get("session_id") or "default"
    _tool_events.clear()
    _consent_shown.clear()  # 턴 시작 — 동의 카드는 서비스별 1장만
    _drain_debug()

    # 1. 인바운드 JWT — Runtime의 customJWT authorizer가 이미 서명을
    #    검증했다. 여기서는 원문을 확보해 Gateway 호출에 재사용한다.
    headers = getattr(context, "request_headers", None) or {}
    auth = ""
    for k, v in headers.items():
        if k.lower() == "authorization":
            auth = v
            break
    jwt = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else ""
    _current["jwt"] = jwt

    # 1-b. revoke 즉시 실효 — authorizer의 JWKS 오프라인 검증은 GlobalSignOut을 모른다.
    #      PrivateLink에서 Cognito GetUser는 호출 불가 → S3 revoke 목록으로 판정. 그 외 오류는 fail-open.
    if jwt and _jwt_revoked(jwt):
        _push_debug("④", "마켓 세션 revoke됨 — 요청 거부", "이 access 토큰은 서명·만료는 유효하지만 "
                    "revoke 목록에 올라 있습니다(revoke 도구가 GlobalSignOut 후 S3 마커 기록). authorizer는 "
                    "오프라인 검증이라 통과시켰지만, 에이전트가 목록을 확인해 즉시 거부합니다.", level="warn",
                    data={"확인": f"s3://{REVOCATION_BUCKET}/{REVOCATION_PREFIX}<sub>.json · token.iat <= revoked_at",
                          "왜 S3인가": "Cognito GetUser는 PrivateLink에서 호출 불가 — VPC 안에선 revoke 목록이 유일한 즉시 판정 수단"})
        for ev in _drain_debug():
            yield ev
        yield {"type": "error", "message": "로그인 세션이 종료되었습니다 — 다시 로그인해 주세요."}
        return

    # 1-c. JWT가 없으면 진행하지 않는다 (배포 환경에서는 authorizer가 먼저 거부하지만,
    #      로컬 실행에서도 기본 고객으로 떨어지지 않도록 명시적으로 종료).
    if not jwt:
        _push_debug("④", "인바운드 JWT 없음 — 요청 거부",
                    "Authorization 헤더에 사용자 JWT가 없어 고객을 바인딩할 수 없습니다.", level="warn")
        for ev in _drain_debug():
            yield ev
        yield {"type": "error", "message": "로그인이 필요합니다 — Authorization: Bearer <JWT>"}
        return

    # 2. 고객 바인딩 — JWT sub → CUSTOMER_MAP. 요청 payload의 customer_id는 무시한다:
    #    같은 토큰으로 다른 고객의 기억/그래프를 요청해도 자기 고객으로 고정된다.
    #    매핑이 없는 회원은 개인화 없이(user-<sub>) 상담만 진행한다.
    sub = _jwt_claims(jwt).get("sub", "")
    customer_id = CUSTOMER_MAP.get(sub) or f"user-{sub[:8]}"
    _current["customer_id"] = customer_id
    if jwt:
        mismatch = bool(requested_cid) and requested_cid != customer_id
        _push_debug("④", "고객 바인딩 — JWT sub → 고객 프로필 (요청값 무시)",
                    f"이 대화의 고객은 요청이 보낸 customer_id가 아니라 토큰 sub로 정해집니다 → "
                    f"{customer_id}." + (" 요청값이 달라 무시했습니다." if mismatch else "")
                    + ("" if sub in CUSTOMER_MAP else " (연결된 고객 프로필 없음 — 개인화 없이 진행)"),
                    level="warn" if (mismatch or sub not in CUSTOMER_MAP) else None,
                    data={"JWT sub": sub or "(없음)", "바인딩된 고객": customer_id,
                          "요청 customer_id": requested_cid or "(없음)",
                          "요청값 무시": "예" if mismatch else "아니오(일치)"},
                    code="customer_id = bound_customer(jwt_claims['sub'])  # payload 값은 참고만\n"
                         "# Neptune MCP도 customer_id 인자 없이 위임 토큰 신원으로 같은 매핑을 적용")
        jc = _jwt_claims(jwt)
        _push_debug("④", "Runtime authorizer가 JWT 검증 (1차 검증)",
                    "customJWT authorizer가 아래 실데이터로 검증했습니다 — "
                    "토큰 헤더의 kid로 JWKS에서 공개키를 찾아 RS256 서명을 "
                    "확인하고, iss·client_id·exp를 대조. 실패 시 에이전트 "
                    "코드는 실행조차 안 됩니다 (401).",
                    claims=jc, token_preview=_token_preview(jwt),
                    data={
                        **_jwks_match(jwt),
                        "iss 검사": jc.get("iss", ""),
                        "client_id ∈ allowedClients": jc.get("client_id", ""),
                        "alg": _jwt_header(jwt).get("alg", ""),
                    },
                    code=("# authorizer 설정 (deploy_runtime.py)\n"
                          "authorizerConfiguration = {\"customJWTAuthorizer\": {\n"
                          "  \"discoveryUrl\": <마켓 IdP openid-configuration>,\n"
                          "  \"allowedClients\": [<앱 클라이언트 ID>]}}"))

    # AgentCore Identity — Runtime이 검증된 사용자 JWT를 workload access
    # token으로 교환(GetWorkloadAccessTokenForJWT)해 컨테이너에 전달한다.
    # "이 워크로드(에이전트)가 이 사용자를 대행 중"이라는 신원 바인딩.
    wat = BedrockAgentCoreContext.get_workload_access_token()
    if wat:
        _push_debug("④", "AgentCore Identity — workload access token 발급",
                    "Runtime이 사용자 JWT를 AgentCore Identity 서비스에서 "
                    "workload access token으로 교환했습니다. '에이전트 워크로드 "
                    "+ 사용자'가 묶인 신원으로, 위키 OBO(USER_FEDERATION) 토큰 "
                    "교환의 기반이 됩니다.",
                    token_preview=_token_preview(wat),
                    claims=_jwt_claims(wat) or None,
                    data={
                        "교환 API": "GetWorkloadAccessTokenForJWT",
                        "입력": "사용자 JWT (마켓 IdP)",
                        "출력 토큰 길이": f"{len(wat)} chars",
                        "바인딩": "workload=쇼핑 에이전트 × user=" +
                                  str(_jwt_claims(jwt).get("username", "?")),
                    })
    for ev in _drain_debug():
        yield ev

    # 권한 tier(에이전트 로컬 힌트) — 프롬프트에 개인화 컨텍스트를 미리 주입할지 결정하는 데만 쓴다.
    # tier의 진실원은 LDAP이며, 실제 데이터 접근은 Gateway interceptor가 주입한 X-Tier로 MCP가 강제한다.
    groups = (_jwt_claims(jwt).get("cognito:groups") or []) if jwt else ["premium"]
    is_premium = "premium" in groups
    if jwt:
        _push_debug("권한", f"에이전트 tier 힌트 — {'premium' if is_premium else 'basic'} (컨텍스트 주입용)",
                    "에이전트는 토큰의 그룹 힌트로 프롬프트에 개인화 컨텍스트를 "
                    "미리 넣을지만 정합니다. tier의 진실원은 별도 VPC의 LDAP이고, "
                    "실제 데이터 차등은 Gateway interceptor가 LDAP 조회로 주입한 "
                    "X-Tier를 근거로 MCP(데이터팀 VPC)가 강제합니다 — 토큰만으로는 "
                    "데이터를 못 바꿉니다(심층 방어).",
                    level=None if is_premium else "warn",
                    data={
                        "cognito:groups(힌트)": groups,
                        "is_premium(힌트)": is_premium,
                        "개인화 컨텍스트 사전주입": "예" if is_premium else "아니오",
                        "tier 진실원/강제": "LDAP → X-Tier → MCP(데이터팀 VPC)",
                    },
                    code=("# 에이전트는 힌트만 — 강제는 백엔드(MCP)에서\n"
                          "is_premium = 'premium' in groups  # 프롬프트 사전주입 힌트\n"
                          "# 실제 tier 데이터 차등: interceptor(LDAP)→X-Tier→MCP"))

    # 2. 컨텍스트 수집 (실행 role의 IAM/SigV4 — 사용자 토큰과 무관). 세 호출은 서로 독립 — 병렬 실행.
    #    프로필 프리페치는 사전주입용이라 동의 대기 없이 결과만 쓴다 — 미동의면 recommend_for_me 툴이 턴 중에 처리.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as _ex:
        _f_hist = _ex.submit(memory_client.recent_turns, customer_id, session_id, 8)
        _f_mem = _ex.submit(memory_client.retrieve_memories, customer_id, query) if is_premium else None
        _f_pf = (_ex.submit(neptune_gateway.call, "neptune___get_customer_profile", {}, jwt)
                 if (is_premium and neptune_gateway.available() and jwt) else None)
        history = _f_hist.result()
        memories = _f_mem.result() if _f_mem else []
        _pf = _f_pf.result() if _f_pf else None
    if _pf and _pf.get("status") == "consent" and not _consent_shown.get("neptune"):
        # 프리페치에서 미동의를 알았으면 카드를 턴 시작에 바로 띄운다 — 툴 호출 때 다시 띄우지 않음
        _consent_shown["neptune"] = _pf["url"]
        _emit_neptune_consent(_pf["url"])
    history_block = "\n".join(f"{t['role']}: {t['text'][:300]}" for t in history) or "(첫 대화)"
    if is_premium:
        mem_block = "\n".join(f"- {m['text']}" for m in memories) or "(아직 없음)"
        profile = {"notice": "개인화 프로필은 통합 Gateway 동의 후 로드됩니다"}
        if _pf and _pf.get("status") == "ok" and not _pf.get("isError"):
            profile = _pf.get("result") or profile
    else:
        memories = []
        mem_block = "(basic 등급 — 개인화 기억 접근 불가)"
        profile = {"notice": "basic 등급 — 개인화 프로필 접근 불가"}
        _push_debug("권한", "basic 힌트 — 개인화 컨텍스트 사전주입 안 함",
                    f"이 사용자({_jwt_claims(jwt).get('username')})는 premium "
                    f"힌트가 없어 장기 기억·개인화 그래프를 프롬프트에 미리 넣지 "
                    f"않습니다. Gateway 툴에서는 MCP가 X-Tier(LDAP)로 개인화 대신 "
                    f"인기 상품 등 제한 데이터를 돌려줍니다.",
                    level="warn")
    for ev in _drain_debug():
        yield ev

    prompt = f"""[장기 기억 — 과거 대화에서 추출된 이 고객의 취향/사실]
{mem_block}

[개인화 그래프 — Neptune 프로필]
{json.dumps(profile, ensure_ascii=False)}

[이번 세션 대화 이력]
{history_block}

[고객 메시지]
{query}"""

    agent = Agent(
        # boto_session 공유 — VPC 모드에서는 자격증명 해석이 느려 공용 세션에서 모델 클라이언트를 만든다.
        model=BedrockModel(model_id=MODEL_ID, boto_session=aws_session.SESSION),
        system_prompt=SYSTEM_PROMPT,
        tools=[search_products, recommend_for_me, check_dietary_safety,
               save_to_wiki, list_wiki_notes],
    )

    # 툴이 동의 대기 등으로 블로킹돼도 debug/auth_url 이벤트가 즉시
    # 흘러나가도록, strands 스트림을 별도 태스크로 돌리며 주기적으로
    # 이벤트 큐를 비운다.
    final_text = []
    q: asyncio.Queue = asyncio.Queue()

    async def _pump():
        try:
            async for ev in agent.stream_async(prompt):
                await q.put(("ev", ev))
        except Exception as e:  # noqa: BLE001
            await q.put(("err", str(e)[:400]))
        finally:
            await q.put(("done", None))

    pump_task = asyncio.create_task(_pump())
    while True:
        for dev in _drain_debug():
            yield dev
        try:
            kind, ev = await asyncio.wait_for(q.get(), timeout=0.4)
        except asyncio.TimeoutError:
            continue
        if kind == "done":
            break
        if kind == "err":
            yield {"type": "error", "message": ev}
            break
        if "data" in ev:
            chunk = ev["data"]
            final_text.append(chunk)
            yield {"type": "chunk", "text": chunk}
    await pump_task
    for dev in _drain_debug():
        yield dev

    answer = "".join(final_text)

    # 3. 대화 턴 저장 → 비동기 장기 추출 → Kinesis → Neptune 그래프
    memory_client.save_turn(customer_id, session_id, query, answer)

    yield {"type": "final",
           "answer": answer,
           "customer_id": customer_id,
           "session_id": session_id,
           "memories_used": memories,
           "tool_events": _tool_events,
           "auth_mode": "jwt-obo-chain" if jwt else "iam-fallback"}


if __name__ == "__main__":
    app.run()
