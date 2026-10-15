"""jira_clone_issue 实连测试: 克隆一个缺陷到「手机APP联调项目」(T9MLBUG)。

用法(先确保网络可达,凭据走 ~/.hermes/config.yaml,不落盘密码):
    /home/tommy/miniconda3/envs/py312/bin/python3 test_clone.py <源issue key> [影响车型] [--copy]

- 第 1 个参数: 源 issue key(必填),如 T75BUG-2196
- 第 2 个参数: 影响车型(可选),不传则从源项目名自动推断
- 加 --copy 才拷贝评论/附件(默认关闭,减少副作用)

注意: 会在 T9MLBUG 里真实创建一条克隆缺陷 + Cloners 关联,测试后请自行删除。
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
        print("!! no JIRA_PASSWORD in config; cannot run authenticated clone")
        sys.exit(2)
    return {k: str(v) for k, v in env.items()}


async def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        sys.exit(1)
    source_key = args[0]
    vehicle_model = args[1] if len(args) > 1 and not args[1].startswith("--") else ""
    copy_flags = "--copy" in args

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = load_env_from_config()
    params = StdioServerParameters(command=SERVER_COMMAND, args=SERVER_ARGS, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            call_args = {
                "source_key": source_key,
                "copy_comments": copy_flags,
                "copy_attachments": copy_flags,
            }
            if vehicle_model:
                call_args["vehicle_model"] = vehicle_model
            result = await session.call_tool("jira_clone_issue", call_args)
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            print("".join(texts))


asyncio.run(main())
