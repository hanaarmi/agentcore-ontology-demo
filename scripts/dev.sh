#!/usr/bin/env bash
# Dev runner: BFF on :${BFF_PORT:-8010}, Vite on :${VITE_PORT:-5181}.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -s "$HOME/.nvm/nvm.sh" ]; then
  source "$HOME/.nvm/nvm.sh"
  nvm use 20 > /dev/null || true
fi

PY="${PY:-${PYTHON:-python3}}"
BFF_HOST="${BFF_HOST:-127.0.0.1}"
BFF_PORT="${BFF_PORT:-8010}"
VITE_PORT="${VITE_PORT:-5181}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# bedrock-agentcore를 아는 boto3(>=1.40)가 필요하다. 오래된 virtualenv가
# 활성화된 셸에서 실행하면 구버전 boto3를 물고 실패하므로 먼저 확인한다.
if ! $PY -c "import boto3; boto3.client('bedrock-agentcore', region_name='$AWS_REGION')" 2>/dev/null; then
  echo "ERROR: $PY 의 boto3가 bedrock-agentcore를 지원하지 않습니다 (구버전)."
  echo "       PY=/path/to/python3 ./scripts/dev.sh 로 최신 boto3가 있는 파이썬을 지정하세요."
  exit 1
fi

echo "Starting BFF on http://$BFF_HOST:$BFF_PORT ..."
$PY -m uvicorn backend.bff.main:app \
   --host "$BFF_HOST" --port "$BFF_PORT" --log-level info &
BFF_PID=$!

echo "Starting Vite on http://127.0.0.1:$VITE_PORT ..."
( cd frontend && npx vite --port "$VITE_PORT" ) &
VITE_PID=$!

cleanup() {
  echo
  echo "Stopping (BFF=$BFF_PID, Vite=$VITE_PID) ..."
  kill $BFF_PID $VITE_PID 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait $BFF_PID $VITE_PID
