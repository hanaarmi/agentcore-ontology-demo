"""통합 AgentCore Gateway를 통한 Neptune 개인화 MCP 호출 (데이터팀 VPC B).
Gateway가 interceptor로 X-Tier를 주입하고 Token Vault의 Tool IdP 위임 토큰(scope graph/read)을 붙여 MCP를 호출한다.
볼트에 토큰이 없으면 URL elicitation(-32042)으로 동의 URL이 온다. 전송은 mcp_session(세션 유지)에 위임.
"""
from __future__ import annotations

import mcp_session

available = mcp_session.available


def call(tool: str, arguments: dict, jwt: str, timeout: int = 75) -> dict:
    """통합 Gateway로 MCP tools/call → {"status": ok|consent|error, ...} (mcp_session.call 참조)."""
    return mcp_session.call(tool, arguments, jwt, timeout)
