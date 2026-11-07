#!/usr/bin/env python3
"""把 authentik 里的头像（同步脚本写入的飞书头像）上传到 GitLab。

GitLab 通过 OIDC 登录时只同步姓名、邮箱等字段，不同步头像，只能由管理员通过 API 上传。

用法：在 org-sync 目录下运行 python3 -m gitlab.avatars [--dry-run]
环境变量：AK_URL、AK_TOKEN（authentik，只读）；GITLAB_URL、GITLAB_TOKEN（GitLab 管理员的个人访问令牌，scope 为 api）。

GitLab 账号按 OIDC 的 sub 对应 authentik 账号，sub 就是 authentik 用户的 uid
（要求 GitLab 的 provider 用默认的 Subject mode：Based on the User's hashed ID）。
上传过的头像地址记在 GitLab 用户的自定义属性 feishu_avatar 里，地址没变就不再上传，可以定期运行。
"""
import argparse
import os
import sys
from collections import Counter

import requests

from common import retry
from common.authentik import AuthentikClient

PROVIDER = "openid_connect"
ATTRIBUTE = "feishu_avatar"
# GitLab 的头像不能超过 200 KB
MAX_AVATAR_BYTES = 200 * 1024
TIMEOUT = 15
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


class GitLabClient:
    """GitLab 管理员 API。试运行时读取照常进行，写入只打印出来。"""

    def __init__(self, url, token, dry_run=False):
        self.base_url = url.rstrip("/") + "/api/v4"
        self.headers = {"PRIVATE-TOKEN": token}
        self.dry_run = dry_run

    def users(self):
        """逐页读取全部用户，带上关联的登录身份和自定义属性。"""
        page = "1"
        while page:
            resp = retry.send("GET", self.base_url + "/users", headers=self.headers, timeout=TIMEOUT, params={
                "page": page, "per_page": 100, "with_custom_attributes": "true",
            })
            resp.raise_for_status()
            yield from resp.json()
            page = resp.headers.get("X-Next-Page")

    def set_avatar(self, user_id, avatar):
        """avatar 是 download() 的返回值：(文件名, 内容, Content-Type)。"""
        self._write(f"/users/{user_id}", files={"avatar": avatar})

    def set_attribute(self, user_id, key, value):
        self._write(f"/users/{user_id}/custom_attributes/{key}", data={"value": value})

    def _write(self, path, **kwargs):
        if self.dry_run:
            print("[dry-run] PUT", path)
            return
        resp = retry.send("PUT", self.base_url + path, headers=self.headers, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()


def download(url):
    """下载头像，返回 (文件名, 内容, Content-Type)。不是图片或超过大小限制时抛出 ValueError。"""
    resp = retry.send("GET", url, timeout=TIMEOUT)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type not in EXTENSIONS:
        raise ValueError(f"不是支持的图片格式：{content_type or '未知'}")
    if len(resp.content) > MAX_AVATAR_BYTES:
        raise ValueError(f"图片 {len(resp.content) // 1024} KB，超过 GitLab 的 200 KB 限制")
    return f"avatar.{EXTENSIONS[content_type]}", resp.content, content_type


def sync(authentik, gitlab):
    """上传有变化的头像，返回 (变更计数, 失败项的说明)。"""
    avatars = {
        user["uid"]: user["attributes"]["avatar"]
        for user in authentik.list("/core/users/", is_active="true")
        if user["attributes"].get("avatar")
    }
    counts, failures = Counter(), []
    for user in gitlab.users():
        uid = next((i["extern_uid"] for i in user.get("identities") or [] if i["provider"] == PROVIDER), None)
        if uid is None or user["state"] != "active":
            continue
        counts["扫码账号"] += 1
        url = avatars.get(uid)
        if url is None:
            continue
        counts["匹配"] += 1
        attributes = {a["key"]: a["value"] for a in user.get("custom_attributes") or []}
        if attributes.get(ATTRIBUTE) == url:
            continue
        try:
            # 试运行不下载，只打印将要发出的写入
            gitlab.set_avatar(user["id"], None if gitlab.dry_run else download(url))
            gitlab.set_attribute(user["id"], ATTRIBUTE, url)
        except (requests.RequestException, ValueError) as exc:
            detail = exc.response.text if isinstance(exc, requests.HTTPError) else exc
            failures.append(f"{user['username']}：{detail}")
            print("失败", user["username"], detail, file=sys.stderr)
            continue
        counts["上传"] += 1
        print("上传", user["username"])
    return counts, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="把 authentik 里的飞书头像上传到 GitLab")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要上传的账号，不写入")
    args = parser.parse_args(argv)

    authentik = AuthentikClient(os.environ["AK_URL"], os.environ["AK_TOKEN"])
    gitlab = GitLabClient(os.environ["GITLAB_URL"], os.environ["GITLAB_TOKEN"], dry_run=args.dry_run)
    try:
        counts, failures = sync(authentik, gitlab)
    except requests.RequestException as exc:
        sys.exit(f"读取 authentik 或 GitLab 失败：{exc}")

    print(
        "试运行完成：" if args.dry_run else "完成：",
        f"扫码账号 {counts['扫码账号']}，匹配到 authentik {counts['匹配']}，上传 {counts['上传']}，失败 {len(failures)}",
        sep="",
    )
    if counts["扫码账号"] and not counts["匹配"]:
        print("一个都没有匹配上：检查 GitLab 的 provider 在 authentik 里的 Subject mode 是不是默认的 hashed ID", file=sys.stderr)
    if failures:
        sys.exit(f"有 {len(failures)} 个头像上传失败：\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
