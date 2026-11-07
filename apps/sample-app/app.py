"""示例自研系统：登录页是自己的，身份由 authentik 通过 OIDC 提供。

两种登录方式最后都走 OIDC 授权码 + PKCE，服务端换取 ID Token，用 authentik
的公钥验签后按 sub 建号或关联。系统自己不保存任何密码。

- 公司账号（飞书扫码）：浏览器跳到 authentik，带授权码回来。
- 账号密码：服务端用 authentik 的 flow executor API 代员工走一遍 authentik 的
  登录流程（密码、MFA 和各项策略都由 authentik 判断），通过后用这个 authentik
  会话请求授权地址拿到授权码，然后注销这个会话。
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from functools import wraps
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import requests
from flask import Flask, abort, g, redirect, render_template, request, session, url_for

ISSUER = os.environ["OIDC_ISSUER"]
CLIENT_ID = os.environ["OIDC_CLIENT_ID"]
CLIENT_SECRET = os.environ["OIDC_CLIENT_SECRET"]
SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email entitlements")
AUTHENTIK_URL = "{0.scheme}://{0.netloc}".format(urlsplit(ISSUER))
AUTH_FLOW = os.environ.get("AUTHENTIK_AUTH_FLOW", "default-authentication-flow")
LOGOUT_FLOW = os.environ.get("AUTHENTIK_LOGOUT_FLOW", "default-invalidation-flow")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8930").rstrip("/")
ADMIN_ENTITLEMENT = os.environ.get("ADMIN_ENTITLEMENT", "Sample App Admins")
DB_PATH = os.environ.get("DB_PATH", "/data/sample-app.db")

REDIRECT_URI = APP_BASE_URL + "/auth/callback"
LOGIN_METHODS = {"sso": "公司账号（飞书扫码）", "password": "账号密码"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sample-app")

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # 回调是 authentik 发起的顶层跳转，Lax 下会带上 cookie；Strict 会丢掉登录前存的 state
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_BASE_URL.startswith("https://"),
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)


class LoginError(Exception):
    pass


_metadata = None
_jwks = None


def metadata():
    global _metadata, _jwks
    if _metadata is None:
        resp = requests.get(ISSUER + ".well-known/openid-configuration", timeout=10)
        resp.raise_for_status()
        md = resp.json()
        if md["issuer"] != ISSUER:
            raise RuntimeError(f"issuer mismatch: discovery says {md['issuer']}, configured {ISSUER}")
        _metadata = md
        _jwks = jwt.PyJWKClient(md["jwks_uri"], cache_keys=True, lifespan=3600)
    return _metadata


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                sub TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                claims TEXT NOT NULL,
                id_token TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_login_at INTEGER NOT NULL
            )"""
        )


def upsert_user(claims, id_token):
    """按 sub 关联本地账号；用户名、姓名、邮箱每次登录按 authentik 刷新。"""
    now = int(time.time())
    username = claims.get("preferred_username") or claims["sub"]
    row = (
        claims["sub"],
        username,
        claims.get("name") or username,
        claims.get("email"),
        json.dumps(claims, ensure_ascii=False),
        id_token,
        now,
        now,
    )
    conn = db()
    created = conn.execute("SELECT 1 FROM users WHERE sub = ?", (claims["sub"],)).fetchone() is None
    conn.execute(
        """INSERT INTO users (sub, username, name, email, claims, id_token, created_at, last_login_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(sub) DO UPDATE SET username = excluded.username, name = excluded.name,
               email = excluded.email, claims = excluded.claims, id_token = excluded.id_token,
               last_login_at = excluded.last_login_at""",
        row,
    )
    conn.commit()
    log.info("%s user sub=%s username=%s", "created" if created else "logged in", claims["sub"], username)
    return conn.execute("SELECT * FROM users WHERE sub = ?", (claims["sub"],)).fetchone()


def current_user():
    sub = session.get("sub")
    if not sub:
        return None
    user = db().execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
    if user is None or not user["active"]:
        session.clear()
        return None
    return user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        g.user = current_user()
        if g.user is None:
            return redirect(url_for("login_page", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        expected = session.get("csrf")
        if not expected or not secrets.compare_digest(request.form.get("csrf", ""), expected):
            abort(400)


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_authorization(next_url):
    """生成一次授权请求：state、nonce、PKCE，返回待存的状态和授权地址。"""
    verifier = secrets.token_urlsafe(64)
    pending = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": verifier,
        "next": safe_next(next_url),
    }
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": pending["state"],
        "nonce": pending["nonce"],
        "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    return pending, metadata()["authorization_endpoint"] + "?" + urlencode(params)


def exchange_code(code, pending):
    md = metadata()
    resp = requests.post(
        md["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pending["verifier"],
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=10,
    )
    if resp.status_code != 200:
        log.warning("token endpoint returned %s: %s", resp.status_code, resp.text[:500])
        raise LoginError("向 authentik 换取令牌失败，请重新登录")
    id_token = resp.json()["id_token"]
    try:
        key = _jwks.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "nonce"]},
        )
    except jwt.PyJWTError as e:
        raise LoginError(f"ID Token 校验失败：{e}") from e
    if not secrets.compare_digest(claims["nonce"], pending["nonce"]):
        raise LoginError("ID Token 校验失败：nonce 不一致")
    return claims, id_token


def start_session(claims, id_token, method):
    user = upsert_user(claims, id_token)
    session.clear()
    session.permanent = True
    session["sub"] = user["sub"]
    session["method"] = method


def render_login(error=None, status=200, **kwargs):
    kwargs.setdefault("next", safe_next(request.values.get("next")))
    return render_template("login.html", error=error, **kwargs), status


@app.get("/login")
def login_page():
    if current_user():
        return redirect(safe_next(request.args.get("next")))
    session.pop("ak_flow", None)
    return render_login()


# ---- 公司账号（飞书扫码）：浏览器跳到 authentik ----


@app.get("/auth/login")
def auth_login():
    pending, url = new_authorization(request.args.get("next"))
    session["oidc"] = pending
    return redirect(url)


@app.get("/auth/callback")
def auth_callback():
    pending = session.pop("oidc", None)
    try:
        if request.args.get("error"):
            raise LoginError(request.args.get("error_description") or request.args["error"])
        if not pending or not secrets.compare_digest(request.args.get("state", ""), pending["state"]):
            raise LoginError("登录请求已失效，请重新登录")
        claims, id_token = exchange_code(request.args.get("code", ""), pending)
    except LoginError as e:
        log.warning("login failed: %s", e)
        return render_login(str(e), 400, next="/")
    start_session(claims, id_token, "sso")
    return redirect(pending["next"])


# ---- 账号密码：服务端代员工走 authentik 的登录流程 ----


def authentik_client(cookies=None):
    s = requests.Session()
    s.cookies.update(cookies or {})
    # authentik 只在本系统的地址属于 AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS 时才采用 X-Forwarded-For，
    # 否则登录记录和防暴力破解的计数都按本系统服务器的地址算
    s.headers["X-Forwarded-For"] = request.remote_addr or ""
    s.headers["User-Agent"] = request.headers.get("User-Agent", "sample-app")
    return s


def flow_api(slug):
    return f"{AUTHENTIK_URL}/api/v3/flows/executor/{slug}/?query="


def flow_submit(s, component, **fields):
    s.headers["X-CSRFToken"] = s.cookies.get("authentik_csrf", "")
    resp = s.post(flow_api(AUTH_FLOW), json={"component": component, **fields}, timeout=10)
    resp.raise_for_status()
    return resp.json()


FIELD_ERRORS = {
    "uid_field": "账号或密码不正确",
    "password": "账号或密码不正确",
    "code": "动态码不正确，请稍等几秒后输入新的动态码",
}


def first_error(challenge):
    for field, errors in challenge.get("response_errors", {}).items():
        for e in errors:
            return FIELD_ERRORS.get(field) or e.get("string") or e.get("code")
    return None


def advance_flow(s, challenge, username, password, code=None):
    """按 authentik 返回的步骤依次作答，直到流程结束、需要动态码或遇到不支持的步骤。

    返回 "done" 或 "code"；登录被拒绝时抛出 LoginError。
    """
    for _ in range(10):
        component = challenge.get("component")
        error = first_error(challenge)
        if component == "xak-flow-redirect":
            return "done"
        if component == "ak-stage-access-denied":
            raise LoginError(challenge.get("error_message") or "authentik 拒绝了这次登录")
        if component == "ak-stage-authenticator-validate":
            classes = {d.get("device_class") for d in challenge.get("device_challenges", [])}
            if not classes & {"totp", "static"}:
                raise LoginError("你的账号还没有设置可在本页面使用的 MFA（动态码），请用公司账号登录")
            if code is None:
                if error:
                    raise LoginError(error, "code")
                return "code"
            challenge, code = flow_submit(s, component, code=code), None
            continue
        if error:
            raise LoginError(error)
        if component == "ak-stage-identification":
            fields = {"uid_field": username}
            if challenge.get("password_fields"):
                fields["password"] = password
            challenge = flow_submit(s, component, **fields)
        elif component == "ak-stage-password" and password is not None:
            challenge = flow_submit(s, component, password=password)
        else:
            log.warning("unsupported flow step: %s", component)
            raise LoginError("登录还需要完成本页面不支持的步骤（如首次设置 MFA），请用公司账号登录")
    raise LoginError("登录流程步骤过多，请用公司账号登录")


def finish_password_login(s, next_url):
    """用 authentik 会话请求授权地址拿授权码，再注销这个会话。"""
    pending, url = new_authorization(next_url)
    try:
        resp = s.get(url, allow_redirects=False, timeout=10)
        location = resp.headers.get("Location", "")
        if resp.status_code == 302 and location.startswith(REDIRECT_URI + "?"):
            params = parse_qs(urlsplit(location).query)
            if params.get("state", [""])[0] != pending["state"] or "code" not in params:
                raise LoginError("authentik 返回的授权结果无效，请重新登录")
            claims, id_token = exchange_code(params["code"][0], pending)
        elif resp.status_code == 200:
            raise LoginError("你没有权限使用本系统，请联系管理员把你加入对应的组")
        else:
            raise LoginError("本系统的授权流程需要在 authentik 页面上确认，请用公司账号登录")
    finally:
        s.get(flow_api(LOGOUT_FLOW), timeout=10)
    start_session(claims, id_token, "password")
    return redirect(pending["next"])


@app.post("/login/password")
def password_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    next_url = safe_next(request.form.get("next"))
    if not username or not password:
        return render_login("请输入账号和密码", 400, username=username)
    s = authentik_client()
    try:
        resp = s.get(flow_api(AUTH_FLOW), timeout=10)
        resp.raise_for_status()
        challenge = resp.json()
        if advance_flow(s, challenge, username, password) == "code":
            session["ak_flow"] = {"cookies": s.cookies.get_dict(), "username": username, "next": next_url}
            return render_login(mfa_username=username)
        return finish_password_login(s, next_url)
    except LoginError as e:
        log.info("password login failed username=%s: %s", username, e.args[0])
        return render_login(e.args[0], 400, username=username)


@app.post("/login/mfa")
def password_login_mfa():
    flow = session.get("ak_flow")
    if not flow:
        return render_login("登录已失效，请重新输入账号密码", 400)
    s = authentik_client(flow["cookies"])
    code = request.form.get("code", "").strip()
    try:
        challenge = flow_submit(s, "ak-stage-authenticator-validate", code=code)
        if advance_flow(s, challenge, flow["username"], None) == "code":
            raise LoginError("动态码不正确", "code")
        session.pop("ak_flow", None)
        return finish_password_login(s, flow["next"])
    except LoginError as e:
        log.info("password login failed username=%s: %s", flow["username"], e.args[0])
        if e.args[1:] == ("code",):
            session["ak_flow"] = {**flow, "cookies": s.cookies.get_dict()}
            return render_login(e.args[0], 400, mfa_username=flow["username"])
        session.pop("ak_flow", None)
        return render_login(e.args[0], 400, username=flow["username"])


# ---- 退出和首页 ----


@app.post("/logout")
def logout():
    user = current_user()
    session.clear()
    if request.form.get("sso") != "1" or user is None:
        return redirect(url_for("login_page"))
    # authentik 只在带上 id_token_hint 时才跳回 post_logout_redirect_uri
    params = {
        "client_id": CLIENT_ID,
        "id_token_hint": user["id_token"],
        "post_logout_redirect_uri": APP_BASE_URL + "/login",
    }
    return redirect(metadata()["end_session_endpoint"] + "?" + urlencode(params))


@app.get("/")
@login_required
def home():
    claims = json.loads(g.user["claims"])
    entitlements = claims.get("entitlements") or []
    return render_template(
        "home.html",
        user=g.user,
        method=LOGIN_METHODS.get(session.get("method"), ""),
        claims=json.dumps(claims, ensure_ascii=False, indent=2),
        groups=claims.get("groups") or [],
        entitlements=entitlements,
        is_admin=ADMIN_ENTITLEMENT in entitlements,
        admin_entitlement=ADMIN_ENTITLEMENT,
    )


@app.template_filter("datetime")
def format_datetime(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@app.get("/healthz")
def healthz():
    return "ok"


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8930")))
