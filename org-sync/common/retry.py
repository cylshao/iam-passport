"""带重试的 HTTP 请求，各同步来源和 authentik 的客户端共用。"""
import time

import requests

ATTEMPTS = 3
# 第一次重试前等待的秒数，之后每次翻倍
BACKOFF = 1.0
RETRY_STATUS = {429, 500, 502, 503, 504}


def send(method, url, *, idempotent=None, transient=None, **kwargs):
    """发送请求，断连、限流和 5xx 时重试；最后一次的响应原样返回，异常原样抛出。

    idempotent：重复发送是否安全，默认只有 POST 不安全。5xx 或读超时时服务端可能已经处理了请求，
    不安全的请求重试会重复创建，所以只在请求确定没送达（连接超时）或被限流时重试。
    transient：判断响应是不是临时错误的函数，用于把错误放在 HTTP 200 响应体里的接口，如飞书。
    """
    if idempotent is None:
        idempotent = method != "POST"
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            if last or not (idempotent or isinstance(exc, requests.ConnectTimeout)):
                raise
        else:
            retryable = resp.status_code == 429 or idempotent and (
                resp.status_code in RETRY_STATUS or (transient is not None and transient(resp))
            )
            if not retryable or last:
                return resp
        time.sleep(BACKOFF * 2 ** attempt)
