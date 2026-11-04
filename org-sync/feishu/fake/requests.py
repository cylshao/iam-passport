import json as _json


class RequestException(Exception):
    pass


class ConnectionError(RequestException):
    pass


class HTTPError(RequestException):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else _json.dumps(body, ensure_ascii=False).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPError(self)


HANDLER = None


def request(method, url, params=None, json=None, data=None, headers=None, timeout=None):
    return HANDLER(method, url, dict(params or {}), json if json is not None else data, headers or {})


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, **kw):
    return request("POST", url, **kw)
