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
import re
import tempfile
import threading
import time
from mcp.server.fastmcp import FastMCP
import requests
import urllib3

from dotenv import load_dotenv

#加载.env文件信息
load_dotenv()
JIRA_URL = os.environ.get("JIRA_URL", "https://jira.gacrnd.com:8443").rstrip("/")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
JIRA_PASSWORD = os.environ.get("JIRA_PASSWORD", "")
JIRA_PERSONAL_TOKEN = os.environ.get("JIRA_PERSONAL_TOKEN", "")
# jira_证书校验
JIRA_VERIFY_SSL = os.environ.get("JIRA_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
#如果来自 .env/os.environ.get，值是字符串，常见坑：JIRA_VERIFY_SSL=False -> 字符串 "False" 不等于 False，因此使用 not 逻辑非 更可靠
if not JIRA_VERIFY_SSL:
    '''
    #如果 JIRA_VERFIFY_SSL 为假值，让 urllib3 不再输出 InsecureRequestWarning 这个警告。
    urllib3.disable_warnings(...)   # urllib3 提供的函数，用来忽略警告
    urllib3.exceptions.InsecureRequestWarning  # 要忽略的警告类型
    访问 HTTPS，但关闭了证书校验。会报警告InsecureRequestWarning，忽略它就 不警告。
    很多内网系统、自签名证书、Jira、GitLab、Jenkins 等，在开发环境里会这么写：
    如果配置为不验证 SSL，就关闭证书校验，同时隐藏警告。如果配置为验证 SSL，就正常验证。
    '''
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)





def _env_float(name: str, default: float) -> float:
    """读浮点型环境变量,缺失或非法时回退默认值。"""
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


#获取默认项目
DEFAULT_PROJECT = os.environ.get("JIRA_DEFAULT_PROJECT", "")
#jira api
API_BASE = "/rest/api/2/"
#定义 jira 字段
DEFAULT_FIELDS = "summary,status,priority,issuetype,assignee,reporter,created,updated"

# ===== 缺陷克隆(PRD: 手机App问题 → clone++ 到「手机APP联调项目」) =====
CLONE_TARGET_PROJECT = "T9MLBUG"  # 手机APP联调项目(覆盖所有车型,根据【影响车型】确定车型来源)

# T9MLBUG 项目 BUG 类型的必填/关键自定义字段 id。
# 实例专属,若字段被重建,用 GET /rest/api/2/field 按中文名重新确认:
#   customfield_15405 = app系统    (多选: Android/IOS/鸿蒙)
#   customfield_12009 = 影响车型   (单选)
#   customfield_11616 = 再现率     (None/Once/Sporadic:10%/30%/50%/>70%/Systematic)
#   customfield_11600 = 严重程度   (S_Blocker/A_Critical/B_Major/C_Minor/D_Normal)
#   customfield_16000 = APP        (传祺/埃安/昊铂)
CF_APP_SYSTEM = "customfield_15405"
CF_VEHICLE_MODEL = "customfield_12009"
CF_REPRODUCIBILITY = "customfield_11616"
CF_APP = "customfield_16000"
CF_SEVERITY = "customfield_11600"

# 源 issue 克隆时需要拷贝的字段(只拉这些,避免 175KB 的全量 payload)

CLONE_SOURCE_FIELDS = "summary,description,customfield_11602,priority,labels,components,comment,attachment,project,issuetype"

# 优先级 → 严重程度 默认映射(克隆到 T9MLBUG 时「严重程度」是必填字段)
_PRIORITY_TO_SEVERITY = {
    "Blocker": "S_Blocker", "Critical": "A_Critical", "Highest": "A_Critical",
    "High": "A_Critical", "Medium": "B_Major", "Low": "C_Minor", "Lowest": "D_Normal",
}

# ===== 定时自动克隆扫描(jira_auto_clone_scan) =====
# 默认扫描条件:我的未完成、状态为「问题确认」、且尚未被克隆过(无 Cloners 关联)的问题。
# issueFunction not in hasLinkType("Cloners") 依赖 ScriptRunner,实例不支持时可改成
# 先用 jira_search 取回列表、再按 key 黑名单过滤。
# 已关闭/已解决等终态:自动克隆扫描要排除,只处理「问题未关闭」的缺陷
CLOSED_STATUSES = ("已关闭", "已解决", "关闭", "废弃", "驳回")

AUTO_SCAN_JQL = (
    'project != T9MLBUG '
    'AND assignee = currentUser() '
    'AND resolution = Unresolved '
    'AND status not in (' + ", ".join(CLOSED_STATUSES) + ') '
    'AND issueFunction not in hasLinkType("Cloners") '
    'AND status = 问题确认 '
    'ORDER BY updated DESC'
)

# 描述里出现 App 相关字眼才认为属于手机 App 问题(大小写不敏感)
AUTO_SCAN_APP_PATTERN = r"app"

# 关键字 → (app系统取值, 经办人)。顺序即优先级:鸿蒙 > IOS/苹果 > Android
AUTO_SCAN_SYSTEM_RULES = [
    ("鸿蒙", ["鸿蒙", "harmony", "harmonyos", "纯血鸿蒙"]),
    ("IOS", ["ios", "iphone", "ipad", "苹果", "apple"]),
    ("Android", ["android", "安卓", "andlog", "androidlog"]),
]
# app系统 → 经办人账号。2026-10-15 起统一流转给 dengjiajun,不再按系统分给
# 庄家达(zhuangjd) / 聂嘉宏(niejiahong001) / 盛希晨(shengxc)
AUTO_SCAN_DEFAULT_ASSIGNEE = os.environ.get("JIRA_AUTO_SCAN_ASSIGNEE", "dengjiajun")
AUTO_SCAN_ASSIGNEE = {
    "Android": AUTO_SCAN_DEFAULT_ASSIGNEE,
    "鸿蒙": AUTO_SCAN_DEFAULT_ASSIGNEE,
    "IOS": AUTO_SCAN_DEFAULT_ASSIGNEE,
}
# ===== 附件拷贝策略 =====
# 旧逻辑:单条 issue 附件总量 >15MB 就整体跳过 → 结果几乎永远不上附件(缺陷日志动辄几十 MB)。
# 新逻辑:总量超过阈值只是「改后台异步拷贝」,不再放弃;只有单个附件超过上限才跳过。
AUTO_SCAN_MAX_ATTACH_MB = _env_float("JIRA_ATTACH_SYNC_LIMIT_MB", 15)          # 总量阈值:> 该值走后台异步
AUTO_SCAN_MAX_SINGLE_ATTACH_MB = _env_float("JIRA_ATTACH_MAX_SINGLE_MB", 100)  # 单个附件上限:> 该值才跳过
ATTACH_DOWNLOAD_TIMEOUT_SEC = int(os.environ.get("JIRA_ATTACH_DOWNLOAD_TIMEOUT", "120"))
ATTACH_UPLOAD_TIMEOUT_SEC = int(os.environ.get("JIRA_ATTACH_UPLOAD_TIMEOUT", "300"))
ATTACH_RETRY = int(os.environ.get("JIRA_ATTACH_RETRY", "2"))
# T9MLBUG 再现率(必填)取不到时的缺省值;可从【出现概率】自动解析,这里兜底
DEFAULT_REPRODUCIBILITY = os.environ.get("JIRA_DEFAULT_REPRODUCIBILITY", "Systematic")



#初始化 name =jira-gac 的mcp
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

#初始化 jira-客户端
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


def _createmeta_fields(target_project: str, issue_type_name: str = "BUG") -> dict:
    """取目标项目某 issue 类型的字段元数据(required + allowedValues),用于克隆前校验/补全。

    返回形如 {field_key: {"required": bool, "allowedValues": ["..."]}} 的字典。
    取不到时返回空字典,调用方据此降级为「直接透传、由 Jira 报错」。"""
    data = client.get(
        "issue/createmeta",
        params={
            "projectKeys": target_project,
            "expand": "projects.issuetypes.fields",
        },
    )
    projects = data.get("projects") or []
    if not projects:
        return {}
    itypes = projects[0].get("issuetypes") or []
    chosen = None
    for it in itypes:
        if it.get("name") == issue_type_name:
            chosen = it
            break
    if chosen is None and itypes:
        chosen = itypes[0]
    if chosen is None:
        return {}
    out = {}
    for key, fld in (chosen.get("fields") or {}).items():
        out[key] = {
            "required": bool(fld.get("required")),
            "allowedValues": [av.get("value") for av in (fld.get("allowedValues") or [])],
        }
    return out


def _guess_vehicle_model(project_name, allowed_values) -> str:
    """从源项目名猜【影响车型】。去掉「项目质量问题/质量问题/项目」后缀后与允许值精确匹配,
    或做唯一前缀匹配(仅当唯一时才算数,避免歧义);都匹配不上返回空串。"""
    if not project_name or not allowed_values:
        return ""
    base = project_name
    for suffix in ("项目质量问题", "质量问题", "项目"):
        base = base.replace(suffix, "")
    base = base.strip(" -_")
    if not base:
        return ""
    if base in allowed_values:
        return base
    matches = [av for av in allowed_values if av.startswith(base)]
    if len(matches) == 1:
        return matches[0]
    return ""


def _copy_comments(source_key: str, new_key: str, src_fields: dict) -> int:
    """把源 issue 的评论逐条拷到新 issue,加「克隆自」前缀(best-effort,单条失败跳过)。"""
    comments = (src_fields.get("comment") or {}).get("comments") or []
    n = 0
    for c in comments:
        body = c.get("body") or ""
        author = _name(c.get("author"))
        prefix = f"[克隆自 {source_key}]" + (f" {author}:" if author else "") + "\n"
        try:
            client.post(f"issue/{new_key}/comment", json={"body": prefix + body})
            n += 1
        except JiraError:
            continue
    return n


def _download_attachment(att: dict) -> str:
    """流式下载源附件到本地临时文件,返回路径(调用方负责删除)。

    流式写盘而非 resp.content,避免几十 MB 的日志一次性吃满内存。"""
    resp = client.session.get(
        att["content"], stream=True, timeout=ATTACH_DOWNLOAD_TIMEOUT_SEC,
        headers={"Accept": "*/*"},
    )
    resp.raise_for_status()
    fd, path = tempfile.mkstemp(prefix="jira_att_")
    with os.fdopen(fd, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
    return path


def _upload_attachment(new_key: str, path: str, filename: str, mime: str) -> None:
    """把本地临时文件以 multipart 上传到目标 issue(走 client.request 统一报错)。"""
    with open(path, "rb") as fh:
        client.request(
            "POST",
            f"issue/{new_key}/attachments",
            files={"file": (filename, fh, mime)},
            headers={"X-Atlassian-Token": "no-check"},
            timeout=ATTACH_UPLOAD_TIMEOUT_SEC,
        )


def _copy_attachments(
    source_key: str,
    new_key: str,
    src_fields: dict,
    max_single_mb: float = AUTO_SCAN_MAX_SINGLE_ATTACH_MB,
) -> dict:
    """把源 issue 的附件逐个下载后上传到新 issue,单个失败不影响其余附件。

    返回 {"copied": n, "failed": n, "skipped": n, "files": [...]} ,
    files[i] = {filename, size_mb, status(copied/skipped/failed), error?};失败附带原因,便于定位。"""
    atts = src_fields.get("attachment") or []
    report = {"copied": 0, "failed": 0, "skipped": 0, "files": []}

    # 目标单已有附件:重跑(超时重试/手工补拷)时避免同名同大小文件被重复上传
    existing = set()
    try:
        tgt = client.get(f"issue/{new_key}", params={"fields": "attachment"})
        for x in (tgt.get("fields") or {}).get("attachment") or []:
            existing.add((x.get("filename"), int(x.get("size") or 0)))
    except Exception:
        pass

    for a in atts:
        content_url = a.get("content")
        filename = a.get("filename") or "attachment"
        size_mb = round(int(a.get("size") or 0) / 1024 / 1024, 2)
        item = {"filename": filename, "size_mb": size_mb, "status": "", "error": None}

        if not content_url:
            item["status"] = "failed"
            item["error"] = "源附件缺少 content 下载地址"
            report["failed"] += 1
            report["files"].append(item)
            continue
        if max_single_mb and size_mb > max_single_mb:
            item["status"] = f"skipped(单文件>{max_single_mb}MB)"
            report["skipped"] += 1
            report["files"].append(item)
            continue
        if existing and (filename, int(a.get("size") or 0)) in existing:
            item["status"] = "skipped(目标已存在同名同大小附件)"
            report["skipped"] += 1
            report["files"].append(item)
            continue

        for attempt in range(1, ATTACH_RETRY + 1):
            path = None
            try:
                path = _download_attachment(a)
                _upload_attachment(
                    new_key, path, filename,
                    a.get("mimeType") or "application/octet-stream",
                )
                item.update(status="copied", error=None, attempts=attempt)
                report["copied"] += 1
                break
            except Exception as e:  # 下载/上传任一环节失败都重试
                item["error"] = str(e)[:300]
                if attempt >= ATTACH_RETRY:
                    item["status"] = "failed"
                    report["failed"] += 1
                else:
                    time.sleep(2 * attempt)
            finally:  # 每轮都清掉临时文件,避免重试时残留占满磁盘
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        report["files"].append(item)

    return report


# 后台附件拷贝任务表(job_id → job),用于大附件异步上传
_ATTACH_JOBS: dict = {}
_ATTACH_JOBS_LOCK = threading.Lock()


def _start_attachment_job(
    source_key: str,
    new_key: str,
    src_fields: dict,
    max_single_mb: float = AUTO_SCAN_MAX_SINGLE_ATTACH_MB,
) -> dict:
    """后台线程拷贝附件,立即返回 job 信息。

    大附件同步拷贝会把整次 jira_auto_clone_scan 拖到客户端超时(单已创建但工具报错),
    异步化后主流程先返回克隆结果,附件在后台继续传。"""
    job_id = f"att-{source_key}-to-{new_key}-{int(time.time() * 1000)}"
    job = {
        "job_id": job_id, "source": source_key, "clone": new_key,
        "status": "running", "result": None, "started_at": time.time(),
    }
    with _ATTACH_JOBS_LOCK:
        _ATTACH_JOBS[job_id] = job

    def _run():
        try:
            job["result"] = _copy_attachments(source_key, new_key, src_fields, max_single_mb)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "failed"
            job["result"] = {"error": str(e)[:300]}
        job["finished_at"] = time.time()

    # 非 daemon:进程退出前会等线程跑完,避免一次性调用(CLI)提前退出截断上传;
    # 对常驻的 MCP 服务无影响(工具立即返回,不阻塞调用方)
    threading.Thread(target=_run, name=f"attach-copy-{job_id}", daemon=False).start()
    return {"job_id": job_id, "status": "running", "source": source_key, "clone": new_key}


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


@mcp.tool()
def _parse_reproducibility(text: str) -> str:
    """从问题描述解析【出现概率】,映射成 T9MLBUG 再现率(customfield_11616)的合法值:
    100%→Systematic,>70%→Sporadic:>70%,50%/30%/10%→对应档位,<10%→Once。"""
    m = re.search(r"出现概率[】\]]?[：:]\s*(\d+)\s*%", text)
    if not m:
        return ""
    p = int(m.group(1))
    if p >= 100:
        return "Systematic"
    if p > 70:
        return "Sporadic:>70%"
    if p >= 50:
        return "Sporadic:50%"
    if p >= 30:
        return "Sporadic:30%"
    if p >= 10:
        return "Sporadic:10%"
    return "Once"


def _guess_module(text: str, fallback: str = "车控") -> str:
    """根据摘要/描述关键词猜测 T9MLBUG 的「模块」(组件)名,取不到时用 fallback。"""
    rules = [
        ("透气", "透气模式"),
        ("一键备车", "一键备车"),
        ("蓝牙", "蓝牙车控/激活"),
        ("NFC", "NFC钥匙"),
        ("OTA", "OTA"),
        ("哨兵", "哨兵模式"),
        ("地图", "地图"),
        ("定位", "车辆位置"),
        ("车辆位置", "车辆位置"),
        ("充电", "充电中心"),
        ("车模", "3D车模"),
        ("弹窗", "弹窗"),
    ]
    for kw, comp in rules:
        if kw in text:
            return comp
    if "远控" in text or "远程" in text:
        return "基础远控"
    return fallback


def jira_clone_issue(
    source_key: str,
    target_project: str = CLONE_TARGET_PROJECT,
    vehicle_model: str = "",
    app_system: str = "",
    reproducibility: str = "",
    severity: str = "",
    app: str = "传祺",
    component: str = "",
    assignee: str = "",
    copy_comments: bool = True,
    copy_attachments: bool = True,
    keep_source_components: bool = True,
    attachments_async: bool = False,
    max_single_attach_mb: float = AUTO_SCAN_MAX_SINGLE_ATTACH_MB,
) -> dict:
    """把缺陷 clone++ 到「手机APP联调项目」(默认 T9MLBUG)—— PRD 缺陷分配流程第 3 步。

    拷贝摘要/描述/优先级/标签/组件,设置目标项目必填字段(影响车型/严重程度等),
    创建 Cloners 关联,并按需拷贝评论与附件(copy_comments/copy_attachments)。
    影响车型(vehicle_model)不确定时留空,工具会尝试从源项目名推断,失败则返回可选值列表。

    附件:默认逐个同步拷贝(流式落盘 + 失败重试 + 逐文件结果报告);
    attachments_async=True 时改后台线程拷贝,立即返回 job_id(大附件避免调用超时)。
    单文件超过 max_single_attach_mb(默认 100MB)才跳过并在结果里标注原因。"""
    try:
        src = client.get(f"issue/{source_key}", params={"fields": CLONE_SOURCE_FIELDS})
        if not src:
            return _err(f"找不到源 issue '{source_key}'")
        sf = src.get("fields", {}) or {}

        # 目标项目字段元数据(必填项 + 允许值),用于校验与补全
        meta = _createmeta_fields(target_project, "BUG")
        vm_allowed = meta.get(CF_VEHICLE_MODEL, {}).get("allowedValues") or []

        # —— 影响车型:入参优先,否则从源项目名推断 ——
        vm = vehicle_model or _guess_vehicle_model(_name(sf.get("project")), vm_allowed)
        if not vm:
            return _err(f"无法确定【影响车型】,请传入 vehicle_model,可选值:{vm_allowed}")
        if vm_allowed and vm not in vm_allowed:
            return _err(f"影响车型 '{vm}' 不在目标项目可选值内,可选值:{vm_allowed}")

        # —— 组装 fields ——
        fields = {
            "project": {"key": target_project},
            "issuetype": {"name": "BUG"},
            "summary": sf.get("summary"),
        }
        # 描述:description 为空时回退到「问题描述」自定义字段,避免重现步骤等关键内容丢失;
        # 目标项目不支持 description 时(如 T9MLBUG 用 customfield_11602 存重现步骤)改写入该字段
        desc = sf.get("description") or sf.get("customfield_11602")
        if desc:
            if "description" in meta:
                fields["description"] = desc
            elif "customfield_11602" in meta:
                fields["customfield_11602"] = desc
        # 再现率/模块是 T9MLBUG 必填:入参没给时自动补全(从【出现概率】解析 + 关键词猜模块),
        # 否则建单会被 Jira 拒绝(再现率是必需的/模块是必需的)
        src_text = f"{sf.get('summary') or ''}\n{sf.get('description') or ''}\n{sf.get('customfield_11602') or ''}"
        reproducibility = reproducibility or _parse_reproducibility(src_text) or DEFAULT_REPRODUCIBILITY
        if not component and not (keep_source_components and sf.get("components")):
            component = _guess_module(src_text)
        pri = _name(sf.get("priority"))
        if pri:
            fields["priority"] = {"name": pri}
        # 标签:仅当目标项目创建界面支持 labels 时才拷贝,否则 Jira 会拒绝整个创建
        if sf.get("labels") and "labels" in meta:
            fields["labels"] = sf["labels"]
        # 组件:入参优先,否则复用源组件名(目标项目存在同名组件才有效)
        if component:
            fields["components"] = [{"name": component}]
        elif keep_source_components and sf.get("components"):
            fields["components"] = [{"name": _name(c)} for c in sf["components"]]
        fields[CF_VEHICLE_MODEL] = {"value": vm}
        if app_system:
            fields[CF_APP_SYSTEM] = [{"value": s.strip()} for s in app_system.split(",") if s.strip()]
        if reproducibility:
            fields[CF_REPRODUCIBILITY] = {"value": reproducibility}
        if app:
            fields[CF_APP] = {"value": app}
        sev = severity or _PRIORITY_TO_SEVERITY.get(pri, "")
        if sev:
            fields[CF_SEVERITY] = {"value": sev}
        if assignee:
            fields["assignee"] = {"name": assignee}

        created = client.post("issue", json={"fields": fields})
        new_key = created.get("key")

        # —— 创建 Cloners 关联(outward=源, inward=克隆) ——
        link_created = False
        if new_key:
            try:
                client.post("issueLink", json={
                    "type": {"name": "Cloners"},
                    "outwardIssue": {"key": source_key},
                    "inwardIssue": {"key": new_key},
                })
                link_created = True
            except JiraError:
                pass  # 关联失败不致命,继续拷贝评论/附件

        n_comments = _copy_comments(source_key, new_key, sf) if (copy_comments and new_key) else 0
        attach_report = {"copied": 0, "failed": 0, "skipped": 0, "files": []}
        attach_job = None
        if copy_attachments and new_key:
            if attachments_async:
                attach_job = _start_attachment_job(source_key, new_key, sf, max_single_attach_mb)
            else:
                attach_report = _copy_attachments(source_key, new_key, sf, max_single_attach_mb)

        return _ok({
            "source": source_key,
            "clone_key": new_key,
            "url": f"{JIRA_URL}/browse/{new_key}",
            "vehicle_model": vm,
            "severity": sev or None,
            "link_created": link_created,
            "copied": {"comments": n_comments, "attachments": attach_report["copied"]},
            "attachments_detail": attach_report,
            "attachment_job": attach_job,
        })
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_copy_attachments(
    source_key: str,
    target_key: str,
    async_mode: bool = False,
    max_single_mb: float = AUTO_SCAN_MAX_SINGLE_ATTACH_MB,
) -> dict:
    """单独补拷附件:把 source_key 的附件拷到 target_key(用于克隆单漏传附件时补救)。

    async_mode=True 时后台执行并立即返回 job_id,用 jira_attachment_job_status 查进度。
    单文件超过 max_single_mb(默认 100MB)才跳过,并在结果里给出逐文件状态。"""
    try:
        src = client.get(f"issue/{source_key}", params={"fields": "attachment"})
        sf = (src or {}).get("fields") or {}
        atts = sf.get("attachment") or []
        base = {
            "source": source_key,
            "target": target_key,
            "url": f"{JIRA_URL}/browse/{target_key}",
            "attachment_count": len(atts),
            "attachment_mb": _attachment_mb(atts),
        }
        if not atts:
            base.update(copied=0, files=[], note="源单无附件")
            return _ok(base)
        if async_mode:
            return _ok({**base, **_start_attachment_job(source_key, target_key, sf, max_single_mb)})
        return _ok({**base, **_copy_attachments(source_key, target_key, sf, max_single_mb)})
    except JiraError as e:
        return _err(e)


@mcp.tool()
def jira_attachment_job_status(job_id: str = "") -> dict:
    """查询后台附件拷贝任务状态。传 job_id 查单个;留空返回全部任务(按启动时间倒序的前 20 条)。"""
    with _ATTACH_JOBS_LOCK:
        if job_id:
            job = _ATTACH_JOBS.get(job_id)
            return _ok(job) if job else _err(f"找不到任务 '{job_id}'")
        jobs = sorted(_ATTACH_JOBS.values(), key=lambda j: j.get("started_at") or 0, reverse=True)[:20]
        return _ok({"total": len(_ATTACH_JOBS), "jobs": jobs})


def _detect_app_system(text: str) -> str:
    """从摘要/描述文本判断手机系统,返回 Android / IOS / 鸿蒙;判断不出返回空串。

    优先级:鸿蒙 > IOS(苹果) > Android —— 与人工分组习惯一致,避免「苹果手机」被 androidlog 之类的
    日志文件名误判成 Android。"""
    t = (text or "").lower()
    if not t:
        return ""
    for system, keywords in AUTO_SCAN_SYSTEM_RULES:
        for kw in keywords:
            if kw.lower() in t:
                return system
    return ""


def _attachment_mb(attachments) -> float:
    total = sum(int(a.get("size") or 0) for a in (attachments or []))
    return round(total / 1024 / 1024, 2)


@mcp.tool()
def jira_auto_clone_scan(
    jql: str = AUTO_SCAN_JQL,
    max_results: int = 50,
    target_project: str = CLONE_TARGET_PROJECT,
    fallback_system: str = "Android",
    copy_comments: bool = True,
    copy_attachments: bool = True,
    dry_run: bool = False,
    attachments_sync_limit_mb: float = AUTO_SCAN_MAX_ATTACH_MB,
    max_single_attach_mb: float = AUTO_SCAN_MAX_SINGLE_ATTACH_MB,
) -> dict:
    """定时扫描 → 自动克隆:按 JQL 找出候选问题,只处理「描述含 App 字眼」的,自动克隆到 T9MLBUG
    并统一分派经办人(默认 dengjiajun,可用 JIRA_AUTO_SCAN_ASSIGNEE 覆盖;
    2026-10-15 起不再按系统分给庄家达/聂嘉宏/盛希晨)。

    判定规则:
      1) 摘要 + 描述 + 「问题描述」自定义字段里出现 app(不区分大小写)才算手机 App 问题;
      2) 系统判定优先级 鸿蒙 > IOS(苹果/iphone/ipad) > Android,判断不出时用 fallback_system;
      2a) 必填字段自动补全:再现率从【出现概率】解析(取不到用 DEFAULT_REPRODUCIBILITY),
          模块按关键词猜测(透气/远控/充电等,兜底「车控」),避免建单被 Jira 拒绝;
      3) 附件默认一律拷贝:单条 issue 附件总量 <= attachments_sync_limit_mb(默认 15MB)同步拷贝,
         超过则自动改后台线程异步拷贝(返回 attachment_job,不阻塞、不放弃);
         只有单个附件超过 max_single_attach_mb(默认 100MB)才跳过并在结果里标注原因;
      4) 已存在 Cloners 关联的单由 JQL 排除,不会重复克隆。

    dry_run=True 时只返回「计划分派」不真正建单,建议首次运行或调规则时先跑一次。"""
    try:
        data = client.get("search", params={
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,description,customfield_11602,priority,components,project,attachment,status",
        })
    except JiraError as e:
        return _err(e)

    issues = data.get("issues") or []
    app_re = re.compile(AUTO_SCAN_APP_PATTERN, re.IGNORECASE)
    cloned, skipped = [], []

    for issue in issues:
        key = issue.get("key")
        f = issue.get("fields", {}) or {}
        summary = f.get("summary") or ""
        desc = " ".join(str(x) for x in (f.get("description"), f.get("customfield_11602")) if x)
        text = f"{summary}\n{desc}"

        if not app_re.search(text):
            skipped.append({"key": key, "summary": summary, "reason": "描述不含 App 字眼"})
            continue

        system = _detect_app_system(text) or fallback_system
        assignee = AUTO_SCAN_ASSIGNEE.get(system, "")
        att_list = f.get("attachment") or []
        attach_mb = _attachment_mb(att_list)
        # 附件策略:总量小 → 同步拷;总量大 → 后台异步拷;都不再直接跳过
        async_attach = copy_attachments and attach_mb > attachments_sync_limit_mb
        attach_mode = "off" if not copy_attachments else ("async" if async_attach else "sync")
        plan = {
            "key": key,
            "summary": summary,
            "app_system": system,
            "assignee": assignee,
            "reproducibility": _parse_reproducibility(text) or DEFAULT_REPRODUCIBILITY,
            "component": _guess_module(text),
            "attachment_count": len(att_list),
            "attachment_mb": attach_mb,
            "copy_attachments": copy_attachments,
            "attachment_mode": attach_mode,
            "attachment_skip_oversize": [
                (a.get("filename"), round(int(a.get("size") or 0) / 1024 / 1024, 2))
                for a in att_list
                if max_single_attach_mb
                and int(a.get("size") or 0) / 1024 / 1024 > max_single_attach_mb
            ],
        }
        if dry_run:
            plan["action"] = "plan"
            cloned.append(plan)
            continue

        # 先按源组件名克隆;若目标项目无同名组件导致创建失败,则去掉组件重试一次
        res = jira_clone_issue(
            source_key=key,
            target_project=target_project,
            app_system=system,
            assignee=assignee,
            reproducibility=plan["reproducibility"],
            component=plan["component"],
            copy_comments=copy_comments,
            copy_attachments=copy_attachments,
            attachments_async=async_attach,
            max_single_attach_mb=max_single_attach_mb,
        )
        if not res.get("ok") and "components" in str(res.get("error", "")).lower():
            res = jira_clone_issue(
                source_key=key,
                target_project=target_project,
                app_system=system,
                assignee=assignee,
                reproducibility=plan["reproducibility"],
                component=plan["component"],
                copy_comments=copy_comments,
                copy_attachments=copy_attachments,
                attachments_async=async_attach,
                max_single_attach_mb=max_single_attach_mb,
                keep_source_components=False,
            )
            plan["retried_without_components"] = True

        if res.get("ok"):
            plan.update(res.get("data") or {})
            plan["action"] = "cloned"
            cloned.append(plan)
        else:
            plan["action"] = "failed"
            plan["error"] = res.get("error")
            skipped.append(plan)

    return _ok({
        "jql": jql,
        "total_matched": data.get("total"),
        "scanned": len(issues),
        "cloned_count": len(cloned),
        "skipped_count": len(skipped),
        "cloned": cloned,
        "skipped": skipped,
    })


if __name__ == "__main__":
    mcp.run()
