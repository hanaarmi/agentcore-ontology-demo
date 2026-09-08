"""ontolo-personalization MCP server.

Neptune Analytics의 초개인화 그래프(고객 취향·제약·구매이력·추천)를 MCP tool로 노출한다.
단일 파일로 stdio(로컬)와 streamable-http(AgentCore Runtime/Gateway) 전송을 모두 지원한다.
환경변수와 실행 방법은 README.md 참조.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

import boto3
from mcp.server.fastmcp import Context, FastMCP

REGION = os.environ.get("AWS_REGION", "us-east-1")

GRAPH_ID = os.environ.get("NEPTUNE_GRAPH_ID", "")
if not GRAPH_ID:
    # NEPTUNE_GRAPH_ID가 없으면 인프라 배포 산출물(<ONTOLO_STATE_DIR>/neptune.json)에서 읽는다.
    _state_dir = os.environ.get("ONTOLO_STATE_DIR") or str(
        Path(__file__).resolve().parents[1] / "infra" / "state")
    state = Path(_state_dir) / "neptune.json"
    if state.exists():
        GRAPH_ID = json.loads(state.read_text()).get("graphId", "")

ASSUME_ROLE_ARN = os.environ.get("NEPTUNE_ASSUME_ROLE_ARN", "")

# 위임 토큰의 필수 읽기 scope — 서명/iss/client_id는 상위 authorizer가 검증했고,
# 여기서는 graph 접근 동의(scope)가 실제로 있는지만 방어적으로 재확인한다.
REQUIRED_SCOPE = os.environ.get("NEPTUNE_REQUIRED_SCOPE", "graph/read")
# 쓰기 scope — 사용자 위임 토큰엔 없고 시스템 파이프라인(client_credentials) 토큰만 가진다.
# 쓰기 툴만 customer_id를 인자로 받고 호출자(client_id)를 provenance로 남긴다.
WRITE_SCOPE = os.environ.get("NEPTUNE_WRITE_SCOPE", "graph/write")
# 관계 타입 허용목록 — cypher에 문자열로 들어가므로 반드시 이 안에서만
ALLOWED_RELATIONS = {"PREFERS", "INTERESTED_IN", "HAS_CONSTRAINT", "HAS_LIFESTYLE", "OWNS_PET"}
MAX_MEMORY_TRAITS = 10

# 고객 바인딩 {마켓 sub: customer_id} — 어느 고객의 그래프를 볼지는 인자가 아니라
# 토큰 신원(username MarketSSO_<sub>)으로만 결정한다. 배포 스크립트가 env로 주입.
CUSTOMER_MAP: dict[str, str] = json.loads(os.environ.get("CUSTOMER_MAP", "{}"))
LOCAL_CUSTOMER_ID = os.environ.get("LOCAL_CUSTOMER_ID", "cust-yuna")  # stdio 로컬 개발용

mcp = FastMCP("ontolo-personalization")

_client_cache: dict = {}


def _ng():
    """neptune-graph 클라이언트. Neptune이 다른 계정이면 AssumeRole 경유.
    임시 자격증명은 만료 5분 전에 갱신한다."""
    now = time.time()
    cached = _client_cache.get("ng")
    if cached and now < _client_cache.get("expires_at", 0) - 300:
        return cached
    if ASSUME_ROLE_ARN:
        sts = boto3.client("sts", region_name=REGION)
        creds = sts.assume_role(
            RoleArn=ASSUME_ROLE_ARN,
            RoleSessionName="ontolo-mcp-neptune",
            DurationSeconds=3600,
        )["Credentials"]
        client = boto3.client(
            "neptune-graph", region_name=REGION,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        _client_cache["expires_at"] = creds["Expiration"].timestamp()
    else:
        client = boto3.client("neptune-graph", region_name=REGION)
        _client_cache["expires_at"] = now + 10 * 3600
    _client_cache["ng"] = client
    return client


# 프로세스 시작 시 자격증명을 미리 해석한다(VPC 모드 런타임의 첫 호출 지연 완화).
# 실패해도 무시 — 첫 쿼리에서 다시 시도된다.
if os.environ.get("MCP_TRANSPORT", "stdio") == "streamable-http":
    try:
        _t0 = time.time()
        _ng()  # 클라이언트 생성(캐시)
        _c = boto3.Session(region_name=REGION).get_credentials()
        if _c:
            _c.get_frozen_credentials()
        print(f"[neptune-mcp] credential warm-up {time.time()-_t0:.2f}s provider={getattr(_c,'method',None)} "
              f"AWS_env={sorted(k for k in os.environ if k.startswith('AWS_'))}", flush=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[neptune-mcp] credential warm-up failed: {str(_e)[:160]}", flush=True)


def q(cypher: str, parameters: dict | None = None) -> list[dict]:
    r = _ng().execute_query(
        graphIdentifier=GRAPH_ID,
        queryString=cypher,
        language="OPEN_CYPHER",
        parameters=parameters or {},
    )
    return json.loads(r["payload"].read()).get("results", [])


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# constraint trait 이름 → 피해야 할 Allergen 노드 이름
TRAIT_TO_ALLERGENS = {
    "유당불내증": ["우유"],
    "대두 알러지": ["대두"],
    "견과 알러지": ["견과", "땅콩"],
    "땅콩 알러지": ["땅콩"],
    "글루텐 민감": ["밀"],
    "계란 알러지": ["계란"],
    "우유 알러지": ["우유"],
}


def _bad_allergens(customer_id: str) -> list[str]:
    rows = q("""
    MATCH (c:Customer {id: $cid})-[:HAS_CONSTRAINT]->(t:Trait)
    RETURN t.name AS name
    """, {"cid": customer_id})
    bad: set[str] = set()
    for row in rows:
        bad.update(TRAIT_TO_ALLERGENS.get(row["name"], []))
    return list(bad)


def _http_headers(ctx: Context | None) -> dict | None:
    """streamable-http일 때만 인바운드 HTTP 헤더 dict를 반환(대소문자 원본).
    stdio(로컬)면 None."""
    try:
        return dict(ctx.request_context.request.headers)
    except Exception:  # noqa: BLE001
        return None


def _get_header(headers: dict, name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


def _caller_identity(ctx: Context | None) -> dict:
    """Authorization 헤더의 JWT 클레임을 (서명 검증 없이) 디코드해 호출자 신원을 반환한다.
    서명 검증은 상위(Runtime/Gateway) authorizer가 담당. 토큰은 Gateway가 사용자 명의로
    교환한 Tool IdP 위임 토큰이며, tier는 토큰이 아니라 X-Tier 헤더(LDAP)에서 온다."""
    headers = _http_headers(ctx)
    if headers is None:
        return {"transport": "stdio", "auth": "none (local)",
                "tier": {"value": "premium", "source": "stdio 로컬(개발용)"}}
    auth = _get_header(headers, "authorization") or ""
    tier_hdr = _get_header(headers, "x-tier")
    tier = {"value": tier_hdr, "source": "X-Tier 헤더 (LDAP 엔타이틀먼트 디렉터리)"} if tier_hdr \
        else {"value": "basic", "source": "X-Tier 없음 → fail-closed(basic)"}
    if not auth.lower().startswith("bearer "):
        return {"transport": "http", "auth": "no bearer token", "tier": tier}
    token = auth.split(" ", 1)[1]
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:  # noqa: BLE001
        return {"transport": "http", "auth": "bearer (decode failed)",
                "error": str(e)[:100], "tier": tier}
    return {
        "transport": "http",
        "auth": "bearer JWT (Tool IdP 위임 토큰)",
        "token_preview": f"{token[:20]}...{token[-10:]}",
        "claims": {k: claims.get(k) for k in
                   ("sub", "username", "cognito:groups", "client_id", "scope",
                    "iss", "token_use", "exp") if k in claims},
        "tier": tier,
    }


# 권한 tier — basic: 상품 검색 + 비개인화 추천만 / premium: 개인화 데이터 전체.
# tier는 토큰이 아니라 Gateway interceptor가 LDAP 조회 후 주입한 X-Tier 헤더에서 온다.
def _tier(ctx: Context | None) -> str:
    return (_caller_identity(ctx).get("tier") or {}).get("value") or "basic"


def _scope_denied(ctx: Context | None, tool_name: str) -> str | None:
    """Tool IdP 토큰에 필수 scope(graph/read)가 없으면 거부(JSON), 통과면 None.
    stdio 로컬은 개발용이라 skip."""
    ident = _caller_identity(ctx)
    if ident.get("transport") == "stdio":
        return None
    scope = (ident.get("claims") or {}).get("scope") or ""
    if REQUIRED_SCOPE in scope.split():
        return None
    return _j({
        "error": f"위임 scope 부족 — 이 그래프 접근에는 '{REQUIRED_SCOPE}' 동의가 필요합니다",
        "tool": tool_name, "required_scope": REQUIRED_SCOPE,
        "granted_scope": scope,
    })


def _bound_customer(ctx: Context | None) -> tuple[str | None, dict]:
    """호출자 신원 → 고객 ID. Tool IdP 위임 토큰의 username `MarketSSO_<마켓 sub>`에서
    마켓 sub를 꺼내 CUSTOMER_MAP으로 매핑한다. 매핑이 없으면 (None, 근거).
    stdio(로컬 개발)는 LOCAL_CUSTOMER_ID."""
    ident = _caller_identity(ctx)
    if ident.get("transport") == "stdio":
        return LOCAL_CUSTOMER_ID, {"source": "stdio 로컬(개발용)"}
    uname = str((ident.get("claims") or {}).get("username") or "")
    sub = uname.split("MarketSSO_", 1)[1] if uname.startswith("MarketSSO_") else ""
    cid = CUSTOMER_MAP.get(sub)
    return cid, {"source": "위임 토큰 username(MarketSSO_<마켓 sub>) → CUSTOMER_MAP",
                 "username": uname, "market_sub": sub, "customer_id": cid}


def _scopes(ctx: Context | None) -> set[str]:
    ident = _caller_identity(ctx)
    if ident.get("transport") == "stdio":
        return {REQUIRED_SCOPE, WRITE_SCOPE}
    return set(((ident.get("claims") or {}).get("scope") or "").split())


def _deny_if_no_write(ctx: Context | None, tool_name: str) -> str | None:
    """graph/write scope(파이프라인 시스템 토큰)가 없으면 거부. 사용자 위임 토큰은 여기서 막힌다."""
    if WRITE_SCOPE in _scopes(ctx):
        return None
    return _j({"error": f"쓰기 권한 없음 — '{WRITE_SCOPE}' scope(시스템 파이프라인 전용)가 필요합니다",
               "tool": tool_name, "granted_scope": " ".join(sorted(_scopes(ctx)))})


def _unbound(tool_name: str, binding: dict) -> str:
    return _j({"error": "이 계정에 연결된 고객 프로필이 없습니다 — 개인화 데이터를 조회할 수 없습니다",
               "tool": tool_name, "customer_binding": binding})


def _deny_if_not_premium(ctx: Context | None, tool_name: str) -> str | None:
    """premium tier(X-Tier)가 아니면 거부 응답(JSON 문자열)을, 통과면 None.
    scope 미달도 함께 거부한다(방어적 재검증)."""
    denied = _scope_denied(ctx, tool_name)
    if denied:
        return denied
    if _tier(ctx) == "premium":
        return None
    return _j({
        "tier_limited": True,
        "error": "이 도구는 premium 등급 전용입니다",
        "tool": tool_name,
        "required_tier": "premium",
        "your_tier": _tier(ctx),
        "tier_source": "X-Tier 헤더 (LDAP 엔타이틀먼트 디렉터리)",
        "hint": "개인화 프로필/취향 기반 추천은 premium 사용자만 접근할 수 있습니다. "
                "상품 검색(search_products)과 인기 추천(get_personalized_candidates)은 사용 가능합니다.",
    })


# ─────────────────────────── tools ───────────────────────────


@mcp.tool()
def whoami(ctx: Context) -> str:
    """이 MCP 툴이 어떤 사용자 신원(JWT)으로 호출되었는지, 그 신원이 어느 고객
    프로필에 바인딩되는지 반환한다. 인증 흐름 디버그용."""
    cid, binding = _bound_customer(ctx)
    return _j({**_caller_identity(ctx), "customer_binding": binding})


# list_customers / query_graph는 다른 고객 데이터에 닿을 수 있어 stdio(로컬)에서만 등록한다(파일 하단).
def list_customers() -> str:
    """[로컬 개발 전용] 개인화 그래프에 등록된 고객 목록을 조회한다."""
    rows = q("MATCH (c:Customer) RETURN c.id AS customer_id, c.name AS name, c.segment AS segment")
    return _j(rows)


@mcp.tool()
def get_customer_profile(ctx: Context = None) -> str:
    """현재 사용자(위임 토큰의 신원)에 바인딩된 고객의 초개인화 프로필 전체를
    조회한다 — 취향/관심사/식이 제약/라이프스타일 trait(신뢰도·출처 포함)와
    최근 구매 이력. 어느 고객인지는 인자가 아니라 호출자 신원에서 결정된다.
    """
    denied = _deny_if_not_premium(ctx, "get_customer_profile")
    if denied:
        return denied
    customer_id, binding = _bound_customer(ctx)
    if not customer_id:
        return _unbound("get_customer_profile", binding)
    traits = q("""
    MATCH (c:Customer {id: $cid})-[r]->(t:Trait)
    RETURN type(r) AS relation, t.name AS trait, t.kind AS kind,
           r.confidence AS confidence, r.source AS source, r.updated_at AS updated_at
    ORDER BY r.confidence DESC
    """, {"cid": customer_id})
    purchases = q("""
    MATCH (c:Customer {id: $cid})-[r:PURCHASED]->(p:Product)
    OPTIONAL MATCH (p)-[:IN_CATEGORY]->(cat:Category)
    RETURN p.sku AS sku, p.name AS name, p.price AS price,
           cat.name AS category, r.date AS date, r.qty AS qty
    ORDER BY r.date DESC
    """, {"cid": customer_id})
    info = q("MATCH (c:Customer {id: $cid}) RETURN c.name AS name, c.segment AS segment",
             {"cid": customer_id})
    return _j({"customer": info[0] if info else None,
               "customer_id": customer_id, "customer_binding": binding,
               "traits": traits, "purchases": purchases,
               "avoid_allergens": _bad_allergens(customer_id),
               "_caller": _caller_identity(ctx)})


@mcp.tool()
def get_personalized_candidates(limit: int = 10, ctx: Context = None) -> str:
    """현재 사용자에 바인딩된 고객의 취향 그래프 기반 추천 후보 상품을 조회한다.
    취향 trait와 상품 속성을 조인해 신뢰도 합산 점수로 정렬하고,
    식이 제약에 걸리는 상품과 이미 구매한 상품은 제외한다.

    tier에 따라 **같은 도구가 다른 데이터**를 돌려준다(데이터 차등은 MCP가 수행):
      premium — 취향 그래프 기반 개인화 후보(matched_traits·점수·알러지 제외)
      basic   — 취향/제약을 반영하지 않는 비개인화 '인기 상품' 뷰(provenance 없음)

    Args:
        limit: 최대 후보 수 (기본 10)
    """
    denied = _scope_denied(ctx, "get_personalized_candidates")
    if denied:
        return denied
    tier = _tier(ctx)
    customer_id, binding = _bound_customer(ctx)
    if tier != "premium":
        # basic: 개인화(취향 그래프·제약)를 쓰지 않는 전역 인기 상품. 취향 provenance 없음.
        rows = q("""
        MATCH (p:Product)<-[r:PURCHASED]-(:Customer)
        OPTIONAL MATCH (p)-[:IN_CATEGORY]->(cat:Category)
        OPTIONAL MATCH (p)-[:OF_BRAND]->(b:Brand)
        RETURN p.sku AS sku, p.name AS name, p.price AS price,
               cat.name AS category, b.name AS brand, count(r) AS popularity
        ORDER BY popularity DESC LIMIT $limit
        """, {"limit": min(limit, 4)})
        return _j({"candidates": rows, "personalized": False, "tier": tier,
                   "tier_source": "X-Tier 헤더 (LDAP 엔타이틀먼트 디렉터리)",
                   "customer_id": customer_id, "customer_binding": binding,
                   "note": "basic 등급 — 취향/제약 기반 개인화 대신 전역 인기 상품을 제공합니다.",
                   "_caller": _caller_identity(ctx)})
    if not customer_id:
        return _unbound("get_personalized_candidates", binding)
    rows = q("""
    MATCH (c:Customer {id: $cid})-[r]->(t:Trait)
    MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute {name: t.name})
    WHERE NOT (c)-[:PURCHASED]->(p)
    OPTIONAL MATCH (p)-[:CONTAINS_ALLERGEN]->(alg:Allergen)
    WHERE alg.name IN $bad
    WITH p, t, r, count(alg) AS bad_count
    WHERE bad_count = 0
    OPTIONAL MATCH (p)-[:IN_CATEGORY]->(cat:Category)
    OPTIONAL MATCH (p)-[:OF_BRAND]->(b:Brand)
    RETURN p.sku AS sku, p.name AS name, p.price AS price,
           cat.name AS category, b.name AS brand,
           collect(DISTINCT t.name) AS matched_traits, sum(r.confidence) AS score
    ORDER BY score DESC LIMIT $limit
    """, {"cid": customer_id, "limit": limit,
          "bad": _bad_allergens(customer_id) or ["__none__"]})
    return _j({"candidates": rows, "personalized": True, "tier": tier,
               "customer_id": customer_id, "customer_binding": binding,
               "_caller": _caller_identity(ctx)})


@mcp.tool()
def check_dietary_safety(skus: list[str], ctx: Context = None) -> str:
    """상품 SKU 목록이 현재 사용자에 바인딩된 고객의 식이 제약(알러지·유당불내증 등)에
    위배되는지 확인한다. 위배 상품과 원인 알러젠을 반환한다 (빈 목록이면 모두 안전).

    Args:
        skus: 확인할 상품 SKU 목록 (예: ["GR-002", "GR-016"])
    """
    denied = _deny_if_not_premium(ctx, "check_dietary_safety")
    if denied:
        return denied
    customer_id, binding = _bound_customer(ctx)
    if not customer_id:
        return _unbound("check_dietary_safety", binding)
    bad = _bad_allergens(customer_id)
    if not bad:
        return _j({"violations": [], "note": "이 고객은 등록된 식이 제약이 없습니다."})
    rows = q("""
    UNWIND $skus AS sku
    MATCH (p:Product {sku: sku})-[:CONTAINS_ALLERGEN]->(a:Allergen)
    WHERE a.name IN $bad
    RETURN p.sku AS sku, p.name AS name, collect(a.name) AS allergens
    """, {"skus": skus, "bad": bad})
    return _j({"violations": rows, "customer_avoids": bad})


@mcp.tool()
def search_products(keyword: str, limit: int = 10) -> str:
    """상품 카탈로그를 키워드로 검색한다. 상품명·설명·카테고리·브랜드·
    속성에 대한 부분 일치.

    Args:
        keyword: 검색어 (예: "커피", "비건", "캠핑")
        limit: 최대 결과 수 (기본 10)
    """
    # Neptune Analytics openCypher는 any()/none() 술어 미지원 —
    # size + 리스트 컴프리헨션으로 속성 매칭.
    rows = q("""
    MATCH (p:Product)
    OPTIONAL MATCH (p)-[:IN_CATEGORY]->(c:Category)
    OPTIONAL MATCH (p)-[:OF_BRAND]->(b:Brand)
    OPTIONAL MATCH (p)-[:HAS_ATTRIBUTE]->(a:Attribute)
    WITH p, c, b, collect(a.name) AS attrs
    WHERE p.name CONTAINS $kw OR p.description CONTAINS $kw
       OR c.name CONTAINS $kw OR b.name CONTAINS $kw
       OR size([x IN attrs WHERE x CONTAINS $kw]) > 0
    RETURN p.sku AS sku, p.name AS name, p.price AS price,
           c.name AS category, b.name AS brand, attrs AS attributes,
           p.description AS description
    LIMIT $limit
    """, {"kw": keyword, "limit": limit})
    return _j(rows)


@mcp.tool()
def get_shared_interests(ctx: Context = None) -> str:
    """현재 사용자에 바인딩된 고객과 같은 취향/관심사 trait를 공유하는 다른 고객이
    몇 명인지 trait별로 집계한다. (개인 식별 정보 없이 카운트만 반환)
    """
    denied = _deny_if_not_premium(ctx, "get_shared_interests")
    if denied:
        return denied
    customer_id, binding = _bound_customer(ctx)
    if not customer_id:
        return _unbound("get_shared_interests", binding)
    rows = q("""
    MATCH (c:Customer {id: $cid})-[]->(t:Trait)<-[]-(o:Customer)
    WHERE o.id <> $cid
    RETURN t.name AS trait, t.kind AS kind, count(DISTINCT o) AS other_customers
    ORDER BY other_customers DESC
    """, {"cid": customer_id})
    return _j(rows)


_WRITE_PATTERN = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD)\b", re.IGNORECASE)


@mcp.tool()
def get_graph_view(ctx: Context = None) -> str:
    """현재 사용자에 바인딩된 고객의 개인화 그래프(시각화용 노드/엣지)를 반환한다 —
    고객 + 취향/제약 trait + 구매 상품 + trait와 맞는 추천 후보 + 같은 trait를 공유하는
    다른 고객 수(집계만, 개인 식별 없음). 고정 쿼리라 다른 고객 데이터에는 닿지 않는다.
    """
    denied = _deny_if_not_premium(ctx, "get_graph_view")
    if denied:
        return denied
    cid, binding = _bound_customer(ctx)
    if not cid:
        return _unbound("get_graph_view", binding)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(nid: str, label: str, name: str, **props):
        nodes.setdefault(nid, {"id": nid, "label": label, "name": name, **props})

    info = q("MATCH (c:Customer {id: $cid}) RETURN c.name AS name, c.segment AS segment", {"cid": cid})
    if not info:
        return _j({"nodes": [], "edges": [], "customer_id": cid})
    add_node(cid, "Customer", info[0]["name"], segment=info[0]["segment"])
    for r in q("""
    MATCH (c:Customer {id: $cid})-[r]->(t:Trait)
    RETURN type(r) AS rel, t.name AS name, t.kind AS kind,
           r.confidence AS confidence, r.source AS source, r.updated_at AS updated_at
    """, {"cid": cid}):
        tid = f"trait:{r['name']}"
        add_node(tid, "Trait", r["name"], kind=r["kind"])
        edges.append({"from": cid, "to": tid, "rel": r["rel"], "confidence": r["confidence"],
                      "source": r["source"], "updated_at": r["updated_at"]})
    for r in q("""
    MATCH (c:Customer {id: $cid})-[pr:PURCHASED]->(p:Product)
    RETURN p.sku AS sku, p.name AS name, pr.date AS date
    """, {"cid": cid}):
        pid = f"prod:{r['sku']}"
        add_node(pid, "Product", r["name"], sku=r["sku"])
        edges.append({"from": cid, "to": pid, "rel": "PURCHASED", "date": r["date"]})
    for r in q("""
    MATCH (c:Customer {id: $cid})-[cr]->(t:Trait)
    MATCH (p:Product)-[:HAS_ATTRIBUTE]->(a:Attribute {name: t.name})
    WHERE NOT (c)-[:PURCHASED]->(p)
    RETURN DISTINCT t.name AS trait, p.sku AS sku, p.name AS name
    LIMIT 40
    """, {"cid": cid}):
        pid = f"prod:{r['sku']}"
        add_node(pid, "ProductCandidate", r["name"], sku=r["sku"])
        edges.append({"from": f"trait:{r['trait']}", "to": pid, "rel": "MATCHES"})
    for r in q("""
    MATCH (c:Customer {id: $cid})-[]->(t:Trait)<-[]-(o:Customer)
    WHERE o.id <> $cid
    RETURN t.name AS trait, count(DISTINCT o) AS others
    """, {"cid": cid}):
        if not r["others"]:
            continue
        sid = f"shared:{r['trait']}"
        add_node(sid, "SharedInterest", f"외 {r['others']}명", count=r["others"])
        edges.append({"from": f"trait:{r['trait']}", "to": sid, "rel": "SHARED_BY"})
    return _j({"nodes": list(nodes.values()), "edges": edges, "customer_id": cid,
               "customer_binding": binding, "_caller": _caller_identity(ctx)})


@mcp.tool()
def list_attribute_vocab(ctx: Context = None) -> str:
    """카탈로그의 상품 속성(Attribute) 어휘 목록 — 메모리에서 추출한 취향을 이 어휘로
    정규화해야 추천 조인(Trait.name = Attribute.name)에 걸린다. 고객 데이터 아님(읽기/쓰기 scope 모두 허용)."""
    if not (_scopes(ctx) & {REQUIRED_SCOPE, WRITE_SCOPE}):
        return _j({"error": "scope 부족", "tool": "list_attribute_vocab"})
    return _j([r["name"] for r in q("MATCH (a:Attribute) RETURN a.name AS name")])


# ─────────── 쓰기 툴 — 메모리→그래프 파이프라인(시스템 신원, graph/write) ───────────

@mcp.tool()
def merge_customer_trait(customer_id: str, relation: str, target: str, kind: str,
                         confidence: float = 0.7, memory_record_id: str = "",
                         ctx: Context = None) -> str:
    """[시스템 전용 · graph/write] 고객→Trait 엣지를 MERGE한다(source=memory, provenance=memory_record_id).
    (고객, 관계, 대상)에 대해 멱등. 호출 주체가 사용자가 아니라 파이프라인이므로 customer_id를 명시한다.

    Args:
        customer_id: 대상 고객 ID (예: cust-yuna)
        relation: PREFERS | INTERESTED_IN | HAS_CONSTRAINT | HAS_LIFESTYLE | OWNS_PET
        target: trait 이름 (예: 저당)
        kind: Preference | Interest | DietaryConstraint | Lifestyle
        confidence: 0~1
        memory_record_id: 출처 메모리 레코드 ID (삭제 전파용)
    """
    denied = _deny_if_no_write(ctx, "merge_customer_trait")
    if denied:
        return denied
    if relation not in ALLOWED_RELATIONS:
        return _j({"error": f"허용되지 않은 관계 타입: {relation}", "allowed": sorted(ALLOWED_RELATIONS)})
    from datetime import datetime, timezone
    caller = (_caller_identity(ctx).get("claims") or {}).get("client_id", "stdio")
    existed = q(f"""
    MATCH (c:Customer {{id: $cid}})-[r:{relation}]->(t:Trait {{name: $target}})
    RETURN r.confidence AS confidence, r.source AS source
    """, {"cid": customer_id, "target": target})
    q(f"""
    MATCH (c:Customer {{id: $cid}})
    MERGE (t:Trait {{name: $target}})
    SET t.kind = $kind
    MERGE (c)-[r:{relation}]->(t)
    SET r.confidence = $conf, r.source = 'memory', r.updated_at = $ts,
        r.memory_record_id = $rid, r.written_by = $by
    """, {"cid": customer_id, "target": target, "kind": kind, "conf": float(confidence),
          "rid": memory_record_id, "ts": datetime.now(tz=timezone.utc).isoformat(), "by": caller})
    return _j({"existed": bool(existed), "previous": existed[0] if existed else None,
               "customer_id": customer_id, "relation": relation, "target": target, "written_by": caller})


@mcp.tool()
def prune_customer_traits(customer_id: str, keep: int = MAX_MEMORY_TRAITS, ctx: Context = None) -> str:
    """[시스템 전용 · graph/write] 메모리 유래 PREFERS/INTERESTED_IN 엣지를 상위 keep개만 남긴다
    (confidence 낮은 순 → 오래된 순 제거). 제거된 trait 목록을 반환한다."""
    denied = _deny_if_no_write(ctx, "prune_customer_traits")
    if denied:
        return denied
    rows = q("""
    MATCH (c:Customer {id: $cid})-[r]->(t:Trait)
    WHERE type(r) IN ['PREFERS', 'INTERESTED_IN'] AND r.source = 'memory'
    RETURN type(r) AS rel, t.name AS name, r.confidence AS confidence, r.updated_at AS updated_at
    ORDER BY r.confidence DESC, r.updated_at DESC
    """, {"cid": customer_id})
    excess = rows[max(int(keep), 0):]
    for row in excess:
        if row["rel"] in ALLOWED_RELATIONS:
            q(f"MATCH (c:Customer {{id: $cid}})-[r:{row['rel']}]->(t:Trait {{name: $name}}) DELETE r",
              {"cid": customer_id, "name": row["name"]})
    return _j({"pruned": excess, "customer_id": customer_id})


@mcp.tool()
def delete_traits_by_record(memory_record_id: str, ctx: Context = None) -> str:
    """[시스템 전용 · graph/write] 삭제된 메모리 레코드(provenance)에서 유래한 trait 엣지를 제거한다."""
    denied = _deny_if_no_write(ctx, "delete_traits_by_record")
    if denied:
        return denied
    rows = q("""
    MATCH (c:Customer)-[r]->(t:Trait)
    WHERE r.memory_record_id = $rid
    DELETE r
    RETURN count(*) AS n
    """, {"rid": memory_record_id})
    return _j({"removed": rows[0]["n"] if rows else 0, "memory_record_id": memory_record_id})


def query_graph(cypher: str, ctx: Context = None) -> str:
    """[로컬 개발 전용] 개인화 그래프에 읽기 전용 openCypher 쿼리를 직접 실행한다.
    스키마: (:Customer)-[:PREFERS|INTERESTED_IN|HAS_CONSTRAINT|HAS_LIFESTYLE|
    OWNS_PET]->(:Trait), (:Customer)-[:PURCHASED]->(:Product)-[:HAS_ATTRIBUTE]->
    (:Attribute), (:Product)-[:IN_CATEGORY]->(:Category), (:Product)-[:OF_BRAND]->
    (:Brand), (:Product)-[:CONTAINS_ALLERGEN]->(:Allergen).
    쓰기 구문(CREATE/MERGE/DELETE/SET 등)은 거부된다.

    Args:
        cypher: 실행할 읽기 전용 openCypher 쿼리
    """
    denied = _deny_if_not_premium(ctx, "query_graph")
    if denied:
        return denied
    if _WRITE_PATTERN.search(cypher):
        return _j({"error": "읽기 전용 툴입니다. 쓰기 구문(CREATE/MERGE/DELETE/SET/REMOVE/DROP)은 허용되지 않습니다."})
    try:
        return _j(q(cypher)[:100])
    except Exception as e:  # noqa: BLE001
        return _j({"error": str(e)[:400]})


# 자유 cypher·전체 고객 목록은 원격(HTTP)에 노출하지 않는다 — 원격은 "현재 사용자의 고객"만 다룬다.
if os.environ.get("MCP_TRANSPORT", "stdio") == "stdio":
    mcp.tool()(list_customers)
    mcp.tool()(query_graph)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "8000"))
        # stateless HTTP(기본): 세션은 Gateway가 관리한다.
        mcp.settings.stateless_http = os.environ.get("MCP_STATELESS", "true").lower() == "true"
        # Runtime/Gateway 뒤에서는 Host 헤더가 플랫폼 내부 도메인으로 바뀌어 들어오므로
        # DNS rebinding 보호를 끈다(안 끄면 421). 인증은 상위 authorizer가 담당.
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
