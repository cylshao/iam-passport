"""authentik API 客户端，各同步来源共用。试运行时读取照常进行，写入只打印出来，不真正发送。"""
import json

from common import retry

PAGE_SIZE = 100
TIMEOUT = 15


class AuthentikClient:
    def __init__(self, url, token, dry_run=False):
        self.base_url = url.rstrip("/") + "/api/v3"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.dry_run = dry_run

    def list(self, path, **filters):
        """逐页读取列表接口，依次给出每一项。attributes 过滤条件按 authentik 的要求以 JSON 传递。"""
        params = {key: json.dumps(value) if key == "attributes" else value for key, value in filters.items()}
        page = 1
        while page:
            body = self._send("GET", path, params={**params, "page": page, "page_size": PAGE_SIZE})
            yield from body["results"]
            page = body["pagination"]["next"]

    def create(self, path, body):
        return self._write("POST", path, body)

    def update(self, path, body):
        return self._write("PATCH", path, body)

    def delete(self, path):
        return self._write("DELETE", path)

    def _write(self, method, path, body=None):
        """发送写入请求，返回响应体（没有内容时是空 dict）。失败时抛出 requests 的异常。"""
        if self.dry_run:
            print("[dry-run]", method, path, body or "")
            # 新建的对象还没有 pk，用一个占位值，后面加组成员时还要用到
            return {"pk": f"dry-run:{(body or {}).get('username') or (body or {}).get('name', '')}"}
        return self._send(method, path, json=body)

    def _send(self, method, path, **kwargs):
        resp = retry.send(method, self.base_url + path, headers=self.headers, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
