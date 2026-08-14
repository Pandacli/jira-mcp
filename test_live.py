"""端到端实连测试:加载 .env 凭据,真实调用 Jira 只读接口验证连通性。"""
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(session, name, args):
    r = await session.call_tool(name, args)
    texts = [c.text for c in r.content if hasattr(c, "text")]
    return "\n".join(texts)


async def main():
    env = os.environ.copy()  # 继承已 source 的 .env
    params = StdioServerParameters(command="python3", args=["server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== jira_get_myself ===")
            print(await call(session, "jira_get_myself", {}))

            print("\n=== jira_list_projects ===")
            print(await call(session, "jira_list_projects", {}))

            print("\n=== jira_search (project 首个可见项目, 最近 3 条) ===")
            print(await call(session, "jira_search",
                             {"jql": "ORDER BY updated DESC", "max_results": 3}))


asyncio.run(main())
