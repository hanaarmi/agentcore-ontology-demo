"""통합 AgentCore Gateway를 통한 온톨로 위키(3rd-party) MCP 호출 (위키팀 VPC C).
Gateway가 Token Vault의 위키 IdP 위임 토큰을 붙여 위키 MCP를 호출하고, 없으면 URL elicitation(-32042)으로 동의 URL이 온다.
토큰은 에이전트에 노출되지 않으므로 신원 가드는 툴 응답의 위키 사용자명(MarketSSO_<마켓 sub>)으로 한다. 전송은 mcp_session에 위임.
"""
from __future__ import annotations

import mcp_session

available = mcp_session.available


def call(tool: str, arguments: dict, jwt: str, timeout: int = 75) -> dict:
    """통합 Gateway로 MCP tools/call → {"status": ok|consent|error, ...} (mcp_session.call 참조)."""
    return mcp_session.call(tool, arguments, jwt, timeout)
