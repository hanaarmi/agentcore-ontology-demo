"""온톨로 위키 — 별도 시스템의 읽기 전용 노트 뷰어 (마켓 앱과 코드·포트·IdP가 다르다).
쇼핑 에이전트가 사용자 위임(OBO) 토큰으로 저장한 노트를, 사용자가 위키 IdP(hosted UI → 마켓 IdP로 302 페더레이션,
JIT 사용자 `MarketSSO_<마켓 sub>`)로 로그인해 확인한다. 위키 API는 로그인 토큰과 같은 sub의 노트만 돌려준다.
실행: ./scripts/wiki.sh
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("ONTOLO_STATE_DIR", ROOT / "infra" / "state"))
OBO_STATE_DIR = Path(os.environ.get("ONTOLO_OBO_STATE_DIR", ROOT / "obo" / "infra" / "state"))
DOWNSTREAM = json.loads((OBO_STATE_DIR / "downstream.json").read_text())
WIKI = json.loads((STATE_DIR / "wiki.json").read_text())
# Agent IdP(마켓) — single logout 연쇄의 마지막 홉(마켓 세션까지 끊는다)
_INBOUND = OBO_STATE_DIR / "inbound.json"
INBOUND = json.loads(_INBOUND.read_text()) if _INBOUND.exists() else {}

HOST = os.environ.get("WIKI_HOST", "127.0.0.1")
PORT = int(os.environ.get("WIKI_PORT", "5182"))
BASE = os.environ.get("WIKI_BASE_URL", f"http://localhost:{PORT}")
HOSTED = DOWNSTREAM["hostedUiBase"]
CLIENT_ID = DOWNSTREAM["clientId"]
CLIENT_SECRET = DOWNSTREAM["clientSecret"]
SCOPES = "openid files/read"
NOTES_URL = WIKI["notesUrl"]
COOKIE = "ontolo-wiki"
SIGN_KEY = os.environ.get("WIKI_COOKIE_KEY", secrets.token_hex(16)).encode()
# 뷰어 세션은 짧게 — 만료되면 SSO를 다시 타서 "지금 IdP 세션이 누구인지"를 따른다
# (IdP에 세션이 있으면 폼 없이 즉시 돌아오므로 사용자는 느끼지 못한다)
SESSION_TTL_S = 10 * 60

app = FastAPI(title="온톨로 위키")


# ───────────────────────── 세션 쿠키 (HMAC 서명) ─────────────────────────

def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    mac = hmac.new(SIGN_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{mac}"


def _verify(cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    raw, mac = cookie.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(SIGN_KEY, raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        s = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    return s if s.get("exp", 0) > time.time() else None


def _claims(token: str) -> dict:
    try:
        p = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:  # noqa: BLE001
        return {}


# ───────────────────────── 페이지 ─────────────────────────

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Apple SD Gothic Neo','Noto Sans KR','Helvetica Neue',Arial,sans-serif;background:#f6f7f9;color:#1f2937}
header{background:#fff;border-bottom:1px solid #e5e7eb}
.bar{max-width:880px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:17px;color:#111827}
.brand i{display:inline-flex;width:30px;height:30px;border-radius:8px;background:#7c3aed;color:#fff;align-items:center;justify-content:center;font-style:normal;font-size:15px}
.brand small{font-weight:500;color:#6b7280;font-size:12px;margin-left:4px}
.who{font-size:12.5px;color:#6b7280;display:flex;gap:12px;align-items:center}
.who b{color:#111827}
.who a,.btn{color:#7c3aed;text-decoration:none;font-weight:600}
main{max-width:880px;margin:0 auto;padding:28px 24px 60px}
h1{font-size:22px;font-weight:800;margin-bottom:6px}
.lede{color:#6b7280;font-size:13.5px;margin-bottom:22px;line-height:1.6}
.note{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px 20px;margin-bottom:12px}
.note .lbl{font-size:10.5px;font-weight:700;letter-spacing:.06em;color:#9ca3af;text-transform:uppercase;margin-bottom:3px}
.note h2{font-size:15.5px;font-weight:700;margin-bottom:10px}
.note p{font-size:13.5px;line-height:1.65;color:#374151;white-space:pre-wrap}
.meta{margin-top:12px;padding-top:10px;border-top:1px dashed #e5e7eb;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:11.5px;color:#6b7280}
.meta span b{color:#9ca3af;font-weight:600;margin-right:4px}
.meta code{font-family:ui-monospace,monospace;font-size:11px;color:#6b7280}
.ok{display:inline-flex;align-items:center;gap:4px;background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;border-radius:999px;padding:1px 8px;font-size:11px;font-weight:700}
.sso{margin-bottom:18px;background:#f5f3ff;border:1px solid #ddd6fe;border-radius:10px;padding:10px 14px;font-size:12.5px;color:#5b21b6;line-height:1.6}
.empty{background:#fff;border:1px dashed #d1d5db;border-radius:12px;padding:40px;text-align:center;color:#6b7280;font-size:14px}
.login{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:40px;text-align:center;max-width:460px;margin:60px auto}
.login h1{font-size:20px}
.login p{color:#6b7280;font-size:13.5px;line-height:1.6;margin:10px 0 22px}
.login .btn{display:inline-block;background:#7c3aed;color:#fff;padding:11px 22px;border-radius:10px;font-size:14px}
.auth{margin-top:34px;border-top:1px solid #e5e7eb;padding-top:14px;font-size:11.5px;color:#9ca3af;line-height:1.7}
.auth code{font-family:ui-monospace,monospace;color:#6b7280;background:#f3f4f6;padding:1px 5px;border-radius:4px}
.err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;border-radius:10px;padding:14px 16px;font-size:13px}
"""


def _page(body: str, who: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>온톨로 위키</title><style>{CSS}</style></head>
<body><header><div class="bar"><div class="brand"><i>W</i>온톨로 위키<small>사내 위키 · 별도 시스템</small></div>
<div class="who">{who}</div></div></header><main>{body}</main></body></html>""")


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _kst(iso: str | None) -> str:
    """ISO-8601 UTC 문자열 → 'YYYY-MM-DD HH:MM KST'"""
    if not iso:
        return ""
    try:
        from datetime import datetime, timedelta, timezone
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=9)))
        return t.strftime("%Y-%m-%d %H:%M KST")
    except Exception:  # noqa: BLE001
        return iso


def _note_card(n: dict, me: str) -> str:
    """노트 한 장 — 제목/내용 라벨 + 메타(저장 시각·저장 계정·ID) + 동일 사용자 확인 배지."""
    author = n.get("author") or ""
    same = bool(me) and author == me
    badge = ('<span class="ok">✓ 저장한 계정 = 로그인 계정</span>' if same
             else '<span class="ok" style="background:#fef2f2;color:#991b1b;border-color:#fecaca">✗ 다른 계정</span>')
    return f"""<article class="note">
<div class="lbl">제목</div><h2>{_e(n.get('title'))}</h2>
<div class="lbl">내용</div><p>{_e(n.get('content'))}</p>
<div class="meta">
<span><b>저장 시각</b>{_e(_kst(n.get('created_at')))}</span>
<span><b>저장한 계정</b><code>{_e(author)}</code> {badge}</span>
<span><b>노트 ID</b><code>{_e(n.get('id'))}</code></span>
<span><b>저장 주체</b>쇼핑 에이전트 (사용자 위임 토큰)</span>
</div></article>"""


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    sess = _verify(request.cookies.get(COOKIE))
    if not sess:
        return _page(f"""
<div class="login"><h1>온톨로 위키</h1>
<p>사내 위키에는 별도 비밀번호가 없습니다.<br>마켓(회사 IdP)에 이미 로그인돼 있으면 <b>로그인 화면 없이</b> 같은 사용자로 들어옵니다.<br>
아니라면 회사 IdP 로그인 화면이 뜹니다.</p>
<a class="btn" href="/login">SSO로 들어가기</a>
<div class="auth">위키 IdP <code>{_e(DOWNSTREAM["poolId"])}</code> → 마켓 IdP로 302 페더레이션(OIDC) → IdP 브라우저 세션 상속<br>
이 화면의 로그인과 쇼핑 에이전트의 위임 저장은 같은 사용자 <code>MarketSSO_&lt;마켓 sub&gt;</code></div></div>""")

    token = sess["at"]
    c = _claims(token)
    me = c.get("username") or ""
    who = (f"<span>SSO 세션 <b>{_e(me)}</b></span>"
           f"<a href=\"/logout\" title=\"회사 IdP 세션까지 끊습니다 — 마켓도 함께 로그아웃됩니다\">로그아웃 (마켓 포함)</a>")
    try:
        r = requests.get(NOTES_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        data = r.json()
    except Exception as ex:  # noqa: BLE001
        return _page(f'<div class="err">위키 API 호출 실패: {_e(ex)}</div>', who)
    if r.status_code != 200:
        return _page(f'<div class="err">위키 API {r.status_code}: {_e(data)}</div>', who)

    notes = list(reversed(data.get("notes", [])))
    if notes:
        items = "".join(_note_card(n, me) for n in notes)
    else:
        items = '<div class="empty">아직 저장된 노트가 없습니다.<br>쇼핑 상담에서 "위키에 저장해줘"라고 하면 여기에 나타납니다.</div>'

    body = f"""<h1>내 노트 <span style="color:#9ca3af;font-weight:600;font-size:15px">{len(notes)}</span></h1>
<p class="lede">쇼핑 에이전트가 <b>내 명의(위임 토큰)</b>로 저장한 노트입니다. 위키 API는 이 로그인 토큰의 사용자(sub) 파일만 돌려주므로 다른 사용자의 노트는 구조적으로 보이지 않습니다.</p>
<div class="sso">🔐 이 화면의 사용자는 <b>회사 IdP(마켓) 세션에서 상속</b>된 것입니다 — 마켓에 로그인한 사람과 같은 사람입니다.
다른 사람으로 보려면 "로그아웃 (마켓 포함)"으로 IdP 세션을 끊고 그 사람으로 다시 로그인해야 합니다.</div>
{items}
<div class="auth">이 페이지의 인증 — 위키 IdP 발급 access token · iss <code>{_e(c.get('iss'))}</code> ·
client_id <code>{_e(c.get('client_id'))}</code> · scope <code>{_e(c.get('scope'))}</code> ·
sub <code>{_e(c.get('sub'))}</code><br>
위키 API(<code>{_e(NOTES_URL)}</code>)는 이 토큰의 JWKS·scope <code>files/read</code>를 검증하고 <code>wiki-notes/{{sub}}.json</code>만 돌려줍니다.</div>"""
    return _page(body, who)


@app.get("/login")
def login():
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "scope": SCOPES,
        "redirect_uri": f"{BASE}/callback", "state": state,
    })
    resp = RedirectResponse(f"{HOSTED}/oauth2/authorize?{q}", status_code=302)
    resp.set_cookie("ontolo-wiki-state", state, httponly=True, max_age=300, samesite="lax")
    return resp


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return _page(f'<div class="err">로그인 실패: {_e(error)} — {_e(error_description)}</div>')
    if not code or state != request.cookies.get("ontolo-wiki-state"):
        return _page('<div class="err">state 불일치 또는 code 없음 — 다시 로그인해 주세요. <a href="/login">로그인</a></div>')
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post(f"{HOSTED}/oauth2/token",
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "authorization_code", "client_id": CLIENT_ID,
                            "code": code, "redirect_uri": f"{BASE}/callback"}, timeout=15)
    if r.status_code != 200:
        return _page(f'<div class="err">토큰 교환 실패 {r.status_code}: {_e(r.text[:300])}</div>')
    tok = r.json()
    at = tok["access_token"]
    resp = RedirectResponse("/", status_code=302)
    ttl = min(SESSION_TTL_S, int(tok.get("expires_in", 3600)) - 60)
    resp.set_cookie(COOKIE, _sign({"at": at, "exp": time.time() + ttl}), httponly=True, samesite="lax")
    resp.delete_cookie("ontolo-wiki-state")
    return resp


@app.get("/logout")
def logout():
    """single logout 1/3 — 뷰어 쿠키 삭제 → Tool IdP 세션 종료 → /logged-out 으로 복귀."""
    q = urllib.parse.urlencode({"client_id": CLIENT_ID, "logout_uri": f"{BASE}/logged-out"})
    resp = RedirectResponse(f"{HOSTED}/logout?{q}", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/logged-out")
def logged_out():
    """single logout 2/3 — Tool IdP 세션 종료 후 Agent IdP(마켓) 세션도 끊는다(반쪽 로그아웃 방지).
    Agent IdP /logout → 마켓 SPA의 /sso-logout(3/3)이 로컬 세션을 지운다."""
    hosted, cid, back = INBOUND.get("hostedUiBase"), INBOUND.get("clientId"), INBOUND.get("ssoLogoutUrl")
    if hosted and cid and back:
        q = urllib.parse.urlencode({"client_id": cid, "logout_uri": back})
        return RedirectResponse(f"{hosted}/logout?{q}", status_code=302)
    return RedirectResponse("/", status_code=302)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
