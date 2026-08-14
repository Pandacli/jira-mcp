#!/usr/bin/env python3
"""轻量 Jira MCP Server —— 面向自托管 Jira Server 7.3.6。

用法(stdio,给 Claude Desktop / Cursor / Hermes 等 MCP 客户端挂载):
    JIRA_URL=https://jira.gacrnd.com:8443 \
    JIRA_USERNAME=<账号> \
    JIRA_PASSWORD=<密码> \
    python3 server.py

环境变量:
    JIRA_URL              Jira 根地址,默认 https://jira.gacrnd.com:8443
    JIRA_USERNAME         登录账号(7.3.6 走 Basic Auth)
    JIRA_PASSWORD         登录密码
    JIRA_PERSONAL_TOKEN   可选;Jira 8.14+ 才有 PAT,7.3.6 忽略
    JIRA_VERIFY_SSL       true/false,自签证书时设 false(默认 false)
    JIRA_DEFAULT_PROJECT  可选;缺省 project key,省得每次传
"""
import os

from mcp.server.fastmcp import FastMCP
import requests
import urllib3
#获取账号信息
JIRA_URL = os.environ.get("JIRA_URL", "https://jira.gacrnd.com:8443").rstrip("/")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
JIRA_PASSWORD = os.environ.get("JIRA_PASSWORD", "")
JIRA_PERSONAL_TOKEN = os.environ.get("JIRA_PERSONAL_TOKEN", "")
JIRA_VERIFY_SSL = os.environ.get("JIRA_VERIFY_SSL", "false").lower() in ("1", "true", "yes")


if not JIRA_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
#获取默认项目
DEFAULT_PROJECT = os.environ.get("JIRA_DEFAULT_PROJECT", "")

API_BASE = "/rest/api/2/"
#定义 jira 字段
DEFAULT_FIELDS = "summary,status,priority,issuetype,assignee,reporter,created,updated"


#启动jira-gac mcp
mcp = FastMCP("jira-gac")


class JiraError(Exception):
    """把 Jira 返回的错误规整成可读信息。"""

    def __init__(self, status: int, data):
        self.status = status
        self.data = data
        msgs = []
        if isinstance(data, dict):
            msgs.extend(data.get("errorMessages") or [])
            msgs.extend(f"{k}: {v}" for k, v in (data.get("errors") or {}).items())
        super().__init__("; ".join(msgs) or f"HTTP {status}")


class JiraClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = JIRA_VERIFY_SSL
        self.session.headers["Accept"] = "application/json"
        if JIRA_PERSONAL_TOKEN:
            self.session.headers["Authorization"] = f"Bearer {JIRA_PERSONAL_TOKEN}"
        elif JIRA_USERNAME:
            self.session.auth = (JIRA_USERNAME, JIRA_PASSWORD)

    def _url(self, path: str) -> str:
        return JIRA_URL + API_BASE + path.lstrip("/")

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("timeout", 30)
        resp = self.session.request(method, self._url(path), **kwargs)
        if resp.status_code == 204:
            return None
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text[:2000]}
        if resp.status_code >= 400:
            raise JiraError(resp.status_code, data)
        return data

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def post_issue_table(self, jql: str, layout_key: str = "list-view",
                         filter_id: str = "-1", start_index: int = 0):
        """POST 到问题导航器内部接口(不在 /rest/api/2 下),form-urlencoded + CSRF 头。"""
        url = JIRA_URL + "/rest/issueNav/1/issueTable"
        resp = self.session.post(
            url,
            data={
                "startIndex": str(start_index),
                "filterId": str(filter_id),
                "jql": jql,
                "layoutKey": layout_key,
            },
            headers={
                "X-Atlassian-Token": "no-check",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {"raw": resp.text[:2000]}
            raise JiraError(resp.status_code, data)
        return resp.json()


client = JiraClient()


def _ok(data):
    return {"ok": True, "data": data}


def _err(e):
    return {"ok": False, "error": str(e)}


def _name(obj):
    """把 Jira 字段值(可能是 dict / str / None)规整成可读字符串。"""
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("displayName") or obj.get("key") or ""
    if isinstance(obj, str):
        return obj
    return ""


def _summarize_issue(issue: dict) -> dict:
    """把单个 issue 精简成 key + 摘要 + 状态/优先级/类型/经办人/报告人 + 时间,
    只保留对 LLM 有用的字段,去掉 avatarUrls/iconUrl/expand 等冗余,避免超大 payload。"""
    f = issue.get("fields", {}) or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": _name(f.get("status")),
        "priority": _name(f.get("priority")),
        "type": _name(f.get("issuetype")),
        "assignee": _name(f.get("assignee")),
        "reporter": _name(f.get("reporter")),
        "created": f.get("created"),
        "updated": f.get("updated"),
    }


def _summarize_search(data: dict) -> dict:
    """精简搜索结果:保留 total 计数 + 精简 issue 列表。"""
    return {
        "total": data.get("total"),
        "startAt": data.get("startAt"),
        "maxResults": data.get("maxResults"),
        "issues": [_summarize_issue(i) for i in (data.get("issues") or [])],
    }


def _summarize_issue_table(data: dict) -> dict:
    """精简 issueTable 结果:丢弃渲染好的 HTML table 字段(几百 KB),只留结构化字段。"""
    tbl = data.get("issueTable", {}) or {}
    return {
        "total": tbl.get("total"),
        "displayed": tbl.get("displayed"),
        "page": tbl.get("page"),
        "pageSize": tbl.get("pageSize"),
        "startIndex": tbl.get("startIndex"),
        "sortBy": tbl.get("sortBy"),
        "columnConfig": tbl.get("columnConfig"),
        "columns": tbl.get("columns"),
        "issueKeys": tbl.get("issueKeys") or [],
    }


@mcp.tool()
def jira_get_myself() -> dict:
    """验证当前登录账号是否可用,返回账号信息(用于排查认证问题)。"""
    try:
        return _ok(client.get("myself"))
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_search(jql: str, max_results: int = 20, fields: str = DEFAULT_FIELDS, raw: bool = False) -> dict:
    """用 JQL 搜索 issue,返回精简列表(key/summary/status/priority/type/assignee 等)。
    raw=True 时返回 Jira 原始完整结果(会很大)。"""
    try:
        data = client.get(
            "search",
            params={"jql": jql, "maxResults": max_results, "fields": fields},
        )
        return _ok(_summarize_search(data) if not raw else data)
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_list_filters() -> dict:
    """列出当前账号可用的 Jira 筛选器(保存的搜索),返回 id + 名称 + JQL。
    拿到筛选器 id 或名称后,可交给 jira_search_by_filter 执行。"""
    try:
        filters = []
        for endpoint in ("filter/favourite", "filter/my"):
            try:
                data = client.get(endpoint)
            except JiraError:
                continue
            items = data if isinstance(data, list) else (data.get("values") or [])
            for f in items:
                filters.append({"id": f.get("id"), "name": f.get("name"), "jql": f.get("jql")})
            if filters:
                break
        return _ok(filters)
    except JiraError as e:
        return _err(e)


def _resolve_filter_jql(filter_ref: str):
    """把筛选器 id 或名称解析成 (jql, name)。按 id 直读优先,其次按名称匹配。"""
    ref = str(filter_ref)
    # 1) 数字 → 当作 id 直接读
    if ref.isdigit():
        try:
            f = client.get(f"filter/{ref}")
            if f.get("jql"):
                return f.get("jql"), f.get("name")
        except JiraError:
            pass
    # 2) 按名称/id 在收藏/我的筛选器里匹配
    for endpoint in ("filter/favourite", "filter/my"):
        try:
            data = client.get(endpoint)
        except JiraError:
            continue
        items = data if isinstance(data, list) else (data.get("values") or [])
        for f in items:
            if str(f.get("id")) == ref or f.get("name") == filter_ref:
                return f.get("jql"), f.get("name")
    return None, None


@mcp.tool()
def jira_search_by_filter(filter_ref: str, max_results: int = 50, fields: str = DEFAULT_FIELDS) -> dict:
    """按筛选器(id 或名称)搜索 issue。先解析出筛选器的 JQL 再执行搜索,返回精简结果。
    筛选器列表可用 jira_list_filters 查询。"""
    try:
        jql, name = _resolve_filter_jql(filter_ref)
        if not jql:
            return _err(f"找不到筛选器 '{filter_ref}'(按 id 或名称都未命中,可用 jira_list_filters 查看)")
        data = client.get(
            "search",
            params={"jql": jql, "maxResults": max_results, "fields": fields},
        )
        summary = _summarize_search(data)
        summary["filter"] = name or filter_ref
        summary["jql"] = jql
        return _ok(summary)
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_issue_table(
    jql: str = "assignee = currentUser() AND resolution = Unresolved order by updated DESC",
    layout_key: str = "list-view",
    filter_id: str = "-1",
    start_index: int = 0,
) -> dict:
    """调 Jira 问题导航器内部接口 POST /rest/issueNav/1/issueTable(form-urlencoded),
    返回问题表格的精简结果(total/issueKeys/sortBy/columns),丢弃巨大的 HTML table 字段。
    默认 jql 筛"当前账号未解决的问题"(assignee = currentUser() AND resolution = Unresolved)。
    layout_key 用 list-view(纯列表)或 split-view;filter_id=-1 表示不基于保存的筛选器。
    与 jira_search 的区别:本工具返回 Jira 页面同款的列配置与排序。"""
    try:
        data = client.post_issue_table(
            jql=jql, layout_key=layout_key, filter_id=filter_id, start_index=start_index,
        )
        return _ok(_summarize_issue_table(data))
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_get_issue(key: str) -> dict:
    """按 key(如 GAC-123)获取单个 issue 的完整字段。"""
    try:
        return _ok(client.get(f"issue/{key}"))
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_list_projects() -> dict:
    """列出当前账号可见的所有 Jira 项目(key + 名称 + 项目类型)。"""
    try:
        data = client.get("project")
        return _ok([
            {"key": p.get("key"), "name": p.get("name"),
             "id": p.get("id"), "type": p.get("projectTypeKey")}
            for p in data
        ])
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_get_issue_types(project_key: str) -> dict:
    """列出某个项目可用的 issue 类型(建 issue 前用来确认类型名,可能是中文)。"""
    try:
        data = client.get(
            "issue/createmeta",
            params={"projectKeys": project_key, "expand": "projects.issuetypes"},
        )
        projects = (data.get("projects") or [])
        types = projects[0].get("issuetypes", []) if projects else []
        return _ok([{"id": t.get("id"), "name": t.get("name")} for t in types])
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    assignee: str = "",
    priority: str = "",
) -> dict:
    """在指定项目创建 issue。issue_type/priority 用名称(可能为中文),先调 jira_get_issue_types 确认。"""
    try:
        fields = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = description
        if assignee:
            fields["assignee"] = {"name": assignee}
        if priority:
            fields["priority"] = {"name": priority}
        data = client.post("issue", json={"fields": fields})
        return _ok({"key": data.get("key"), "id": data.get("id"),
                    "url": f"{JIRA_URL}/browse/{data.get('key')}"})
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_update_issue(
    key: str,
    summary: str = "",
    description: str = "",
    assignee: str = "",
    priority: str = "",
) -> dict:
    """更新 issue 的 summary/description/assignee/priority,只更新传入的字段。"""
    try:
        fields = {}
        if summary:
            fields["summary"] = summary
        if description:
            fields["description"] = description
        if assignee:
            fields["assignee"] = {"name": assignee}
        if priority:
            fields["priority"] = {"name": priority}
        if not fields:
            return _err("没有传入任何要更新的字段")
        client.put(f"issue/{key}", json={"fields": fields})
        return _ok({"key": key, "updated": list(fields.keys())})
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_list_transitions(key: str) -> dict:
    """列出某个 issue 当前可用的状态流转(transition id + 名称)。"""
    try:
        data = client.get(f"issue/{key}/transitions")
        trans = data.get("transitions", [])
        return _ok([{"id": t.get("id"), "name": t.get("name")} for t in trans])
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_transition_issue(key: str, transition_name: str) -> dict:
    """按名称流转 issue 状态(如 '开始处理' / '已解决'),先调 jira_list_transitions 确认名称。"""
    try:
        data = client.get(f"issue/{key}/transitions")
        target = None
        for t in data.get("transitions", []):
            if t.get("name") == transition_name or str(t.get("id")) == transition_name:
                target = t
                break
        if not target:
            names = [t.get("name") for t in data.get("transitions", [])]
            return _err(f"找不到状态 '{transition_name}',可用:{names}")
        client.post(f"issue/{key}/transitions", json={"transition": {"id": target["id"]}})
        return _ok({"key": key, "transitioned_to": target["name"]})
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_add_comment(key: str, body: str) -> dict:
    """给 issue 添加评论。"""
    try:
        data = client.post(f"issue/{key}/comment", json={"body": body})
        return _ok({"key": key, "comment_id": data.get("id")})
    except JiraError as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()
