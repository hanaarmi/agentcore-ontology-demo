"""프로세스 공용 boto3 Session — 자격증명을 프로세스당 1회만 해석한다.
VPC 모드 런타임에서는 자격증명 provider 체인 해석이 느려, 호출마다 새 Session을 만들지 않고 공유한다.
시작 시 provider 체인 진단(AWS_* 환경변수 이름, 해석 소요시간, provider)을 로그로 남긴다.
"""
from __future__ import annotations

import logging
import os
import time

import boto3

LOG = logging.getLogger("ShoppingAgent")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_t0 = time.time()
SESSION = boto3.Session(region_name=REGION)
_creds = None
try:
    _creds = SESSION.get_credentials()
    if _creds is not None:
        _creds.get_frozen_credentials()  # 실제 해석 강제(지연 로딩)
except Exception as e:  # noqa: BLE001
    LOG.warning("credential warm-up failed: %s", str(e)[:200])
_elapsed = time.time() - _t0

# 진단 — 값은 남기지 않고 이름만(자격증명 노출 금지). URI 계열은 호스트만.
_aws_env = sorted(k for k in os.environ if k.startswith("AWS_"))
_uri_hosts = {k: (os.environ.get(k, "").split("//")[-1].split("/")[0]) for k in _aws_env if "URI" in k or "ENDPOINT" in k}
LOG.info("boto3 session warm-up: %.2fs · provider=%s · AWS_* env=%s · uri_hosts=%s",
         _elapsed, getattr(_creds, "method", None), _aws_env, _uri_hosts)


def client(service: str, **kw):
    """공용 세션에서 클라이언트 생성 — 자격증명 재해석 없음."""
    return SESSION.client(service, region_name=REGION, **kw)
