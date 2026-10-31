import copy
import importlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fake"))
sys.path.insert(1, os.path.dirname(__file__))
os.environ.update(FEISHU_APP_ID="a", FEISHU_APP_SECRET="s", AK_URL="https://ak.test/", AK_TOKEN="t")

import requests  # fake

FS = {}
AKS = {}
WRITES = []


def page(items, token, size):
    start = int(token or 0)
    chunk = items[start:start + size]
    more = start + size < len(items)
    data = {"has_more": more, "items": chunk}
    if more:
        data["page_token"] = str(start + size)
    return data


def handler(method, url, params, body, headers):
    R = requests.Response
    if url == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal":
        assert method == "POST" and body == {"app_id": "a", "app_secret": "s"}, body
        if "token" in FS.get("errors", ()):
            return R(400, {"code": 10014, "msg": "app secret invalid"})
        return R(200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200})
    if url.startswith("https://open.feishu.cn/open-apis"):
        path = url[len("https://open.feishu.cn/open-apis"):]
        if FS.get("flaky", {}).get(path, 0) > 0:
            FS["flaky"][path] -= 1
            return R(503, None) if FS.get("flaky_kind") == "503" else (_ for _ in ()).throw(requests.ConnectionError("reset"))
        if path in FS.get("errors", ()):
            return R(400, {"code": 40004, "msg": "no dept authority error"})
        assert headers["Authorization"] == "Bearer t-1"
        if path == "/contact/v3/departments/0/children":
            assert params["fetch_child"] == "true"
            return R(200, {"code": 0, "data": page(FS["depts"], params.get("page_token"), 2)})
        if path == "/contact/v3/users/find_by_department":
            did = params["department_id"]
            direct = [u for u in FS["users"] if did in u["department_ids"]]
            return R(200, {"code": 0, "data": page(direct, params.get("page_token"), 2)})
        raise AssertionError(path)

    assert url.startswith("https://ak.test/api/v3"), url
    path = url[len("https://ak.test/api/v3"):]
    if method != "GET":
        WRITES.append((method, path, copy.deepcopy(body)))

    def listing(objs):
        flt = json.loads(params.get("attributes", "{}"))
        objs = [o for o in objs if all(o["attributes"].get(k) == v for k, v in flt.items())]
        if "username" in params:
            objs = [o for o in objs if o["username"] == params["username"]]
        if "user__username" in params:
            objs = [o for o in objs if o["username"] == params["user__username"]]
        size, p = 3, int(params["page"])
        chunk = objs[(p - 1) * size:p * size]
        nxt = p + 1 if p * size < len(objs) else 0
        return R(200, {"pagination": {"next": nxt}, "results": copy.deepcopy(chunk)})

    users, groups, sessions = AKS["users"], AKS["groups"], AKS["sessions"]
    if path == "/core/users/" and method == "GET":
        return listing(list(users.values()))
    if path == "/core/users/" and method == "POST":
        if any(u["username"] == body["username"] for u in users.values()):
            return R(400, {"username": ["This field must be unique."]})
        pk = max(users, default=0) + 1
        users[pk] = {"pk": pk, "is_active": True, "attributes": {}, **body}
        return R(201, copy.deepcopy(users[pk]))
    if path.startswith("/core/users/") and method == "PATCH":
        pk = int(path.split("/")[3])
        users[pk].update(copy.deepcopy(body))
        return R(200, copy.deepcopy(users[pk]))
    if path == "/core/groups/" and method == "GET":
        return listing(list(groups.values()))
    if path == "/core/groups/" and method == "POST":
        assert all(g["name"] != body["name"] for g in groups.values())
        pk = str(uuid.uuid4())
        groups[pk] = {"pk": pk, **copy.deepcopy(body)}
        return R(201, copy.deepcopy(groups[pk]))
    if path.startswith("/core/groups/") and method == "PATCH":
        pk = path.split("/")[3]
        groups[pk].update(copy.deepcopy(body))
        return R(200, copy.deepcopy(groups[pk]))
    if path == "/core/authenticated_sessions/" and method == "GET":
        return listing([{**s, "attributes": {}} for s in sessions])
    if path.startswith("/core/authenticated_sessions/") and method == "DELETE":
        sid = path.split("/")[3]
        AKS["sessions"] = [s for s in sessions if s["uuid"] != sid]
        return R(204, None)
    raise AssertionError((method, path))


requests.HANDLER = handler


def run(*args):
    WRITES.clear()
    sys.argv = ["sync", *args]
    mod = importlib.import_module("feishu_authentik_sync")
    mod = importlib.reload(mod)
    mod.RETRY_BACKOFF = 0
    try:
        mod.main()
        return None
    except SystemExit as exc:
        return str(exc)


def fu(uid, name, email, depts, frozen=False):
    return {"user_id": uid, "open_id": "ou_" + uid, "name": name, "email": email,
            "department_ids": depts, "status": {"is_frozen": frozen, "is_resigned": False}}


def by_username(name):
    return next(u for u in AKS["users"].values() if u["username"] == name)


def group(name):
    return next(g for g in AKS["groups"].values() if g["name"] == name)


def members(name):
    return sorted(AKS["users"][pk]["username"] for pk in group(name)["users"])


FS.update(
    depts=[
        {"open_department_id": "od-tech", "name": "技术中心", "parent_department_id": "0"},
        {"open_department_id": "od-ops", "name": "运维组", "parent_department_id": "od-tech"},
        {"open_department_id": "od-mkt", "name": "市场部", "parent_department_id": "0"},
    ],
    users=[
        fu("u1", "张三", "ZhangSan@corp.com", ["od-ops"]),
        fu("u2", "李四", "lisi@corp.com", ["od-tech"]),
        fu("u3", "王五", "", ["od-mkt"]),
        fu("u4", "赵六", "zhaoliu@corp.com", ["od-mkt"], frozen=True),
        fu("u5", "老板", "boss@corp.com", ["0"]),
    ],
)
AKS.update(users={1: {"pk": 1, "username": "akadmin", "name": "admin", "email": "",
                      "is_active": True, "attributes": {}}}, groups={}, sessions=[])

# 1. dry-run 不产生任何写入
assert run("--dry-run") is None
assert WRITES == [] and len(AKS["users"]) == 1, WRITES

# 2. 首次同步：建号（冻结的不建）、邮箱前缀做用户名、没邮箱用 user_id、上级部门组包含下级成员
assert run() is None
names = sorted(u["username"] for u in AKS["users"].values())
assert names == ["akadmin", "boss", "lisi", "u3", "zhangsan"], names
assert by_username("zhangsan")["email"] == "zhangsan@corp.com"
assert members("飞书/技术中心") == ["lisi", "zhangsan"]
assert members("飞书/技术中心/运维组") == ["zhangsan"]
assert members("飞书/市场部") == ["u3"]

# 3. 再跑一次是幂等的
assert run() is None
assert WRITES == [], WRITES

# 4. 部门改名、员工改名
FS["depts"][1]["name"] = "SRE组"
FS["users"][1]["name"] = "李四四"
assert run() is None
assert members("飞书/技术中心/SRE组") == ["zhangsan"]
assert by_username("lisi")["name"] == "李四四"

# 5. 离职：禁用、踢会话、移出部门组；管理员手动禁用的账号不受影响
AKS["sessions"] = [{"uuid": "s1", "username": "zhangsan"}, {"uuid": "s2", "username": "lisi"}]
by_username("lisi")["is_active"] = False
left = FS["users"].pop(0)
assert run() is None
zs = by_username("zhangsan")
assert zs["is_active"] is False and zs["attributes"]["feishu_deactivated"] is True
assert [s["uuid"] for s in AKS["sessions"]] == ["s2"]
assert members("飞书/技术中心") == ["lisi"]

# 6. 重新入职：自动恢复；手动禁用的 lisi 仍保持禁用
FS["users"].insert(0, left)
assert run() is None
assert by_username("zhangsan")["is_active"] is True
assert by_username("lisi")["is_active"] is False
assert members("飞书/技术中心/SRE组") == ["zhangsan"]

# 7. 飞书接口报错：直接退出，不做任何写入
FS["errors"] = ["/contact/v3/users/find_by_department"]
msg = run()
assert msg and "返回错误" in msg and WRITES == [], (msg, WRITES)
FS["errors"] = ["token"]
msg = run()
assert msg and "token 失败" in msg and WRITES == [], (msg, WRITES)
FS["errors"] = []

# 8. 用户名和手工账号冲突：记录失败、继续处理其他人，最后以非 0 退出
FS["users"].append(fu("u9", "冒名", "akadmin@corp.com", ["od-mkt"]))
FS["users"].append(fu("u10", "新人", "xinren@corp.com", ["od-mkt"]))
msg = run()
assert msg and "1 项失败" in msg and "手工创建" in msg, msg
assert not any(u["attributes"].get("feishu_user_id") == "u9" for u in AKS["users"].values())
assert by_username("xinren")["is_active"] is True
FS["users"] = FS["users"][:-2]
by_username("xinren")["is_active"] = False
by_username("xinren")["attributes"]["feishu_deactivated"] = True

# 9. 重新入职但飞书分配了新的 user_id：不自动合并，提示管理员；改绑后自动恢复
left = FS["users"].pop(0)
assert run() is None
assert by_username("zhangsan")["is_active"] is False
FS["users"].insert(0, {**left, "user_id": "u1b", "open_id": "ou_u1b"})
msg = run()
assert msg and "u1b" in msg and "重新入职" in msg and "禁用" in msg, msg
by_username("zhangsan")["attributes"]["feishu_user_id"] = "u1b"
assert run() is None
assert by_username("zhangsan")["is_active"] is True
assert members("飞书/技术中心/SRE组") == ["zhangsan"]

# 10. 飞书接口偶发 503 或断连：重试后成功
for kind in ("503", "conn"):
    FS["flaky_kind"] = kind
    FS["flaky"] = {"/contact/v3/users/find_by_department": 2}
    assert run() is None, kind
    assert WRITES == [], (kind, WRITES)
FS["flaky"] = {"/contact/v3/users/find_by_department": 3}
FS["flaky_kind"] = "503"
msg = run()
assert msg and "返回错误" in msg and "HTTP 503" in msg and WRITES == [], (msg, WRITES)
FS["flaky"] = {}

# 11. 大批量消失（比如通讯录权限范围被误改）：触发保护，不做任何写入
for i in range(60):
    FS["users"].append(fu(f"x{i}", f"员工{i}", f"x{i}@corp.com", ["od-mkt"]))
assert run() is None
FS["users"] = FS["users"][:4]
msg = run()
assert msg and "超过阈值" in msg and WRITES == [], (msg, WRITES)

print("all scenarios passed")
