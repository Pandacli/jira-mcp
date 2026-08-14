"""端到端自测:以真实 MCP 客户端身份拉起 server.py,验证握手 + 工具注册 + 一次工具调用。"""
import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command="python3",
        args=["server.py"],
        env=None,  # 继承当前环境(无凭据,预期工具调用返回 401 错误信息)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"握手成功,工具数 = {len(tools.tools)}")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description[:60]!r}")

            # 端到端调一次 jira_get_myself(无凭据 → 应返回 ok=false 的错误,而非抛异常)
            result = await session.call_tool("jira_get_myself", {})
            for c in result.content:
                print("\n[jira_get_myself 返回]", c.text[:300])


asyncio.run(main())
