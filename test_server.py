#!/usr/bin/env python3
"""server.py 单元测试(离线,不访问真实 Jira)。

运行:
    python3 -m unittest test_server -v
    # 或用项目 conda 环境
    /home/tommy/miniconda3/envs/py312/bin/python -m unittest test_server -v

覆盖点(对应 server.py 的关键行为契约):
    1. 工具函数:_name / _summarize_* / _env_float / _attachment_mb
    2. 判定逻辑:_detect_app_system(鸿蒙 > IOS > Android)、_parse_reproducibility、_guess_module、_guess_vehicle_model
    3. 附件拷贝:_copy_attachments(成功/超大跳过/缺下载地址/失败重试/去重/临时文件清理)、后台任务
    4. 克隆流程:jira_clone_issue 必填字段自动补全、关联创建、经办人分派
    5. 自动扫描:jira_auto_clone_scan dry_run 计划、App 过滤、JQL 排除终态
"""
import os
import sys
import types
import glob
import shutil
import tempfile
import time
import unittest

# ---- 让 server.py 可离线导入:本地 mcp 2.x 没有 fastmcp 子模块时打桩 ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("JIRA_USERNAME", "tester")
os.environ.setdefault("JIRA_PASSWORD", "pwd")
try:
    from mcp.server.fastmcp import FastMCP  # noqa: F401
except Exception:
    mcp_mod = sys.modules.get("mcp") or types.ModuleType("mcp")
    srv_mod = types.ModuleType("mcp.server")
    fast_mod = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:  # 桩:tool() 原样返回函数,便于直接调用被测函数
        def __init__(self, name=""):
            self.name = name

        def tool(self, *a, **k):
            return lambda fn: fn

        def run(self, *a, **k):
            pass

    fast_mod.FastMCP = FastMCP
    srv_mod.fastmcp = fast_mod
    mcp_mod.server = srv_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = srv_mod
    sys.modules["mcp.server.fastmcp"] = fast_mod

import server  # noqa: E402

MB = 1024 * 1024
TMP_PREFIX = "jira_att_"


# ============================ 测试替身 ============================
class FakeResponse:
    """模拟 requests.Response,只实现 server.py 用到的接口。"""

    def __init__(self, status_code=200, payload=None, chunks=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._chunks = chunks or []
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        for c in self._chunks:
            yield c


class FakeSession:
    """模拟 requests.Session:记录下载/上传,可注入上传失败次数。"""

    def __init__(self, download_bytes=b"x" * 32, fail_upload_times=0):
        self.download_bytes = download_bytes
        self.fail_upload_times = fail_upload_times
        self.downloads = []
        self.uploads = []
        self.upload_calls = 0

    def get(self, url, **kw):
        self.downloads.append({"url": url, "stream": kw.get("stream")})
        return FakeResponse(chunks=[self.download_bytes])

    def request(self, method, url, **kw):
        files = kw.get("files") or {}
        if files:
            self.upload_calls += 1
            name, fh, mime = files["file"]
            data = fh.read()
            self.uploads.append({
                "url": url, "method": method, "filename": name, "mime": mime,
                "bytes": len(data),
                "token": (kw.get("headers") or {}).get("X-Atlassian-Token"),
                "timeout": kw.get("timeout"),
            })
            if self.upload_calls <= self.fail_upload_times:
                raise RuntimeError("模拟上传失败")
        return FakeResponse(payload=[{"id": 1}])


# ============================ 基础工具函数 ============================
class TestHelpers(unittest.TestCase):
    def test_env_float(self):
        os.environ["_TEST_FLOAT"] = "12.5"
        self.assertEqual(server._env_float("_TEST_FLOAT", 1.0), 12.5)
        os.environ["_TEST_FLOAT"] = "abc"
        self.assertEqual(server._env_float("_TEST_FLOAT", 3.0), 3.0)
        self.assertEqual(server._env_float("_TEST_NOT_EXIST", 7.0), 7.0)

    def test_name(self):
        self.assertEqual(server._name({"name": "张三"}), "张三")
        self.assertEqual(server._name({"displayName": "李四"}), "李四")
        self.assertEqual(server._name({"key": "T9MLBUG"}), "T9MLBUG")
        self.assertEqual(server._name("plain"), "plain")
        self.assertEqual(server._name(None), "")

    def test_summarize_issue_keeps_useful_fields(self):
        issue = {"key": "A-1", "fields": {
            "summary": "标题", "status": {"name": "问题确认"},
            "priority": {"name": "High"}, "issuetype": {"name": "BUG"},
            "assignee": {"name": "me"}, "reporter": {"name": "you"},
            "created": "2026-01-01", "updated": "2026-01-02",
            "expand": "x", "avatarUrls": {},
        }}
        out = server._summarize_issue(issue)
        self.assertEqual(out["key"], "A-1")
        self.assertEqual(out["status"], "问题确认")
        self.assertNotIn("avatarUrls", out)

    def test_summarize_search_and_table(self):
        data = {"total": 2, "startAt": 0, "maxResults": 50,
                "issues": [{"key": "A-1", "fields": {}}, {"key": "A-2", "fields": {}}]}
        self.assertEqual(server._summarize_search(data)["issues"][1]["key"], "A-2")
        tbl = {"issueTable": {"total": 3, "issueKeys": ["A-1"], "table": "<html>巨量</html>"}}
        out = server._summarize_issue_table(tbl)
        self.assertEqual(out["total"], 3)
        self.assertNotIn("table", out)

    def test_attachment_mb(self):
        self.assertEqual(server._attachment_mb([{"size": MB}, {"size": MB / 2}]), 1.5)
        self.assertEqual(server._attachment_mb(None), 0.0)


# ============================ 判定逻辑 ============================
class TestDetection(unittest.TestCase):
    def test_detect_app_system_priority(self):
        self.assertEqual(server._detect_app_system("鸿蒙手机无法连接"), "鸿蒙")
        self.assertEqual(server._detect_app_system("harmonyos 端崩溃"), "鸿蒙")
        self.assertEqual(server._detect_app_system("苹果手机 iPhone 15 闪退"), "IOS")
        self.assertEqual(server._detect_app_system("androidlog 日志"), "Android")
        # 鸿蒙 > IOS > Android:同一段文本同时含多系统时取优先级高的
        self.assertEqual(server._detect_app_system("iPhone 与 鸿蒙 均有问题"), "鸿蒙")
        self.assertEqual(server._detect_app_system(""), "")

    def test_parse_reproducibility(self):
        cases = [
            ("【出现概率】：100%", "Systematic"),
            ("出现概率:80%", "Sporadic:>70%"),
            ("出现概率:50%", "Sporadic:50%"),
            ("出现概率:40%", "Sporadic:30%"),
            ("出现概率:10%", "Sporadic:10%"),
            ("出现概率:5%", "Once"),
        ]
        for text, expect in cases:
            self.assertEqual(server._parse_reproducibility(text), expect, text)
        self.assertEqual(server._parse_reproducibility("【出现概率】：升级两次出现两次"), "")

    def test_guess_module(self):
        self.assertEqual(server._guess_module("开启透气模式提示超时"), "透气模式")
        self.assertEqual(server._guess_module("远控关闭用电保持失败"), "基础远控")
        self.assertEqual(server._guess_module("远程控制打开后备箱"), "基础远控")
        self.assertEqual(server._guess_module("【OTA】无法执行升级"), "OTA")
        self.assertEqual(server._guess_module("充电界面显示异常"), "充电中心")
        self.assertEqual(server._guess_module("蓝牙钥匙连接失败"), "蓝牙车控/激活")
        self.assertEqual(server._guess_module("登录页闪退"), "车控")
        self.assertEqual(server._guess_module("登录页闪退", fallback="备用"), "备用")

    def test_guess_vehicle_model(self):
        allowed = ["T68", "T68-G", "T68-M1", "T75", "S52", "T09-Y1"]
        self.assertEqual(server._guess_vehicle_model("T68项目质量问题", allowed), "T68")
        self.assertEqual(server._guess_vehicle_model("T75", allowed), "T75")
        # 唯一前缀匹配才算数
        self.assertEqual(server._guess_vehicle_model("T09", allowed), "T09-Y1")
        # 歧义/匹配不上返回空,交由调用方报错
        self.assertEqual(server._guess_vehicle_model("T6", allowed), "")
        self.assertEqual(server._guess_vehicle_model("", allowed), "")
        self.assertEqual(server._guess_vehicle_model("T68项目质量问题", []), "")


# ============================ 附件拷贝 ============================
class TestCopyAttachments(unittest.TestCase):
    def setUp(self):
        self._orig_session = server.client.session
        self._orig_get = server.client.get
        self._orig_sleep = server.time.sleep
        server.time.sleep = lambda *a, **k: self._orig_sleep(0.01)  # 真实但极短的退避,保留重试语义
        server.client.get = lambda path, params=None: {"fields": {"attachment": []}}
        # 清理历史残留(别的进程/被中断的用例可能留下),避免影响本次残留断言
        for p in glob.glob(os.path.join(tempfile.gettempdir(), TMP_PREFIX + "*")):
            try:
                os.remove(p)
            except OSError:
                shutil.rmtree(p, ignore_errors=True)

    def tearDown(self):
        server.client.session = self._orig_session
        server.client.get = self._orig_get
        server.time.sleep = self._orig_sleep
        leftovers = glob.glob(os.path.join(tempfile.gettempdir(), TMP_PREFIX + "*"))
        self.assertEqual(leftovers, [], "临时文件残留:" + str(leftovers))

    def _att(self, name="log.txt", size_mb=1, content="https://jira/secure/a", mime="text/plain"):
        return {"filename": name, "size": int(size_mb * MB), "content": content, "mimeType": mime}

    def test_copy_success_and_cleanup(self):
        session = FakeSession(download_bytes=b"y" * 100)
        server.client.session = session
        report = server._copy_attachments("SRC-1", "T9ML-1", {"attachment": [self._att()]})
        self.assertEqual((report["copied"], report["failed"], report["skipped"]), (1, 0, 0))
        self.assertEqual(report["files"][0]["status"], "copied")
        up = session.uploads[0]
        self.assertTrue(up["url"].endswith("/rest/api/2/issue/T9ML-1/attachments"))
        self.assertEqual(up["filename"], "log.txt")
        self.assertEqual(up["bytes"], 100)
        self.assertEqual(up["token"], "no-check")
        self.assertTrue(session.downloads[0]["stream"], "下载必须流式")

    def test_skip_oversize_and_missing_content(self):
        server.client.session = FakeSession()
        atts = [self._att("huge.zip", 200), self._att("broken.bin", 1, content=None)]
        report = server._copy_attachments("SRC-1", "T9ML-1", {"attachment": atts}, max_single_mb=100)
        self.assertEqual((report["copied"], report["skipped"], report["failed"]), (0, 1, 1))
        self.assertIn("skipped(单文件>", report["files"][0]["status"])
        self.assertIn("缺少 content", report["files"][1]["error"])

    def test_retry_then_success(self):
        session = FakeSession(fail_upload_times=1)  # 第 1 次上传失败,第 2 次成功
        server.client.session = session
        report = server._copy_attachments("SRC-1", "T9ML-1", {"attachment": [self._att()]})
        self.assertEqual(report["copied"], 1)
        self.assertEqual(report["files"][0]["attempts"], 2)
        self.assertIsNone(report["files"][0]["error"], "重试成功后不应保留上次错误")

    def test_retry_exhausted_records_error(self):
        session = FakeSession(fail_upload_times=99)
        server.client.session = session
        report = server._copy_attachments("SRC-1", "T9ML-1", {"attachment": [self._att()]})
        self.assertEqual((report["copied"], report["failed"]), (0, 1))
        self.assertEqual(session.upload_calls, server.ATTACH_RETRY)
        self.assertIn("模拟上传失败", report["files"][0]["error"])

    def test_skip_when_target_already_has_same_file(self):
        server.client.session = FakeSession()
        existing = [{"filename": "log.txt", "size": MB}]
        server.client.get = lambda path, params=None: {"fields": {"attachment": existing}}
        report = server._copy_attachments("SRC-1", "T9ML-1",
                                          {"attachment": [self._att("log.txt", 1)]})
        self.assertEqual((report["copied"], report["skipped"]), (0, 1))
        self.assertIn("已存在同名同大小附件", report["files"][0]["status"])

    def test_background_job_completes(self):
        server.client.session = FakeSession()
        job = server._start_attachment_job("SRC-1", "T9ML-2", {"attachment": [self._att()]})
        self.assertEqual(job["status"], "running")
        deadline = 10.0
        while deadline > 0 and server._ATTACH_JOBS[job["job_id"]]["status"] == "running":
            time.sleep(0.05)  # 真实等待后台线程(server.time.sleep 已被打桩)
            deadline -= 0.05
        done = server._ATTACH_JOBS[job["job_id"]]
        self.assertEqual(done["status"], "done", f"任务未结束:{done}")
        self.assertEqual(done["result"]["copied"], 1)
        status = server.jira_attachment_job_status(job["job_id"])
        self.assertTrue(status["ok"])
        self.assertFalse(server.jira_attachment_job_status("not-exist")["ok"])


class TestCopyComments(unittest.TestCase):
    def test_comments_copied_with_prefix(self):
        posted = []

        def fake_post(path, json=None):
            posted.append((path, json["body"]))
            return {"id": "1"}

        orig = server.client.post
        server.client.post = fake_post
        try:
            src = {"comment": {"comments": [{"body": "复现了", "author": {"name": "tester"}},
                                            {"body": "", "author": None}]}}
            n = server._copy_comments("SRC-1", "T9ML-1", src)
        finally:
            server.client.post = orig
        self.assertEqual(n, 2)
        self.assertTrue(posted[0][1].startswith("[克隆自 SRC-1] tester:"))
        self.assertEqual(posted[0][0], "issue/T9ML-1/comment")


# ============================ 克隆与扫描流程 ============================
class TestCloneIssue(unittest.TestCase):
    """jira_clone_issue:必填字段自动补全 + 关联创建 + 分派。"""

    CREATEMETA = {
        "projects": [{
            "issuetypes": [{
                "name": "BUG",
                "fields": {
                    "summary": {"required": True},
                    "description": {"required": False},
                    "labels": {"required": False},
                    server.CF_VEHICLE_MODEL: {"required": True,
                                              "allowedValues": [{"value": "T75"}, {"value": "S52"}]},
                    server.CF_REPRODUCIBILITY: {"required": True, "allowedValues": []},
                    server.CF_APP_SYSTEM: {"required": True, "allowedValues": []},
                },
            }]
        }]
    }

    def setUp(self):
        self._orig_get = server.client.get
        self._orig_post = server.client.post
        self._orig_session = server.client.session
        self._orig_sleep = server.time.sleep
        server.time.sleep = lambda *a, **k: self._orig_sleep(0.01)  # 真实但极短的退避,保留重试语义
        server.client.session = FakeSession()
        self.calls = {"get": [], "post": []}

        def fake_get(path, params=None):
            self.calls["get"].append(path)
            if path == "issue/createmeta":
                return self.CREATEMETA
            if path.startswith("issue/"):
                return {"key": "SRC-1", "fields": {
                    "summary": "远程控制打开后备箱失败",
                    "description": "",
                    "customfield_11602": "【出现概率】：100%\n测试步骤:手机APP点击远程控制",
                    "priority": {"name": "High"},
                    "project": {"name": "T75项目质量问题"},
                    "issuetype": {"name": "BUG"},
                    "labels": ["app"],
                    "components": [],
                    "comment": {"comments": []},
                    "attachment": [{"filename": "a.log", "size": MB, "content": "https://jira/a",
                                    "mimeType": "text/plain"}],
                }}
            return {}

        def fake_post(path, json=None):
            self.calls["post"].append((path, json))
            if path == "issue":
                return {"key": "T9ML-999"}
            return {"id": "1"}

        server.client.get = fake_get
        server.client.post = fake_post

    def tearDown(self):
        server.client.get = self._orig_get
        server.client.post = self._orig_post
        server.client.session = self._orig_session
        server.time.sleep = self._orig_sleep

    def test_clone_autofills_required_fields(self):
        res = server.jira_clone_issue(source_key="SRC-1", app_system="Android",
                                      assignee=server.AUTO_SCAN_DEFAULT_ASSIGNEE)
        self.assertTrue(res["ok"], res.get("error"))
        d = res["data"]
        self.assertEqual(d["clone_key"], "T9ML-999")
        self.assertEqual(d["vehicle_model"], "T75")
        self.assertEqual(d["severity"], "A_Critical")  # High → A_Critical
        self.assertTrue(d["link_created"])

        created = [j for p, j in self.calls["post"] if p == "issue"][0]["fields"]
        self.assertEqual(created["project"]["key"], server.CLONE_TARGET_PROJECT)
        self.assertEqual(created[server.CF_VEHICLE_MODEL]["value"], "T75")
        self.assertEqual(created[server.CF_APP_SYSTEM], [{"value": "Android"}])
        self.assertEqual(created[server.CF_REPRODUCIBILITY]["value"], "Systematic")
        self.assertEqual(created["components"], [{"name": "基础远控"}])
        self.assertEqual(created["assignee"]["name"], "dengjiajun")
        # 描述为空时回退到「问题描述」自定义字段
        self.assertIn("出现概率", created["description"])
        # Cloners 关联:outward=源,inward=克隆
        link = [j for p, j in self.calls["post"] if p == "issueLink"][0]
        self.assertEqual(link["type"]["name"], "Cloners")
        self.assertEqual(link["outwardIssue"]["key"], "SRC-1")
        self.assertEqual(link["inwardIssue"]["key"], "T9ML-999")

    def test_clone_async_attachments_returns_job(self):
        res = server.jira_clone_issue(source_key="SRC-1", app_system="Android",
                                      attachments_async=True)
        self.assertTrue(res["ok"])
        self.assertIsNotNone(res["data"]["attachment_job"])
        self.assertEqual(res["data"]["attachment_job"]["status"], "running")

    def test_clone_fails_when_vehicle_model_unknown(self):
        server.client.get = lambda path, params=None: (
            {"projects": [{"issuetypes": [{"name": "BUG", "fields": {
                server.CF_VEHICLE_MODEL: {"required": True, "allowedValues": []}}}]}]}
            if path == "issue/createmeta" else {"key": "SRC-1", "fields": {
                "summary": "x", "priority": {"name": "High"},
                "project": {"name": "未知项目"}}})
        res = server.jira_clone_issue(source_key="SRC-1")
        self.assertFalse(res["ok"])
        self.assertIn("无法确定【影响车型】", res["error"])


class TestAutoCloneScan(unittest.TestCase):
    """jira_auto_clone_scan:dry_run 计划、App 过滤、附件模式、终态排除。"""

    def _search_payload(self):
        return {"total": 3, "issues": [
            {"key": "A-1", "fields": {
                "summary": "远控失败", "description": "", "customfield_11602": "手机APP点击无响应",
                "priority": {"name": "High"},
                "attachment": [{"filename": "a.log", "size": 5 * MB}]}},
            {"key": "A-2", "fields": {
                "summary": "开启透气模式超时", "description": "手机APP", "customfield_11602": "",
                "priority": {"name": "High"},
                "attachment": [{"filename": "big.log", "size": 40 * MB},
                               {"filename": "huge.dump", "size": 300 * MB}]}},
            {"key": "A-3", "fields": {
                "summary": "车窗异响", "description": "实车测试", "customfield_11602": "",
                "priority": {"name": "High"}, "attachment": []}},
        ]}

    def setUp(self):
        self._orig_get = server.client.get
        self._orig_sleep = server.time.sleep
        server.time.sleep = lambda *a, **k: self._orig_sleep(0.01)  # 真实但极短的退避,保留重试语义
        server.client.get = lambda path, params=None: self._search_payload()

    def tearDown(self):
        server.client.get = self._orig_get
        server.time.sleep = self._orig_sleep

    def test_dry_run_plan(self):
        res = server.jira_auto_clone_scan(max_results=10, dry_run=True)
        self.assertTrue(res["ok"], res.get("error"))
        d = res["data"]
        self.assertEqual(d["scanned"], 3)
        self.assertEqual(len(d["cloned"]), 2)
        self.assertEqual(len(d["skipped"]), 1)
        self.assertIn("App 字眼", d["skipped"][0]["reason"])

        m = {i["key"]: i for i in d["cloned"]}
        self.assertEqual(m["A-1"]["app_system"], "Android")      # fallback
        self.assertEqual(m["A-1"]["assignee"], "dengjiajun")     # 统一分派
        self.assertEqual(m["A-1"]["component"], "基础远控")
        self.assertEqual(m["A-1"]["reproducibility"], server.DEFAULT_REPRODUCIBILITY)
        self.assertEqual(m["A-1"]["action"], "plan")
        # 5MB → 同步;340MB → 异步,且 300MB 单文件列入跳过清单
        self.assertEqual(m["A-1"]["attachment_mode"], "sync")
        self.assertEqual(m["A-2"]["attachment_mode"], "async")
        self.assertEqual(m["A-2"]["attachment_skip_oversize"], [("huge.dump", 300.0)])

    def test_jql_excludes_closed_statuses(self):
        jql = server.AUTO_SCAN_JQL
        self.assertIn("status not in", jql)
        for st in server.CLOSED_STATUSES:
            self.assertIn(st, jql)
        self.assertIn('issueFunction not in hasLinkType("Cloners")', jql)
        self.assertIn("resolution = Unresolved", jql)


class TestCopyAttachmentsTool(unittest.TestCase):
    def setUp(self):
        self._orig_get = server.client.get
        self._orig_session = server.client.session
        self._orig_sleep = server.time.sleep
        server.time.sleep = lambda *a, **k: self._orig_sleep(0.01)  # 真实但极短的退避,保留重试语义
        server.client.session = FakeSession()

    def tearDown(self):
        server.client.get = self._orig_get
        server.client.session = self._orig_session
        server.time.sleep = self._orig_sleep

    def test_no_attachments(self):
        server.client.get = lambda path, params=None: {"fields": {"attachment": []}}
        res = server.jira_copy_attachments("SRC-1", "T9ML-1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["attachment_count"], 0)
        self.assertIn("源单无附件", res["data"]["note"])

    def test_sync_copy(self):
        atts = [{"filename": "a.log", "size": MB, "content": "https://jira/a"}]
        server.client.get = lambda path, params=None: (
            {"fields": {"attachment": atts}} if path.startswith("issue/SRC")
            else {"fields": {"attachment": []}})
        res = server.jira_copy_attachments("SRC-1", "T9ML-1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["copied"], 1)
        self.assertEqual(res["data"]["attachment_mb"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
