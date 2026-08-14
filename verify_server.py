"""Verify the optimized jira-mcp server: handshake + new filter tools work end-to-end.

Reads JIRA_* credentials from ~/.hermes/config.yaml (the source of truth for the
Hermes MCP mount) so no password is hardcoded here or passed on the CLI.
"""
import asyncio
import os
import sys

import yaml

SERVER_COMMAND = "/home/tommy/miniconda3/envs/py312/bin/python3"
SERVER_ARGS = ["/media/tommy/win_documents/code/mcp/jira-mcp/server.py"]


def load_env_from_config():
    cfg_path = os.path.expanduser("~/.hermes/config.yaml")
    with open(cfg_path) as fh:
        cfg = yaml.safe_load(fh)
    block = cfg.get("mcp_servers", {}).get("jira-gac", {})
    env = block.get("env", {})
    if not env.get("JIRA_PASSWORD"):
        print("!! no JIRA_PASSWORD in config; cannot run authenticated probe")
        sys.exit(2)
    return {k: str(v) for k, v in env.items()}


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = load_env_from_config()
    params = StdioServerParameters(command=SERVER_COMMAND, args=SERVER_ARGS, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"handshake OK, {len(names)} tools:")
            for n in names:
                print(f"  - {n}")
            assert "jira_list_filters" in names, "missing jira_list_filters"
            assert "jira_search_by_filter" in names, "missing jira_search_by_filter"

            # probe new tools
            for tool, args in (
                ("jira_list_filters", {}),
                ("jira_search_by_filter", {"filter_ref": "huangpeibin", "max_results": 3}),
            ):
                result = await session.call_tool(tool, args)
                texts = [c.text for c in result.content if getattr(c, "text", None)]
                print(f"\n[{tool}] {''.join(texts)[:600]}")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
