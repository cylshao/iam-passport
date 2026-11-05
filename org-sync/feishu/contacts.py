"""飞书通讯录的只读客户端。

用应用身份（tenant_access_token）调用通讯录 v3 接口，员工统一用 union_id 标识：
union_id 在同一企业的各个自建应用里都相同，以后换飞书应用也不影响账号绑定。
"""
from collections import deque
from dataclasses import dataclass

import requests

from common import retry

BASE_URL = "https://open.feishu.cn/open-apis"
ROOT_DEPARTMENT = "0"
PAGE_SIZE = 50
TIMEOUT = 10
# 飞书的临时错误码，HTTP 状态码可能是 200，要看响应体：40003 内部错误，99991400 请求频率超限
TRANSIENT_CODES = {40003, 99991400}


class FeishuError(Exception):
    """飞书接口出错。读取通讯录在写入任何数据之前，所以遇到它可以直接停止同步。"""


@dataclass
class Directory:
    """读取到的通讯录。

    departments：部门 ID -> {"name": 部门名, "parent": 上级部门 ID}，不含根部门。
    users：union_id -> 飞书返回的员工信息，已去掉冻结和离职的人。
    memberships：union_id -> 员工直接所在的部门 ID，可能包含根部门。
    """

    departments: dict
    users: dict
    memberships: dict

    def lineage(self, dept_id):
        """从这个部门往上，依次给出它和它的各级上级部门，不含根部门。"""
        while dept_id in self.departments:
            yield dept_id
            dept_id = self.departments[dept_id]["parent"]

    def path(self, dept_id):
        """部门的完整路径，如“技术中心/运维组”。"""
        return "/".join(self.departments[d]["name"] for d in reversed(list(self.lineage(dept_id))))


class FeishuClient:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None

    def read_directory(self):
        departments = self._departments()
        users, memberships = {}, {}
        for dept_id in [ROOT_DEPARTMENT, *departments]:
            for user in self._list("/contact/v3/users/find_by_department", {
                "department_id": dept_id,
                "department_id_type": "open_department_id",
                "user_id_type": "union_id",
            }):
                # 开通了 contact:user.employee:readonly 才返回 status；离职的人本来就不在部门里
                status = user.get("status") or {}
                if status.get("is_frozen") or status.get("is_resigned"):
                    continue
                users[user["union_id"]] = user
                memberships.setdefault(user["union_id"], set()).add(dept_id)
        return Directory(departments, users, memberships)

    def _departments(self):
        """从根部门开始逐级查询下级部门。

        部门接口只有开通 contact:department.organize:readonly 才返回上级部门，
        逐级查询时上级就是正在查询的那个部门，不需要这项权限。
        """
        departments, queue = {}, deque([ROOT_DEPARTMENT])
        while queue:
            parent = queue.popleft()
            for dept in self._list(
                f"/contact/v3/departments/{parent}/children", {"department_id_type": "open_department_id"}
            ):
                dept_id = dept["open_department_id"]
                departments[dept_id] = {"name": dept["name"], "parent": parent}
                queue.append(dept_id)
        return departments

    def _list(self, path, params):
        """逐页读取列表接口，依次给出每一项。"""
        params = {**params, "page_size": PAGE_SIZE}
        while True:
            data = self._call(
                "GET", path, f"飞书接口 {path} 返回错误",
                params=params, headers={"Authorization": f"Bearer {self._tenant_token()}"},
            ).get("data") or {}
            yield from data.get("items") or []
            if not data.get("has_more"):
                return
            params = {**params, "page_token": data["page_token"]}

    def _tenant_token(self):
        if self._token is None:
            body = self._call(
                "POST", "/auth/v3/tenant_access_token/internal", "获取飞书 token 失败",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            self._token = body["tenant_access_token"]
        return self._token

    @staticmethod
    def _call(method, path, error, **kwargs):
        """调用接口并返回响应体。只读接口和获取 token 都可以放心重试。

        飞书的错误放在响应体的 code 里，HTTP 状态码可能仍是 200；网关错误页这类非 JSON 的响应也算出错。
        """
        try:
            resp = retry.send(
                method, BASE_URL + path, idempotent=True, transient=lambda r: _body(r).get("code") in TRANSIENT_CODES,
                timeout=TIMEOUT, **kwargs,
            )
        except requests.RequestException as exc:
            raise FeishuError(f"{error}：{exc}") from exc
        body = _body(resp)
        if body.get("code") != 0:
            raise FeishuError(f"{error}（HTTP {resp.status_code}）：{body or resp.text[:200]}")
        return body


def _body(resp):
    """响应体解析成的 dict；不是 JSON 时返回空 dict。"""
    try:
        return resp.json() or {}
    except ValueError:
        return {}
