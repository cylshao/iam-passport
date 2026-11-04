#!/usr/bin/env python3
"""飞书通讯录 -> authentik：建号、更新资料、按部门建组、离职或冻结即禁用。

环境变量：FEISHU_APP_ID、FEISHU_APP_SECRET、AK_URL、AK_TOKEN。
建议每 10 分钟运行一次；第一次先加 --dry-run，只打印将要做的变更。
任何一项写入失败时，其余变更照常处理，最后以非 0 退出码结束，便于告警。
"""
import json
import os
import sys
import time

import requests

FEISHU = "https://open.feishu.cn/open-apis"
AK = os.environ["AK_URL"].rstrip("/") + "/api/v3"
AK_HEADERS = {"Authorization": f"Bearer {os.environ['AK_TOKEN']}"}
DRY_RUN = "--dry-run" in sys.argv
MANAGED = {"feishu_managed": True}
# 一次要禁用的账号超过这个比例就中止，防止飞书接口或权限范围异常时把全员禁用
MAX_DISABLE_RATIO = 0.1
RETRIES = 3
RETRY_BACKOFF = 1.0
RETRY_STATUS = {429, 500, 502, 503, 504}

failures = []


def fail(message):
    print("失败", message, file=sys.stderr)
    failures.append(message)


def http(method, url, **kwargs):
    """网络错误、限流和 5xx 时重试。POST 不是幂等的，只在限流（请求未被处理）时重试。"""
    for attempt in range(RETRIES):
        last = attempt == RETRIES - 1
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException:
            if last:
                raise
        else:
            retryable = resp.status_code == 429 or (method != "POST" and resp.status_code in RETRY_STATUS)
            if not retryable or last:
                return resp
        time.sleep(RETRY_BACKOFF * 2 ** attempt)


def feishu_body(resp, what):
    """飞书返回非 0 code 或非 JSON（比如网关错误页）时，在写入任何数据前退出。"""
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if body.get("code") != 0:
        sys.exit(f"{what}（HTTP {resp.status_code}）：{body or resp.text[:200]}")
    return body


def feishu_token():
    resp = http(
        "POST",
        f"{FEISHU}/auth/v3/tenant_access_token/internal",
        json={"app_id": os.environ["FEISHU_APP_ID"], "app_secret": os.environ["FEISHU_APP_SECRET"]},
        timeout=10,
    )
    return feishu_body(resp, "获取飞书 token 失败")["tenant_access_token"]


def feishu_list(token, path, params):
    params = {**params, "page_size": 50}
    while True:
        resp = http(
            "GET", f"{FEISHU}{path}", params=params, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        data = feishu_body(resp, f"飞书接口 {path} 返回错误").get("data") or {}
        yield from data.get("items") or []
        if not data.get("has_more"):
            return
        params["page_token"] = data["page_token"]


def ak(method, path, **kwargs):
    if DRY_RUN and method != "GET":
        print("[dry-run]", method, path, kwargs.get("json", ""))
        return {"pk": f"new:{kwargs.get('json', {}).get('username', '')}"}
    resp = http(method, f"{AK}{path}", headers=AK_HEADERS, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def ak_list(path, params=None):
    page = 1
    while page:
        body = ak("GET", path, params={**(params or {}), "page": page, "page_size": 100})
        yield from body["results"]
        page = body["pagination"]["next"]


def username_of(user):
    """必须和 authentik 里飞书 source 属性映射的规则保持一致。"""
    email = (user.get("email") or "").lower()
    return email.split("@")[0] if email else user["user_id"]


def explain_conflict(username, fid):
    """用户名已被占用时，说明被谁占用、管理员该怎么处理。"""
    holder = next(ak_list("/core/users/", {"username": username}), None)
    if holder is None:
        return f"用户名 {username} 无法使用"
    old_fid = holder["attributes"].get("feishu_user_id")
    if not old_fid:
        return f"用户名 {username} 已被手工创建的账号占用，需要人工处理"
    state = "启用" if holder["is_active"] else "禁用"
    return (
        f"用户名 {username} 已被飞书 user_id 为 {old_fid} 的账号（{state}）占用。"
        f"如果是同一人重新入职，把该账号 attributes 里的 feishu_user_id 改为 {fid}，下次同步会自动恢复；"
        f"如果是不同的人（邮箱被复用），不要合并账号，需要人工处理"
    )


def main():
    token = feishu_token()
    depts = {
        d["open_department_id"]: d
        for d in feishu_list(
            token,
            "/contact/v3/departments/0/children",
            {"fetch_child": "true", "department_id_type": "open_department_id"},
        )
    }

    def lineage(dept_id):
        while dept_id in depts:
            yield dept_id
            dept_id = depts[dept_id].get("parent_department_id")

    def group_name(dept_id):
        return "飞书/" + "/".join(depts[d]["name"] for d in reversed(list(lineage(dept_id))))

    users = {}
    for dept_id in ["0", *depts]:
        for user in feishu_list(
            token,
            "/contact/v3/users/find_by_department",
            {"department_id": dept_id, "department_id_type": "open_department_id", "user_id_type": "user_id"},
        ):
            status = user.get("status") or {}
            if not (status.get("is_frozen") or status.get("is_resigned")):
                users[user["user_id"]] = user

    managed = {
        u["attributes"]["feishu_user_id"]: u
        for u in ak_list("/core/users/", {"attributes": json.dumps(MANAGED)})
    }
    to_disable = [u for fid, u in managed.items() if fid not in users and u["is_active"]]
    if len(to_disable) > max(5, len(managed) * MAX_DISABLE_RATIO):
        sys.exit(f"本次要禁用 {len(to_disable)} 个账号，超过阈值，已中止，请人工确认")

    pks = {}
    for fid, user in users.items():
        fields = {"name": user.get("name") or fid, "email": (user.get("email") or "").lower()}
        old = managed.get(fid)
        if old is None:
            username = username_of(user)
            try:
                created = ak("POST", "/core/users/", json={
                    **fields,
                    "username": username,
                    "type": "internal",
                    "attributes": {**MANAGED, "feishu_user_id": fid},
                })
            except requests.HTTPError as exc:
                conflict = exc.response.status_code == 400 and "username" in exc.response.text
                fail(explain_conflict(username, fid) if conflict else f"新建 {username}：{exc.response.text}")
                continue
            except requests.RequestException as exc:
                fail(f"新建 {username}：{exc}")
                continue
            pks[fid] = created["pk"]
            print("新建", username)
            continue
        pks[fid] = old["pk"]
        # 只恢复被本脚本禁用过的人（重新入职），管理员手动禁用的账号不动
        if not old["is_active"] and old["attributes"].get("feishu_deactivated"):
            fields["is_active"] = True
            fields["attributes"] = {**old["attributes"], "feishu_deactivated": False}
        changes = {k: v for k, v in fields.items() if old.get(k) != v}
        if changes:
            try:
                ak("PATCH", f"/core/users/{old['pk']}/", json=changes)
            except requests.RequestException as exc:
                fail(f"更新 {old['username']}：{exc}")
                continue
            print("更新", old["username"], list(changes))

    for old in to_disable:
        try:
            ak("PATCH", f"/core/users/{old['pk']}/", json={
                "is_active": False,
                "attributes": {**old["attributes"], "feishu_deactivated": True},
            })
            for session in ak_list("/core/authenticated_sessions/", {"user__username": old["username"]}):
                ak("DELETE", f"/core/authenticated_sessions/{session['uuid']}/")
        except requests.RequestException as exc:
            fail(f"禁用 {old['username']}：{exc}")
            continue
        print("禁用", old["username"])

    # 上级部门的组也包含下级部门的人，绑定“飞书/技术中心”即可覆盖整个技术中心
    members = {d: set() for d in depts}
    for fid, user in users.items():
        if fid in pks:
            for dept_id in user.get("department_ids") or []:
                for d in lineage(dept_id):
                    members[d].add(pks[fid])

    groups = {
        g["attributes"].get("feishu_department_id"): g
        for g in ak_list("/core/groups/", {"attributes": json.dumps(MANAGED)})
    }
    for dept_id, group in groups.items():
        if dept_id not in depts and group["users"]:
            try:
                ak("PATCH", f"/core/groups/{group['pk']}/", json={"users": []})
            except requests.RequestException as exc:
                fail(f"清空组 {group['name']}：{exc}")
    for dept_id, pk_set in members.items():
        name, group = group_name(dept_id), groups.get(dept_id)
        try:
            if group is None:
                ak("POST", "/core/groups/", json={
                    "name": name,
                    "users": list(pk_set),
                    "attributes": {**MANAGED, "feishu_department_id": dept_id},
                })
                print("新建组", name)
            elif group["name"] != name or set(group["users"]) != pk_set:
                ak("PATCH", f"/core/groups/{group['pk']}/", json={"name": name, "users": list(pk_set)})
                print("更新组", name)
        except requests.RequestException as exc:
            fail(f"同步组 {name}：{exc}")

    if failures:
        sys.exit(f"本次同步有 {len(failures)} 项失败，其余变更已完成：\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
