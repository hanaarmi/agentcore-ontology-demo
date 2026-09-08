# Ontolo Market — AgentCore 초개인화 쇼핑 에이전트 데모

> **참고용 소스코드입니다.** 아키텍처와 인증 흐름을 설명하기 위한 교육용 데모이며, 프로덕션 환경에서의
> 동작·보안·성능을 보증하지 않습니다. 그대로 배포하지 말고, 자신의 환경에 맞게 검토·보강한 뒤 사용하세요.
> AWS 공식 샘플이 아닙니다.

Amazon Bedrock AgentCore(Runtime · Gateway · Identity · Memory) 위에서 동작하는 쇼핑 상담
에이전트 데모입니다. 사용자 신원(JWT)이 브라우저 → BFF → 에이전트 → 통합 Gateway까지
그대로 흐르고, Gateway 뒤의 내부 MCP 서버(개인화 그래프, 사내 위키)는 Tool IdP가 발급한
**사용자 명의의 위임 토큰**으로만 호출됩니다. 권한 등급(tier)은 토큰이 아니라 LDAP이
결정하고, Gateway의 REQUEST interceptor가 `X-Tier` 헤더로 강제 주입합니다.

이 저장소에는 **애플리케이션 코드만** 들어 있습니다. AWS 리소스 프로비저닝 스크립트,
배포 상태 파일은 포함하지 않습니다.

> **데모 전용 패턴 안내.** 인증 흐름을 눈으로 보여주기 위해 운영에서는 쓰지 않을 패턴을 의도적으로
> 포함합니다 — 토큰을 브라우저 localStorage에 저장, SSE에 `?token=` 쿼리 전달, 디버그 패널에
> JWT claims 표시, 무인증 데모 리셋(revoke) API. 운영에서는 HttpOnly 쿠키 기반 BFF 세션, 헤더를
> 붙일 수 있는 fetch 스트림, 관리자 API 인증으로 대체해야 합니다. `data/`의 고객·구매·건강 정보는
> 전부 합성 데이터입니다. MCP 서버(`mcp_server/`, `wiki_mcp/`)는 AgentCore Runtime/Gateway 뒤에서만
> HTTP로 동작하도록 만들어졌고, 로컬에서는 stdio로만 실행하세요.

## 구성

| 디렉토리 | 역할 | 실행 위치 |
|---|---|---|
| `frontend/` | React + Vite SPA — 쇼핑 상담, 개인화 그래프, 장기기억, 인증 흐름 디버그 패널 | 로컬 (`:5181`) |
| `backend/bff/` | FastAPI BFF — JWT 검증·고객 바인딩, 에이전트 호출(SSE), 그래프 읽기(Gateway 경유), 장기기억 파이프라인 컨슈머 | 로컬 (`:8010`) |
| `app/shopping_agent/` | Strands 기반 쇼핑 에이전트 — AgentCore Runtime(VPC 모드)에 배포 | AgentCore Runtime |
| `mcp_server/` | 개인화 그래프(Neptune Analytics) MCP 서버 — tier 데이터 차등, 읽기/쓰기 scope 분리 | AgentCore Runtime |
| `wiki_mcp/` | 사내 위키 MCP 서버 — 위임 토큰 신원으로 노트 저장 | AgentCore Runtime |
| `wiki/` | 위키 뷰어(3rd-party 시스템 대역) — Tool IdP SSO 로그인 | 로컬 (`:5182`) |
| `interceptor/` | Gateway REQUEST interceptor Lambda — LDAP 조회 → `X-Tier` 주입, revoke 목록 확인 | Lambda |
| `data/` | 데모 상품 카탈로그·고객 페르소나 | — |
| `scripts/` | 로컬 실행 스크립트 | — |

## 아키텍처

![Ontolo Market 아키텍처 — VPC 분리 구조](docs/architecture.png)

## 사전 준비

이 코드는 다음 AWS 리소스가 이미 있다고 가정합니다. 프로비저닝 스크립트는 저장소에 없습니다.

- Cognito 사용자 풀 2개(Agent IdP, Tool IdP)와 OIDC 페더레이션, 리소스 서버 scope(`files/read`, `graph/read`, `graph/write`)
- AgentCore Runtime 3개(쇼핑 에이전트, 개인화 MCP, 위키 MCP), 통합 Gateway(MCP 프로토콜, MCP 세션 활성), AgentCore Identity OAuth2 credential provider, AgentCore Memory
- Neptune Analytics 그래프(사설 엔드포인트), Kinesis 스트림(Memory records), S3 버킷(위키 노트·revoke 목록)
- LDAP(glauth 등) + interceptor Lambda

리소스를 만든 뒤 `infra/state/*.example.json`, `obo/infra/state/*.example.json`을 참고해
같은 이름의 `*.json`을 채웁니다(자세한 내용은 각 디렉토리의 README).

## 로컬 실행

```bash
# BFF + 프론트엔드
pip install -r backend/bff/requirements.txt
(cd frontend && npm install)
./scripts/dev.sh            # BFF :8010, Vite :5181

# 위키 뷰어(별도 시스템)
pip install -r wiki/requirements.txt
./scripts/wiki.sh           # :5182
```

`interceptor/requirements.txt`는 Lambda 배포 패키지용, `app/shopping_agent`·`mcp_server`·`wiki_mcp`의
requirements는 AgentCore Runtime CodeZip용입니다.

AWS 자격증명은 표준 boto3 체인(환경변수, 프로파일, SSO)을 사용합니다.

## 환경변수

공통

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AWS_REGION` | `us-east-1` | 리전 |
| `ONTOLO_STATE_DIR` | `infra/state` | 배포 상태 파일 디렉토리 |
| `ONTOLO_OBO_STATE_DIR` | `obo/infra/state` | IdP 상태 파일 디렉토리 |

BFF(`backend/bff`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ONTOLO_CUSTOMERS_FILE` | `data/customers.json` | 고객 페르소나·계정 바인딩 |
| `WIKI_BASE_URL` | `http://localhost:5182` | 위키 뷰어 주소(프론트엔드에 `wikiBase`로 전달) |
| `ONTOLO_MODEL_ID` | Claude Sonnet 4.5 (us. 프로파일) | 파이프라인·요약에 쓰는 Bedrock 모델 ID |

위키 뷰어(`wiki`)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `WIKI_HOST` / `WIKI_PORT` | `127.0.0.1` / `5182` | 뷰어 바인드 주소·포트 |
| `WIKI_BASE_URL` | `http://localhost:5182` | OAuth redirect/logout URI의 기준 주소 |
| `WIKI_COOKIE_KEY` | 임의 생성 | 세션 쿠키 서명 키 |

쇼핑 에이전트(`app/shopping_agent`, Runtime env)

`BEDROCK_MODEL_ID`, `MEMORY_ID`, `NEPTUNE_GRAPH_ID`, `CUSTOMER_MAP`(JSON, `{sub: customer_id}`),
`UNIFIED_GATEWAY_URL`, `OBO_PROVIDER_NAME`, `OBO_SCOPES`, `NEPTUNE_SCOPES`, `REVOCATION_BUCKET`, `REVOCATION_PREFIX`

MCP 서버·interceptor의 환경변수는 `mcp_server/README.md`와 각 파일 상단을 참고하세요.

## 데모 계정

`data/customers.json`의 `market_user`가 Cognito 사용자(`sub`)와 고객 프로필을 연결합니다.
저장소의 값은 플레이스홀더이므로 자신의 풀에서 만든 사용자의 `sub`로 바꿔야 합니다.
매핑이 없는 회원은 개인화 없이(`user-<sub>`) 상담만 가능하고, JWT가 없는 요청은 거부됩니다.

## 라이선스

MIT — [LICENSE](LICENSE)
