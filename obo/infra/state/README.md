# infra/state

BFF·위키 뷰어·MCP 서버가 시작할 때 읽는 배포 상태 파일 위치입니다. 실제 `*.json`은
계정 ID, ARN, Cognito 클라이언트 시크릿을 담으므로 저장소에 포함하지 않습니다(`.gitignore`).

각 `*.example.json`은 필요한 키만 보여주는 템플릿입니다. 자신의 AWS 환경에 리소스를
만든 뒤 같은 이름의 `*.json`으로 채워 넣으면 됩니다. 디렉토리 위치는 환경변수
`ONTOLO_STATE_DIR`(기본 `infra/state`)와 `ONTOLO_OBO_STATE_DIR`(기본 `obo/infra/state`)로
바꿀 수 있습니다.
