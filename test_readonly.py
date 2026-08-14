"""只读工具全覆盖测试:get_issue_types / get_issue / list_transitions(无写副作用)。"""
import json
import os

import server


def trim(o, n=400):
    return json.dumps(o, ensure_ascii=False)[:n]


print("=== jira_get_issue_types('A02BUG') ===")
print(trim(server.jira_get_issue_types("A02BUG")))

print("\n=== jira_get_issue('A21G3BUG-93') (精简 key/type/status/summary) ===")
r = server.jira_get_issue("A21G3BUG-93")
if r["ok"]:
    f = r["data"]["fields"]
    print(json.dumps({
        "key": r["data"]["key"],
        "summary": f["summary"],
        "issuetype": f["issuetype"]["name"],
        "status": f["status"]["name"],
        "priority": f["priority"]["name"],
    }, ensure_ascii=False))
else:
    print(r)

print("\n=== jira_list_transitions('A21G3BUG-93') ===")
print(trim(server.jira_list_transitions("A21G3BUG-93")))

print("\n=== 错误处理: jira_get_issue('NO_SUCH-999999') ===")
print(trim(server.jira_get_issue("NO_SUCH-999999")))
