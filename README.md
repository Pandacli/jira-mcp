# jira-mcp —— 自托管 Jira 的轻量 MCP Server

一个面向 **自托管 Jira Server 7.3.6**（私有化部署，如 `https://jira.gacrnd.com:8443`）
的最小 MCP 服务器。官方 Atlassian Rovo MCP 只支持 Cloud（`*.atlassian.net`），
这个项目填补自托管场景的空缺，直连 Jira REST API v2，通过 MCP 协议把 Jira 能力
暴露给 Claude Desktop / Cursor / Hermes / VS Code Copilot 等 AI 客户端。

本文档既包含快速上手，也包含完整的设计说明，供维护 / 扩展 / 接入使用。

---

## 目录

- [特性](#特性)
- [快速上手](#快速上手)
- [工具清单](#工具清单)
- [背景与动机](#背景与动机)
- [设计目标](#设计目标)
- [整体架构](#整体架构)
- [配置设计](#配置设计)
- [HTTP 客户端层设计](#http-客户端层设计)
- [工具 API 设计](#工具-api-设计)
- [抗截断设计](#抗截断设计)
- [内部接口专项设计](#内部接口专项设计)
- [错误处理设计](#错误处理设计)
- [关键设计决策与权衡](#关键设计决策与权衡)
- [测试与验证设计](#测试与验证设计)
- [已知限制与注意点](#已知限制与注意点)
- [扩展方向](#扩展方向)
- [附录：文件清单](#附录文件清单)

---

## 特性

- stdio 传输，任何 MCP 客户端都能挂（Claude Desktop / Cursor / Hermes / VS Code Copilot）
- Basic Auth 认证（7.3.6 无 PAT），支持自签证书（默认不校验 SSL）
- 13 个工具：搜索 / 查询 / 建单 / 改单 / 流转 / 评论 / 项目与类型枚举 / 身份校验 / 保存筛选器 / 问题表格
- 服务端做 payload 精简，抗 LLM 工具结果截断

## 快速上手

### 环境要求

- Python 3.10+ ，已装 `mcp`、`requests`

### 安装

```bash
cd /media/tommy/win_documents/code/mcp/jira-mcp
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 内容到你的客户端配置或 export 环境变量：

```bash
export JIRA_URL=https://jira.gacrnd.com:8443
export JIRA_USERNAME=<你的账号>
export JIRA_PASSWORD=<密码>
```

> 认证说明：Jira 7.3.6 没有 Personal Access Token（8.14 才引入），只能用
> Basic Auth（账号 + 密码）。建议用一个**专用服务账号**，别用个人主账号。
> 若账号走 SSO/LDAP，确认该账号能用密码做 REST 认证。

### 挂载示例

#### Claude Desktop（claude_desktop_config.json）

```json
{
  "mcpServers": {
    "jira-gac": {
      "command": "python3",
      "args": ["/media/tommy/win_documents/code/mcp/jira-mcp/server.py"],
      "env": {
        "JIRA_URL": "https://jira.gacrnd.com:8443",
        "JIRA_USERNAME": "你的账号",
        "JIRA_PASSWORD": "你的密码",
        "JIRA_VERIFY_SSL": "false"
      }
    }
  }
}
```

#### Hermes Agent

见 `hermes mcp` 相关命令或 `hermes-mcp-setup` skill，把上面的 command/args/env
加到 MCP 服务器列表即可（stdio 类型）。

### 快速自测

```bash
# 仅测工具注册(不连 Jira)
python3 - <<'PY'
import server
print("tools:", [t for t in dir(server) if t.startswith("jira_")])
PY
```

---

## 工具清单

| 工具 | 说明 |
|------|------|
| jira_get_myself | 校验账号可用(排查认证) |
| jira_search | JQL 搜索 issue |
| jira_list_filters | 列出当前账号的保存筛选器 |
| jira_search_by_filter | 按筛选器(id/名称)搜索 |
| jira_issue_table | 问题导航器表格(页面同款列配置) |
| jira_get_issue | 按 key 查单个 issue 全字段 |
| jira_list_projects | 列出可见项目 |
| jira_get_issue_types | 列项目可用 issue 类型(可能是中文) |
| jira_create_issue | 建单 |
| jira_update_issue | 改单(summary/description/assignee/priority) |
| jira_list_transitions | 列可用的状态流转 |
| jira_transition_issue | 按名称流转状态 |
| jira_add_comment | 加评论 |

---

## 背景与动机

官方 Atlassian **Rovo MCP Server**（`github.com/atlassian/atlassian-mcp-server`）仅面向
**Cloud**（`*.atlassian.net`），其入口 `mcp.atlassian.com` 无法代理自托管（Server / Data Center）
实例。本项目直连 **自托管 Jira Server 7.3.6** 的 REST API v2，填补自托管场景的空缺。

### 关键约束（决定了多项设计选择）

| 约束 | 影响 |
|------|------|
| Jira **7.3.6 < 8.14**，无 Personal Access Token（PAT 8.14 才引入） | 认证只能走 **Basic Auth**（账号 + 密码） |
| 自托管实例使用**自签证书**（8443 端口） | 默认关闭 SSL 校验，并抑制 `InsecureRequestWarning` |
| 社区 `sooperset/mcp-atlassian` 要求 8.14+ | 不依赖它，自己实现最小 server |
| 账号可能走 SSO/LDAP | Basic Auth 仍按用户目录校验；建议用**专用服务账号** |
| AI 客户端工具结果有大小上限（沙箱 ~ 数百 KB） | 搜索结果必须**精简摘要**，丢弃巨型字段 |

---

## 设计目标

1. **最小依赖、单文件**：`server.py` 一个文件即可运行，仅依赖 `mcp` + `requests`。
2. **stdio 传输**：任何 MCP 客户端都能挂，无需网络端口 / 进程常驻管理。
3. **AI 友好**：工具返回值统一 `{"ok": bool, ...}`，**永不抛异常**，错误信息可读，
   让 LLM 客户端能读取错误并继续，而不是被异常打断。
4. **抗截断**：对搜索 / 表格等大 payload 做结构化精简，只保留对 LLM 有用的字段。
5. **可诊断**：提供身份校验工具、端到端验证脚本，便于排查认证 / 连通性问题。

---

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│  MCP 客户端 (Claude Desktop / Cursor / Hermes / Copilot)     │
└───────────────────────────────┬─────────────────────────────┘
                                │ stdio (JSON-RPC 2.0)
                                │ initialize / tools/list / tools/call
┌───────────────────────────────▼─────────────────────────────┐
│  FastMCP("jira-gac")                ← MCP SDK 层             │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  13 个 @mcp.tool() 处理器                                │ │
│  │  jira_get_myself / jira_search / jira_list_filters /    │ │
│  │  jira_search_by_filter / jira_issue_table /             │ │
│  │  jira_get_issue / jira_create_issue / jira_update_issue │ │
│  │  jira_list_transitions / jira_transition_issue /        │ │
│  │  jira_add_comment / jira_list_projects /                │ │
│  │  jira_get_issue_types                                   │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  JiraClient          ← HTTP 客户端层（requests.Session）│ │
│  │  · Basic Auth / Bearer / SSL 开关                        │ │
│  │  · request() 统一请求 + 错误规整                         │ │
│  │  · post_issue_table() 内部接口专用（CSRF 头）            │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  JiraError           ← 错误规整层                        │ │
│  │  · errorMessages / errors → 可读字符串                   │ │
│  └─────────────────────────────────────────────────────────┘ │
└───────────────────────────────┬─────────────────────────────┘
                                │ HTTPS (verify 可关)
┌───────────────────────────────▼─────────────────────────────┐
│  自托管 Jira Server 7.3.6                                    │
│  · REST API v2   : /rest/api/2/...                          │
│  · 内部接口      : /rest/issueNav/1/issueTable               │
└─────────────────────────────────────────────────────────────┘
```

**分层职责**：
- **工具层**（`@mcp.tool()`）只做「入参校验 → 调 client → 精简 → 包 `_ok/_err`」，不含 HTTP 细节。
- **客户端层**（`JiraClient`）只做「拼 URL → 发请求 → 解析 JSON → 抛 `JiraError`」。
- **规整层**（`JiraError` / `_name` / `_summarize_*`）负责「原始 Jira 数据 → LLM 友好输出」。

---

## 配置设计

全部通过环境变量注入，避免硬编码凭据，便于客户端配置与 CI 隔离。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JIRA_URL` | `https://jira.gacrnd.com:8443` | Jira 根地址，末尾 `/` 会被 `rstrip` 去掉 |
| `JIRA_USERNAME` | 空 | Basic Auth 用户名（7.3.6 主认证方式） |
| `JIRA_PASSWORD` | 空 | Basic Auth 密码 |
| `JIRA_PERSONAL_TOKEN` | 空 | 可选；Jira 8.14+ 的 PAT，7.3.6 忽略 |
| `JIRA_VERIFY_SSL` | `false` | `true/false`；自签证书设 `false` |
| `JIRA_DEFAULT_PROJECT` | 空 | 预留：缺省 project key（当前工具未消费，留作扩展） |

**认证优先级**（见 `JiraClient.__init__`）：
1. 若设 `JIRA_PERSONAL_TOKEN` → `Authorization: Bearer <token>`（8.14+）；
2. 否则若设 `JIRA_USERNAME` → `requests` 的 `session.auth = (username, password)`（HTTP Basic）；
3. 两者皆无 → 匿名请求（工具调用会收到 401，规整为 `ok=false` 错误，不崩溃）。

**SSL 处理**：
```python
JIRA_VERIFY_SSL = os.environ.get("JIRA_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
if not JIRA_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```
- `session.verify = JIRA_VERIFY_SSL` 决定 TLS 证书校验；
- 关闭校验时抑制 `InsecureRequestWarning`，避免 stdio 上刷屏。

> **安全建议**：优先用专用服务账号而非个人主账号；`JIRA_DEFAULT_PROJECT` 为预留字段，
> 当前未在工具逻辑中消费，后续可用来简化建单参数。

---

## HTTP 客户端层设计

### JiraError — 错误规整

把 Jira 返回的错误体规整成一条可读字符串，供 LLM 直接展示：

```python
class JiraError(Exception):
    def __init__(self, status, data):
        self.status = status
        self.data = data
        msgs = []
        if isinstance(data, dict):
            msgs.extend(data.get("errorMessages") or [])      # 顶层错误消息数组
            msgs.extend(f"{k}: {v}" for k, v in (data.get("errors") or {}).items())  # 字段级错误
        super().__init__("; ".join(msgs) or f"HTTP {status}")
```

- 兼容 Jira 两种错误体：全局 `errorMessages`（字符串数组）与字段级 `errors`（dict）。
- 两者皆无时退化为 `HTTP <status>`，保证总有可读信息。

### JiraClient — 请求封装

```python
class JiraClient:
    def __init__(self):
        self.session = requests.Session()                 # 连接复用（连接池 / Keep-Alive）
        self.session.verify = JIRA_VERIFY_SSL
        self.session.headers["Accept"] = "application/json"
        # ... 认证设置见「配置设计」
```

- 使用 `requests.Session` 复用底层连接，避免每次工具调用重新握手（自签 8443 握手开销不小）。
- `_url(path)`：`JIRA_URL + "/rest/api/2/" + path.lstrip("/")`，统一 API v2 前缀。
- `request(method, path, **kwargs)`：核心统一方法——
  - 默认 `timeout=30`；
  - `204 No Content` → 返回 `None`；
  - 尝试 `resp.json()`，解析失败则退化为 `{"raw": resp.text[:2000]}`（截断防超大文本）；
  - `status >= 400` → 抛 `JiraError`。
- `get/post/put` 为便捷方法。
- `post_issue_table(...)`：内部接口专用（见「内部接口专项设计」），直接 POST 而非走
  `request()`，因为目标 `/rest/issueNav/1/issueTable` 不在 `/rest/api/2/` 下，且需要
  `form-urlencoded` body + CSRF 免检头 + XHR 头。

---

## 工具 API 设计

### 统一响应规约

每个工具**只返回 JSON，不抛异常**，两种形态：

```python
def _ok(data):  return {"ok": True,  "data": data}
def _err(e):    return {"ok": False, "error": str(e)}
```

- 成功：`{"ok": true, "data": <精简后的结构>}`
- 失败：`{"ok": false, "error": "<可读错误>"}`

> 设计意图：AI 客户端通过 `ok` 判断成败，失败时读 `error` 继续推理或重试，
> 不会被 Python 异常中断整条工具调用链路。

### 工具清单与数据流

| 工具 | 方法 | Jira 端点 | 写副作用 | 输出精简策略 |
|------|------|-----------|----------|--------------|
| `jira_get_myself` | GET | `/myself` | 无 | 原始（诊断用） |
| `jira_search` | GET | `/search` | 无 | `_summarize_search` |
| `jira_list_filters` | GET | `/filter/favourite` → `/filter/my` | 无 | `[{id,name,jql}]` |
| `jira_search_by_filter` | GET | `/filter/{id}` + `/search` | 无 | `_summarize_search` + `filter/jql` |
| `jira_issue_table` | POST | `/rest/issueNav/1/issueTable` | 无 | `_summarize_issue_table` |
| `jira_get_issue` | GET | `/issue/{key}` | 无 | 原始（单条） |
| `jira_list_projects` | GET | `/project` | 无 | `[{key,name,id,type}]` |
| `jira_get_issue_types` | GET | `/issue/createmeta` | 无 | `[{id,name}]` |
| `jira_create_issue` | POST | `/issue` | **有** | `{key,id,url}` |
| `jira_update_issue` | PUT | `/issue/{key}` | **有** | `{key,updated:[...]}` |
| `jira_list_transitions` | GET | `/issue/{key}/transitions` | 无 | `[{id,name}]` |
| `jira_transition_issue` | POST | `/issue/{key}/transitions` | **有** | `{key,transitioned_to}` |
| `jira_add_comment` | POST | `/issue/{key}/comment` | **有** | `{key,comment_id}` |

### 各工具设计要点

**jira_get_myself** — 身份诊断入口。返回 `client.get("myself")` 原始结果，用于排查
认证是否可用。匿名/密码错误时返回 `ok=false` 且含可读错误，不崩溃。

**jira_search(jql, max_results=20, fields=DEFAULT_FIELDS, raw=False)**
- `DEFAULT_FIELDS = "summary,status,priority,issuetype,assignee,reporter,created,updated"`，
  显式指定字段列表，**避免拉取全字段**（默认会带 avatarUrls/expand 等冗余）。
- `raw=False` 走精简；`raw=True` 返回 Jira 原始完整结果（调用方明确需要
  description/attachments 等大字段时才用）。

**jira_list_filters / jira_search_by_filter** — 保存筛选器支持
- Jira 保存的筛选器在 `GET /filter/favourite`（收藏）与 `/filter/my`（我的），
  每项含 `id/name/jql`。
- `_resolve_filter_jql(filter_ref)` 解析规则：
  1. `filter_ref` 是纯数字 → 当作 id 直接 `GET /filter/{id}` 读 jql；
  2. 否则在 `favourite` / `my` 两个端点里按 id 或名称匹配；
  3. 都未命中 → 返回 `ok=false`，提示可用 `jira_list_filters` 查看。
- 结果额外附 `filter`（名称）与 `jql`（解析出的 JQL），便于调用方继续扩展 JQL。

**jira_issue_table** — 问题导航器内部接口（详见「内部接口专项设计」）

**jira_get_issue(key)** — 按 key 查单条，返回原始字段（单条 payload 小，无需精简）。

**jira_get_issue_types(project_key)** — 建单前的类型枚举
- 调 `GET /issue/createmeta?projectKeys=<k>&expand=projects.issuetypes`；
- 返回 `[{id, name}]`。**name 可能本地化（中文「任务/缺陷」）**，建单前必须用它确认。

**jira_create_issue(project_key, summary, description="", issue_type="Task", assignee="", priority="")**
- `issue_type` / `priority` / `assignee` 均用**名称**（Server 版语义，非 Cloud 的 accountId）：
  - `issuetype: {"name": ...}`、`priority: {"name": ...}`、`assignee: {"name": ...}`；
  - 空值字段不写入 `fields`，避免误提交；
- 成功返回 `{key, id, url}`，`url` 直接拼成 `/browse/{key}` 供用户点击。

**jira_update_issue(key, summary/description/assignee/priority)**
- 只把**非空**字段放入 `fields`（增量更新）；
- 全空 → 返回 `ok=false`「没有传入任何要更新的字段」，避免无意义 PUT。

**jira_list_transitions / jira_transition_issue**
- `list` 返回 `[{id, name}]`；
- `transition` 支持按**名称或 id**定位目标流转（名称本地化，所以支持 id 兜底）；
- 未命中 → 返回 `ok=false` 并列出**所有可用名称**，方便客户端纠正。

---

## 抗截断设计

MCP 工具结果过大（如一次 raw 搜索 50 条 issue 约 180 KB）会撑爆 LLM 的工具结果沙箱。
本项目在**服务端**做精简，把体积压到 ~1/10：

### `_name(obj)` — 字段规整

Jira 字段值可能是 `dict`（如 `{"name": ..., "displayName": ..., "key": ...}`）、`str` 或
`None`。统一规整成可读字符串：

```python
def _name(obj):
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("displayName") or obj.get("key") or ""
    if isinstance(obj, str):
        return obj
    return ""
```

### `_summarize_issue` — 单条 issue 精简

```python
return {
    "key", "summary", "status", "priority", "type",
    "assignee", "reporter", "created", "updated",
}
```
- 丢弃 `avatarUrls` / `iconUrl` / `expand` / 嵌套对象等冗余字段；
- 状态/优先级/类型/经办人/报告人全部 flatten 成字符串。

### `_summarize_search` — 搜索结果精简

保留分页元信息 `total / startAt / maxResults` + 精简后的 `issues` 列表。

### `_summarize_issue_table` — 表格结果精简（最激进）

`issueTable` 接口返回的 `table` 字段是**渲染好的 HTML**（几百 KB）。只保留：

```python
total, displayed, page, pageSize, startIndex, sortBy, columnConfig, columns, issueKeys
```

丢弃 HTML `table` 字段本身。

---

## 内部接口专项设计

`jira_issue_table` 是对 Jira **问题导航器内部接口**的封装，与标准 REST v2 不同：

- 端点：`POST /rest/issueNav/1/issueTable`（**不在** `/rest/api/2/` 前缀下）；
- 请求体：`application/x-www-form-urlencoded`；
- 参数：`startIndex` / `filterId` / `jql` / `layoutKey`；
- 必要请求头：
  - `X-Atlassian-Token: no-check` — 跳过 Jira 的 XSRF 校验；
  - `X-Requested-With: XMLHttpRequest` — 标记为 AJAX 请求。

**与 `jira_search` 的区别**：本工具返回 **Jira 页面同款的列配置与排序**
（`columnConfig` / `sortBy`），更适合还原「我在 Jira 页面上看到的那个列表」。

**默认 JQL**：`assignee = currentUser() AND resolution = Unresolved order by updated DESC`，
即「当前账号未解决的问题」，配合 `jira-unresolved-issues` skill 使用。

---

## 错误处理设计

| 场景 | 行为 |
|------|------|
| HTTP 4xx/5xx | `request()` 抛 `JiraError`，工具捕获后 `_err(e)` |
| JSON 解析失败 | 退化为 `{"raw": text[:2000]}`，避免异常 |
| 204 无内容 | 返回 `None`（如 PUT 成功无 body） |
| 筛选器未命中 | `_err("找不到筛选器 ...")` 附排查提示 |
| 状态流转未命中 | `_err(...)` 附可用名称列表 |
| 更新无字段 | `_err("没有传入任何要更新的字段")` |
| 无凭据 | 请求返回 401 → 规整为 `ok=false`，不崩溃 |

**设计原则**：所有错误都走 `{"ok": false, "error": <可读文本>}`，错误文本尽量带上
「下一步建议」（如「可用 jira_list_filters 查看」）。

---

## 关键设计决策与权衡

| 决策 | 理由 | 权衡 |
|------|------|------|
| 单文件 `server.py` | 部署极简，stdio 挂载无路径依赖 | 扩展性靠新增函数，非模块化 |
| 服务端做 payload 精简 | 抗截断，省 LLM token | `raw=True` 需显式 opt-in 才能拿全量 |
| 认证走 Basic Auth | 7.3.6 无 PAT 的硬约束 | 密码明文走 HTTPS，需专用账号 |
| 默认关 SSL 校验 | 自签证书环境 | 有中间人风险，仅内网可接受 |
| 工具永不抛异常 | LLM 能读错误继续推理 | 客户端需自己判断 `ok` |
| `assignee` 用 `{"name": ...}` | Server 版语义 | Cloud 用 `accountId`，不可照抄 Cloud 示例 |
| 名称 + id 双通道定位（流转/筛选器） | 名称本地化易错，id 稳定 | 略增解析逻辑 |

---

## 测试与验证设计

项目内置三类验证脚本，分层覆盖：

| 脚本 | 层 | 说明 |
|------|----|------|
| `test_handshake.py` | 传输 + 注册 | 以真实 MCP 客户端身份拉起 server，验证握手 + 工具注册 + 一次调用（无凭据 → 应得 `ok=false` 而非异常） |
| `verify_server.py` | 端到端 | 从 `~/.hermes/config.yaml` 读凭据（不落盘密码），握手 + 断言 `jira_list_filters`/`jira_search_by_filter` 已注册 + 探测两工具 |
| `test_readonly.py` | 只读工具 | 直接 import server，覆盖 `get_issue_types` / `get_issue` / `list_transitions`（无写副作用）+ 错误分支 |
| `test_live.py` | 实连 | 加载 `.env`，真实调 `get_myself` / `list_projects` / `search` |

**验证原则**：不要只 import 模块做 Python 内省 —— 必须走 **MCP stdio 传输**做
`initialize` + `list_tools` + `call_tool`，才能发现传输层 / 注册层的 bug。

**已知坑**：stdio server 会把 INFO 日志（`Processing request of type ...`）打到 stdout，
握手时无害（客户端读 JSON-RPC 帧而非裸 stdout），但 shell 一行式调用时应 `2>/dev/null` 抑制。

---

## 已知限制与注意点

1. **建单前先确认类型名**：`issue_type` / `priority` 名称可能本地化（中文环境「任务/缺陷」），
   先用 `jira_get_issue_types` 确认精确名称。
2. **`description` 是纯字符串**：7.x 上 wiki/plain 渲染按字段配置，纯字符串两者兼容。
3. **代理会破坏自托管 Jira 的 TLS**：`jira.gacrnd.com` 解析到内网 IP，但 shell 的
   `https_proxy`（如 Clash `127.0.0.1:7897`）仍会命中，因为 `no_proxy` 的 CIDR 条目
   （`172.16.0.0/12`）匹配的是**主机名而非解析后的 IP**。CONNECT 隧道导致 TLS 握手
   失败报 `SSL: UNEXPECTED_EOF_WHILE_READING`（非证书错误）。修复：把主机名加入
   `no_proxy`（如 `.gacrnd.com`），或对 session 设 `trust_env=False` / 显式空代理。
4. **编辑 server.py 后需重启会话**：Hermes 在会话启动时发现 MCP 工具；改动后需
   `/reset`（CLI）/ `/restart`（gateway）才生效。
5. **`JIRA_DEFAULT_PROJECT` 尚未消费**：预留字段，后续可用于建单默认项目。
6. **工具永不抛异常**：每个工具返回 `{"ok": true, "data": ...}` 或
   `{"ok": false, "error": ...}`，失败不会抛异常，便于 AI 客户端读取错误继续处理。

---

## 扩展方向

- **分页遍历**：`jira_search` 当前单页返回，可加 `startAt` 遍历；
- **消费 `JIRA_DEFAULT_PROJECT`**：建单时缺省 project key；
- **附件 / 字段配置读取**：接入 `raw=True` 已有数据，封装成专用工具；
- **8.14+ PAT 兼容**：`JIRA_PERSONAL_TOKEN` 已预留，升级实例后自动走 Bearer；
- **权限白名单**：为写操作（建单/改单/流转/评论）加项目级 / 账号级开关，控制 AI 写权限。

---

## 附录：文件清单

| 文件 | 作用 |
|------|------|
| `server.py` | 主程序，全部工具 + 客户端实现 |
| `requirements.txt` | 依赖：`mcp>=1.20.0`、`requests>=2.31.0` |
| `README.md` | 本文档：快速上手 + 详细设计 |
| `.env.example` | 环境变量模板 |
| `verify_server.py` | 端到端握手 + 新工具探测 |
| `test_handshake.py` | stdio 握手 + 注册自测 |
| `test_readonly.py` | 只读工具覆盖测试 |
| `test_live.py` | 实连冒烟测试 |
