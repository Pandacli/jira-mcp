#!/usr/bin/env bash
# =============================================================
# jira-mcp.sh —— 启动 Jira MCP Server (jira-gac)
#
# 用法:
#   ./jira-mcp.sh           直接启动(stdio 模式, 供 MCP 客户端拉起)
#   ./jira-mcp.sh status    查看服务状态
#   ./jira-mcp.sh restart   重启开机自启服务
#   ./jira-mcp.sh stop      停止开机自启服务
#
# 开机自启: 由 systemd user service 托管 (jira-mcp.service)
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="jira-mcp.service"
PY312="/home/tommy/miniconda3/envs/py312/bin/python"

# ---------- 子命令: 服务管理 ----------
if [ "${1:-}" = "status" ]; then
    systemctl --user status "$SERVICE_NAME" --no-pager
    exit $?
elif [ "${1:-}" = "restart" ]; then
    systemctl --user restart "$SERVICE_NAME"
    echo "[jira-mcp] 已重启 $SERVICE_NAME"
    exit 0
elif [ "${1:-}" = "stop" ]; then
    systemctl --user stop "$SERVICE_NAME"
    echo "[jira-mcp] 已停止 $SERVICE_NAME"
    exit 0
fi

# ---------- 环境检查 ----------
if [ ! -x "$PY312" ]; then
    echo "[jira-mcp] ERROR: 找不到 conda py312 python: $PY312" >&2
    echo "[jira-mcp] 请先创建环境: conda create -n py312 python=3.12" >&2
    exit 1
fi
if [ ! -f "$SCRIPT_DIR/server.py" ]; then
    echo "[jira-mcp] ERROR: 找不到 server.py: $SCRIPT_DIR/server.py" >&2
    exit 1
fi

# ---------- 注入 .env 环境变量(JIRA_URL/USERNAME/PASSWORD 等) ----------
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

# ---------- 启动 MCP Server (stdio) ----------
cd "$SCRIPT_DIR"
echo "[jira-mcp] 启动 Jira MCP Server (py312) ..." >&2
exec "$PY312" "$SCRIPT_DIR/server.py" "$@"
