#!/usr/bin/env bash
# 온톨로 위키 (별도 시스템, 읽기 전용 뷰어) — http://localhost:${WIKI_PORT:-5182}
# 마켓 데모(dev.sh)와 무관하게 단독 실행된다. 위키 IdP 로그인은 마켓 SSO로 302.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-${PYTHON:-python3}}"
export WIKI_PORT="${WIKI_PORT:-5182}"
echo "Starting 온톨로 위키 on http://localhost:$WIKI_PORT ..."
exec "$PY" wiki/app.py
