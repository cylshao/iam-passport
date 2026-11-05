#!/usr/bin/env python3
"""飞书通讯录 -> authentik：建号、更新资料、按部门建组、离职或冻结即禁用。

用法：在 org-sync 目录下运行 python3 -m feishu.main [--dry-run] [--max-disable N]
环境变量：FEISHU_APP_ID、FEISHU_APP_SECRET、AK_URL、AK_TOKEN。

建议每 10 分钟运行一次；第一次先加 --dry-run，只打印将要做的变更。
读取飞书出错时，在写入任何数据前退出；某一项写入 authentik 失败时，其余变更照常处理，最后以非 0 退出码结束，便于告警。
需要的飞书权限和同步的字段见 docs/feishu.md 的 2.2 节和 5.5 节。
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from common.authentik import AuthentikClient
from feishu.contacts import FeishuClient, FeishuError

# 同步脚本建的账号和组都带这个标记，脚本只处理带标记的，手工建的账号和组不受影响
MANAGED = {"feishu_managed": True}
# 一次要禁用的账号超过 5 个且超过在职账号的 10% 时中止，防止通讯录范围被误改后把全员禁用。
# 确实是批量离职时，人工核对名单后用 --max-disable 临时放开
DISABLE_GUARD_MIN = 5
DISABLE_GUARD_RATIO = 0.1
GROUP_PREFIX = "飞书/"
CHINA_TZ = timezone(timedelta(hours=8))
# 飞书的性别是数字，0（未知）不写
GENDERS = {1: "male", 2: "female", 3: "other"}


def text(value):
    return (value or "").strip()


def date_of(timestamp):
    """飞书的时间戳（秒）按北京时间换算成日期，如 2023-11-15。"""
    return datetime.fromtimestamp(timestamp, CHINA_TZ).date().isoformat() if timestamp else ""


# 员工资料：飞书字段、authentik attributes 里的键、取值方法。上级要换算成用户名，单独处理
PROFILE_FIELDS = [
    ("employee_no", "employee_no", text),
    ("mobile", "mobile", text),
    ("job_title", "job_title", text),
    ("city", "work_city", text),
    ("work_station", "work_station", text),
    ("gender", "gender", lambda value: GENDERS.get(value, "")),
    ("join_time", "join_date", date_of),
]


class SyncAborted(Exception):
    """同步前的检查没通过，没有写入任何数据。"""


def english_name(user):
    """从飞书员工信息里取英文名，作为用户名。取不到时返回空字符串。

    优先用英文名字段；没填时取“张伟-David”里 - 后面的部分；姓名是纯英文时直接用。
    转小写，空格换成点，例如“John Smith” -> john.smith。
    """
    name = text(user.get("en_name"))
    if not name:
        full = text(user.get("name"))
        name = full if full.isascii() else full.rpartition("-")[2]
    name = re.sub(r"\s+", ".", name.strip().lower())
    return re.sub(r"[^a-z0-9._-]", "", name).strip(".-")


def basic_fields(uid, user):
    """账号的姓名和邮箱。"""
    # authentik 保存时会去掉首尾空格，这里也去掉，否则每次同步都会以为姓名变了
    fields = {"name": text(user.get("name")) or uid}
    # 邮箱权限被关掉时飞书不再返回邮箱，这时不写，保留已有的邮箱
    email = text(user.get("email") or user.get("enterprise_email")).lower()
    if email:
        fields["email"] = email
    return fields


def avatar_of(user):
    """240 像素头像的地址。飞书头像的 CDN 地址不需要登录就能访问。"""
    return (user.get("avatar") or {}).get("avatar_240") or ""


def merge_profile(attributes, user, usernames):
    """返回写入头像和员工资料后的 attributes，不修改传入的 dict。

    飞书没返回的字段保留原值，这样权限被关掉时不会清空全员的数据；
    返回了空值说明飞书里清空了，这里也删掉。
    """
    values = {attr: convert(user[key]) for key, attr, convert in PROFILE_FIELDS if key in user}
    if "leader_user_id" in user:
        # 飞书按请求里的 user_id_type 返回上级的 union_id，这里换成上级在 authentik 里的用户名
        values["leader"] = usernames.get(user["leader_user_id"], "")
    merged = {**attributes, "avatar": avatar_of(user)}
    for attr, value in values.items():
        if value:
            merged[attr] = value
        else:
            merged.pop(attr, None)
    return merged


class Sync:
    def __init__(self, feishu, authentik, max_disable=None):
        self.feishu = feishu
        self.authentik = authentik
        # 本次最多允许禁用的账号数；None 表示按 DISABLE_GUARD_MIN 和 DISABLE_GUARD_RATIO 计算
        self.max_disable = max_disable
        self.failures = []
        self.changes = Counter()

    def run(self):
        """同步一次，返回失败项的说明。"""
        directory = self.feishu.read_directory()
        accounts = {
            account["attributes"]["feishu_union_id"]: account
            for account in self.authentik.list("/core/users/", attributes=MANAGED)
        }
        leavers = [a for uid, a in accounts.items() if uid not in directory.users and a["is_active"]]
        # 按在职账号计算：已禁用的账号会越积越多，算进去的话阈值会逐年变松
        active = sum(account["is_active"] for account in accounts.values())
        limit = self.max_disable if self.max_disable is not None else max(DISABLE_GUARD_MIN, active * DISABLE_GUARD_RATIO)
        if len(leavers) > limit:
            raise SyncAborted(
                f"本次要禁用 {len(leavers)} 个账号，超过阈值，已中止。"
                f"请先用 --dry-run 核对待禁用名单，确认无误后用 --max-disable {len(leavers)} 运行一次"
            )

        usernames = self.allocate_usernames(directory.users, accounts)
        pks = {}
        for uid, user in directory.users.items():
            account = accounts.get(uid)
            pk = self.update_user(account, user, usernames) if account else self.create_user(uid, user, usernames)
            if pk is not None:
                pks[uid] = pk
        for account in leavers:
            self.disable_user(account)
        self.sync_groups(directory, pks)
        return self.failures

    def allocate_usernames(self, users, accounts):
        """返回 union_id -> 用户名。没有英文名的新人不在结果里。

        已有账号沿用原来的用户名：用户名只在建号时分配，之后不再改动。
        新人用英文名，和已有账号（包括手工建的）重名时依次加数字，如 lily、lily2、lily3。
        新人的用户名在建号前一次分配好，这样上级和下属同一次入职时，下属的上级也能写上。
        """
        usernames = {uid: account["username"] for uid, account in accounts.items()}
        taken = {account["username"] for account in self.authentik.list("/core/users/")}
        for uid, user in users.items():
            base = english_name(user)
            if uid in usernames or not base:
                continue
            username, n = base, 1
            while username in taken:
                n += 1
                username = f"{base}{n}"
            taken.add(username)
            usernames[uid] = username
        return usernames

    def create_user(self, uid, user, usernames):
        """建号，返回新账号的 pk；建号失败时返回 None。"""
        username = usernames.get(uid)
        if username is None:
            self.fail(f"{user.get('name')} 没有英文名，无法建号。请在飞书里填写英文名，或把姓名改成“中文名-英文名”")
            return None
        created = self.attempt(f"新建 {username}", self.authentik.create, "/core/users/", {
            **basic_fields(uid, user),
            "username": username,
            "type": "internal",
            "attributes": merge_profile({**MANAGED, "feishu_union_id": uid}, user, usernames),
        })
        if created is None:
            return None
        self.report("新建", username)
        return created["pk"]

    def update_user(self, account, user, usernames):
        """把有变化的资料写回账号，返回账号的 pk。"""
        uid = account["attributes"]["feishu_union_id"]
        wanted = {**basic_fields(uid, user), "attributes": merge_profile(account["attributes"], user, usernames)}
        # 只恢复被脚本禁用的账号（比如解冻后重新出现在通讯录里），管理员手动禁用的不动
        if not account["is_active"] and account["attributes"].get("feishu_deactivated"):
            wanted["is_active"] = True
            wanted["attributes"]["feishu_deactivated"] = False
        changes = {key: value for key, value in wanted.items() if account.get(key) != value}
        if changes and self.attempt(
            f"更新 {account['username']}", self.authentik.update, f"/core/users/{account['pk']}/", changes
        ) is not None:
            self.report("更新", account["username"], list(changes))
        return account["pk"]

    def disable_user(self, account):
        """禁用账号，并踢掉它在 authentik 里的登录会话。"""
        def disable():
            self.authentik.update(f"/core/users/{account['pk']}/", {
                "is_active": False,
                "attributes": {**account["attributes"], "feishu_deactivated": True},
            })
            for session in self.authentik.list("/core/authenticated_sessions/", user__username=account["username"]):
                self.authentik.delete(f"/core/authenticated_sessions/{session['uuid']}/")
            return True

        if self.attempt(f"禁用 {account['username']}", disable):
            self.report("禁用", account["username"])

    def sync_groups(self, directory, pks):
        """每个部门对应一个组，组名是部门路径，如“飞书/技术中心/运维组”。

        上级部门的组也包含下级部门的人，绑定“飞书/技术中心”就能覆盖整个技术中心。
        """
        members = {dept_id: set() for dept_id in directory.departments}
        for uid, pk in pks.items():
            for dept_id in directory.memberships[uid]:
                for ancestor in directory.lineage(dept_id):
                    members[ancestor].add(pk)

        names = {dept_id: GROUP_PREFIX + directory.path(dept_id) for dept_id in directory.departments}
        groups = {
            group["attributes"].get("feishu_department_id"): group
            for group in self.authentik.list("/core/groups/", attributes=MANAGED)
        }
        # 已删除部门的组：组可能还绑定着应用，删掉会连带丢失配置，所以不删
        orphans = {group["name"]: group for dept_id, group in groups.items() if dept_id not in directory.departments}
        # 部门删除后又建了同名部门：沿用原来的组，应用绑定也跟着保留。authentik 的组名不能重复，不沿用就建不了组
        for dept_id, name in names.items():
            if dept_id not in groups and name in orphans:
                groups[dept_id] = orphans.pop(name)
        # 其余的清空成员；别的部门改名改成了它的名字时，给它的名字加后缀，把名字让出来
        wanted = set(names.values())
        for name, group in orphans.items():
            changes = {"users": []} if group["users"] else {}
            if name in wanted:
                changes["name"] = f"{name}（已删除）"
            if changes and self.attempt(
                f"清理组 {name}", self.authentik.update, f"/core/groups/{group['pk']}/", changes
            ) is not None:
                self.report("清理组", name, list(changes))

        for dept_id, name in names.items():
            pk_set, group = members[dept_id], groups.get(dept_id)
            if group is None:
                if self.attempt(f"新建组 {name}", self.authentik.create, "/core/groups/", {
                    "name": name,
                    "users": list(pk_set),
                    "attributes": {**MANAGED, "feishu_department_id": dept_id},
                }) is not None:
                    self.report("新建组", name)
                continue
            changes = {}
            if group["name"] != name:
                changes["name"] = name
            if set(group["users"]) != pk_set:
                changes["users"] = list(pk_set)
            if group["attributes"].get("feishu_department_id") != dept_id:
                changes["attributes"] = {**group["attributes"], "feishu_department_id": dept_id}
            if changes and self.attempt(
                f"更新组 {name}", self.authentik.update, f"/core/groups/{group['pk']}/", changes
            ) is not None:
                self.report("更新组", name, list(changes))

    def report(self, change, *details):
        """打印一项完成的变更，并计入结束时的汇总。"""
        print(change, *details)
        self.changes[change] += 1

    def summary(self):
        """一行汇总，如“新建 2，更新 5，失败 1”。没有变更也打印，从日志里能看出这次运行过。"""
        parts = [f"{change} {n}" for change, n in self.changes.items()]
        if self.failures:
            parts.append(f"失败 {len(self.failures)}")
        return "，".join(parts) or "没有变更"

    def attempt(self, what, action, *args):
        """执行一次写入并返回结果。失败时记下来并返回 None，调用方跳过这一项，继续处理其他的。"""
        try:
            return action(*args)
        except requests.HTTPError as exc:
            self.fail(f"{what}：{exc.response.text}")
        except requests.RequestException as exc:
            self.fail(f"{what}：{exc}")
        return None

    def fail(self, message):
        print("失败", message, file=sys.stderr)
        self.failures.append(message)


def main(argv=None):
    parser = argparse.ArgumentParser(description="把飞书通讯录同步到 authentik")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的变更，不写入")
    parser.add_argument("--max-disable", type=int, metavar="N",
                        help="本次最多禁用 N 个账号，代替默认阈值。只在核对过待禁用名单后手动运行时使用")
    args = parser.parse_args(argv)

    feishu = FeishuClient(os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"])
    authentik = AuthentikClient(os.environ["AK_URL"], os.environ["AK_TOKEN"], dry_run=args.dry_run)
    sync = Sync(feishu, authentik, max_disable=args.max_disable)
    try:
        failures = sync.run()
    except (FeishuError, SyncAborted) as exc:
        sys.exit(str(exc))
    print("试运行完成：" if args.dry_run else "同步完成：", sync.summary(), sep="")
    if failures:
        sys.exit(f"本次同步有 {len(failures)} 项失败，其余变更已完成：\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
