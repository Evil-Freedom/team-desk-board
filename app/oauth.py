# -*- coding: utf-8 -*-
"""飞书用户授权（OAuth 2.0 授权码模式）。

为什么走用户授权而不是应用身份：
    应用身份要 approval:approval:readonly，这个 scope 在你们租户需要管理员审核；
    而用户身份的 approval:approval:read 已经在授权范围内。
    每个人授权一次，就能读他自己的审批，不需要任何管理员操作。

流程：
    /login          → 302 到飞书授权页
    用户点「授权」   → 飞书回调 /oauth/callback?code=...
    /oauth/callback → code 换 access_token + refresh_token → 存 users 表
                    → 立刻用这个 token 拉一次该用户的审批

约束：
    redirect_uri 必须在飞书应用后台「安全设置 - 重定向 URL」里登记过，
    且飞书服务器要能访问到。内网部署需要公网入口或内网穿透。
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

BASE = "https://open.feishu.cn"
AUTHORIZE_URL = f"{BASE}/open-apis/authen/v1/authorize"
TOKEN_URL = f"{BASE}/open-apis/authen/v2/oauth/token"
USERINFO_URL = f"{BASE}/open-apis/authen/v1/user_info"


def authorize_url(app_id, redirect_uri, state="", scope=""):
    """拼授权地址。

    scope 留空 = 走应用默认配置（当前行为）。
    需要 refresh_token 时必须显式带 offline_access：
    不带这个 scope，飞书不会签发 refresh_token，access_token 两小时一过就要重授权。
    """
    params = {
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scope.strip():
        params["scope"] = scope.strip()
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def _post_json(url, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8")), None
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, repr(e)[:200]


def exchange_code(app_id, app_secret, code, redirect_uri):
    """授权码换 token。返回 (token_dict, error)。"""
    payload, err = _post_json(TOKEN_URL, {
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": app_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    })
    if err:
        return None, err
    if payload.get("code") not in (0, None):
        return None, payload.get("error_description") or payload.get("msg") or "换取 token 失败"

    data = payload.get("data") or payload
    if not data.get("access_token"):
        return None, payload.get("error") or "返回里没有 access_token"
    return data, None


def refresh(app_id, app_secret, refresh_token):
    payload, err = _post_json(TOKEN_URL, {
        "grant_type": "refresh_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "refresh_token": refresh_token,
    })
    if err:
        return None, err
    # v2 token 接口把字段放在顶层（和 exchange_code 一样），
    # 只认 payload["data"] 会永远取不到 access_token；
    # 更糟的是飞书的 refresh_token 是一次性的，取不到就等于把它白扔了
    data = payload.get("data") or payload
    if not data.get("access_token"):
        return None, (payload.get("error_description")
                      or payload.get("error")
                      or payload.get("msg")
                      or "刷新失败")
    return data, None


def userinfo(access_token):
    req = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return None, repr(e)[:200]
    if payload.get("code") != 0:
        return None, payload.get("msg") or "读取用户信息失败"
    return payload.get("data") or {}, None


def save_token(open_id, name, token):
    now = int(time.time())
    with db.session() as conn:
        conn.execute("""
            INSERT INTO users (open_id, name, access_token, refresh_token, expires_at, refresh_expires_at, authorized_at, scope)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(open_id) DO UPDATE SET
                name=excluded.name,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                refresh_expires_at=excluded.refresh_expires_at,
                authorized_at=excluded.authorized_at,
                scope=COALESCE(excluded.scope, users.scope)
        """, (open_id, name, token.get("access_token"), token.get("refresh_token"),
              now + int(token.get("expires_in") or 7200),
              now + int(token.get("refresh_token_expires_in") or 2592000),
              now, token.get("scope")))


# refresh_token 是一次性的，并发刷新会白扔一个，必须串行
_REFRESH_LOCK = threading.Lock()


def get_valid_token(open_id, app_id, app_secret):
    """取一个可用的 access_token，过期前 5 分钟自动刷新。返回 (token, error)。"""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE open_id=?", (open_id,)).fetchone()
    if not row:
        return None, "未授权"
    row = dict(row)

    if row["expires_at"] and row["expires_at"] - 300 > time.time():
        return row["access_token"], None

    if not row["refresh_token"]:
        return None, "无 refresh_token，需要重新授权"

    # 飞书的 refresh_token 用一次就作废，两个线程同时刷会有一个拿到空结果，
    # 所以这里必须串行，并在进锁后重新检查一次——可能别人刚刷完。
    with _REFRESH_LOCK:
        with db.session() as conn:
            fresh = conn.execute(
                "SELECT access_token, expires_at, refresh_token FROM users WHERE open_id=?",
                (open_id,)).fetchone()

        if fresh and fresh["expires_at"] and fresh["expires_at"] - 300 > time.time():
            return fresh["access_token"], None

        rt = (fresh["refresh_token"] if fresh else None) or row["refresh_token"]
        token, err = refresh(app_id, app_secret, rt)
        if err:
            return None, f"刷新 token 失败：{err}"

        save_token(open_id, row.get("name") or "", token)
        return token["access_token"], None


def list_users():
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT open_id, name, expires_at, authorized_at, last_sync_at FROM users")]
    now = int(time.time())
    for r in rows:
        r["valid"] = bool(r["expires_at"] and r["expires_at"] > now)
    return rows
