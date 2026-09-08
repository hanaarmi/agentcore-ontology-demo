# ontolo-personalization MCP server

Neptune Analytics에 저장된 초개인화 그래프(고객 취향·식이 제약·구매 이력·추천 후보)를
MCP tool로 노출하는 서버입니다. 단일 파일(`server.py`)로 구성되어 있으며, 로컬 MCP
클라이언트(stdio)와 AgentCore Runtime/Gateway(streamable-http) 양쪽에서 동작합니다.

핵심 설계:

- 어느 고객의 데이터를 볼지는 **인자가 아니라 호출자 신원**(위임 토큰의
  `username = MarketSSO_<sub>` → `CUSTOMER_MAP`)으로 결정합니다.
- 권한 tier(`basic`/`premium`)는 토큰이 아니라 Gateway interceptor가 주입한
  `X-Tier` 헤더에서 옵니다. tier별 데이터 차등은 이 서버가 수행합니다.
- 읽기 툴은 `graph/read`, 쓰기 툴은 `graph/write` scope(시스템 파이프라인 토큰)를 요구합니다.
- 서명/발급자 검증은 상위(Runtime/Gateway) JWT authorizer가 담당하고, 여기서는
  scope를 방어적으로 재확인만 합니다.

## Tools

| tool | 접근 | 설명 |
|---|---|---|
| `whoami` | 모두 | 호출자 신원(JWT 클레임, tier)과 고객 바인딩 결과. 인증 흐름 디버그용 |
| `search_products` | 모두 | 카탈로그 키워드 검색 (상품명·설명·카테고리·브랜드·속성) |
| `get_personalized_candidates` | basic / premium | 추천 후보. premium은 취향 그래프 기반 개인화, basic은 비개인화 인기 상품 |
| `get_customer_profile` | premium | 취향/관심사/식이 제약/라이프스타일 trait + 구매 이력 + 피해야 할 알러젠 |
| `check_dietary_safety` | premium | SKU 목록이 고객의 식이 제약에 위배되는지 검사 |
| `get_shared_interests` | premium | 같은 trait를 공유하는 다른 고객 수 (집계만) |
| `get_graph_view` | premium | 시각화용 노드/엣지 (고객·trait·구매·추천 후보·공유 관심사) |
| `list_attribute_vocab` | read 또는 write scope | 상품 속성(Attribute) 어휘 목록. 메모리 유래 취향 정규화용 |
| `merge_customer_trait` | `graph/write` | 고객→Trait 엣지 MERGE (source=memory, provenance 기록) |
| `prune_customer_traits` | `graph/write` | 메모리 유래 PREFERS/INTERESTED_IN 엣지를 상위 N개만 유지 |
| `delete_traits_by_record` | `graph/write` | 삭제된 메모리 레코드에서 유래한 trait 엣지 제거 |
| `list_customers` | stdio 전용 | 그래프에 등록된 고객 목록 |
| `query_graph` | stdio 전용 | 읽기 전용 openCypher 직접 실행 (쓰기 구문 차단) |

`list_customers`, `query_graph`는 다른 고객 데이터에 닿을 수 있어 `MCP_TRANSPORT=stdio`일 때만
등록됩니다. 원격(HTTP) 서버에는 노출되지 않습니다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Neptune Analytics / STS 리전 |
| `NEPTUNE_GRAPH_ID` | (없음) | Neptune Analytics graph id. 없으면 `${ONTOLO_STATE_DIR}/neptune.json`의 `graphId`를 읽음 |
| `ONTOLO_STATE_DIR` | `../infra/state` (이 디렉터리 기준) | 인프라 배포 산출물 디렉터리 |
| `NEPTUNE_ASSUME_ROLE_ARN` | (없음) | 설정 시 이 role을 AssumeRole해서 Neptune에 쿼리 (Neptune이 다른 계정에 있을 때) |
| `NEPTUNE_REQUIRED_SCOPE` | `graph/read` | 읽기 툴에 필요한 scope |
| `NEPTUNE_WRITE_SCOPE` | `graph/write` | 쓰기 툴에 필요한 scope |
| `CUSTOMER_MAP` | `{}` | JSON `{ "<마켓 sub>": "<customer_id>" }` — 토큰 신원 → 고객 ID 매핑 |
| `LOCAL_CUSTOMER_ID` | `cust-yuna` | stdio(로컬 개발)에서 바인딩할 고객 ID |
| `MCP_TRANSPORT` | `stdio` | `stdio` 또는 `streamable-http` |
| `MCP_HOST` | `0.0.0.0` | streamable-http 바인드 주소 |
| `MCP_PORT` | `8000` | streamable-http 포트 |
| `MCP_STATELESS` | `true` | streamable-http를 stateless로 실행 (Gateway가 세션 관리) |

## 로컬 실행 (stdio)

```bash
pip install -r requirements.txt
export AWS_REGION=us-east-1
export NEPTUNE_GRAPH_ID=<graph-id>        # 또는 ONTOLO_STATE_DIR에 neptune.json 배치
python3 server.py                          # MCP_TRANSPORT=stdio 기본
```

MCP 클라이언트 설정 예시(`.mcp.json`):

```json
{
  "mcpServers": {
    "ontolo-personalization": {
      "command": "python3",
      "args": ["mcp_server/server.py"],
      "env": { "AWS_REGION": "us-east-1", "NEPTUNE_GRAPH_ID": "<graph-id>" }
    }
  }
}
```

stdio에서는 인증 헤더가 없으므로 tier는 `premium`, 고객은 `LOCAL_CUSTOMER_ID`로 취급되며
읽기/쓰기 scope가 모두 부여됩니다.

## 원격 실행 (AgentCore Runtime / Gateway)

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8000 python3 server.py
# → http://<host>:8000/mcp
```

AgentCore Runtime에 `serverProtocol: MCP`로 배포하면 그대로 동작합니다.
Runtime/Gateway 뒤에서는 Host 헤더가 플랫폼 내부 도메인으로 바뀌어 들어오므로
DNS rebinding 보호는 비활성화되어 있습니다(인증은 상위 authorizer가 담당).

### 크로스 어카운트

Neptune Analytics는 리소스 기반 정책을 지원하지 않으므로, Neptune이 다른 계정에 있으면
`NEPTUNE_ASSUME_ROLE_ARN`으로 AssumeRole해야 합니다. 대상 role에는
`neptune-graph:ReadDataViaQuery`(쓰기 툴 사용 시 `WriteDataViaQuery`)와 `neptune-graph:GetGraph`
권한, 그리고 이 서버 실행 주체를 신뢰하는 trust policy가 필요합니다.
