# -*- coding: utf-8 -*-
"""会话 Cookie 的签发与校验。

为什么必须有签名：
    Cookie 原来只存 `uid=ou_xxx` 明文，而 `/api/board` 又把全员的 open_id
    发给每个登录成员。两者一凑，任何成员都能把别人的 open_id 填进 Cookie，
    直接以管理员身份访问——服务端只查「这个 open_id 在不在 users 表」，
    完全分不出这是本人还是抄来的。

现在的 Cookie 形态：`uid=<open_id>.<签发时间>.<hmac>`
    hmac 覆盖 open_id + 签发时间，密钥来自 BOARD_SESSION_SECRET
    （没配时退回 QL_INTERNAL_TOKEN，两者都没有就拒绝一切 Cookie）。

为什么还接受一次旧格式：
    换成签名格式那天，已有用户的浏览器里全是旧 Cookie。如果直接全拒，
    他们只能重新走 OAuth——而公网回调地址当时还没在飞书后台登记，
    等于把所有人锁在门外。所以旧格式**只在本次请求**被接受，
    并立即回发一个签名后的新 Cookie，之后旧格式永久失效。
"""
import hashlib
import hmac
import os
import time

from . import config

COOKIE_NAME = "uid"
MAX_AGE_S = 2592000           # 30 天，与 Set-Cookie 的 Max-Age 保持一致
LEGACY_GRACE_S = 7 * 86400    # 旧格式只在这段时间内允许「换发一次」


def _secret():
    s = os.environ.get("BOARD_SESSION_SECRET") or ""
    if s:
        return s
    # 退回内部接口密钥：已经在 board.env 里，运维不需要再记一个新密钥。
    # 代价是两个用途共享密钥，对单机内部系统可以接受。
    s = os.environ.get("QL_INTERNAL_TOKEN") or (config.load().get("internal_token") or "")
    return s


def _macro(open_id, issued_at):
    return f"{open_id}.{issued_at}".encode("utf-8")


def issue(open_id, now=None):
    """签发 Cookie 值。密钥缺失时返回空串（调用方据此不发 Cookie）。"""
    sec = _secret()
    if not sec or not open_id:
        return ""
    ts = int(now or time.time())
    sig = hmac.new(sec.encode("utf-8"), _macro(open_id, ts), hashlib.sha256).hexdigest()
    return f"{open_id}.{ts}.{sig}"


def verify(value, now=None):
    """校验 Cookie。返回 (open_id, 状态)。

    状态：`ok` 正常、`legacy` 旧格式（调用方应换发新 Cookie）、`bad` 拒绝。
    """
    raw = (value or "").strip()
    if not raw:
        return "", "bad"
    now = int(now or time.time())

    parts = raw.rsplit(".", 2)
    if len(parts) == 3 and parts[0].startswith("ou_"):
        open_id, ts_s, sig = parts
        try:
            ts = int(ts_s)
        except ValueError:
            return "", "bad"
        if now - ts > MAX_AGE_S:
            return "", "bad"
        sec = _secret()
        if not sec:
            return "", "bad"
        expect = hmac.new(sec.encode("utf-8"), _macro(open_id, ts),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return "", "bad"
        return open_id, "ok"

    # 旧格式：形如 ou_xxx，没有签名部分
    if raw.startswith("ou_") and "." not in raw:
        # 没有密钥就无法换发签名 Cookie，此时旧格式必须一律拒——
        # 否则「配置丢了密钥」会变成「任何人都能伪造 open_id 进来」。
        if not _secret():
            return "", "bad"
        return raw, "legacy"

    return "", "bad"


def cookie_header(value, secure=False):
    flags = "; Secure" if secure else ""
    return (f"{COOKIE_NAME}={value}; Path=/; Max-Age={MAX_AGE_S}; "
            f"HttpOnly; SameSite=Lax{flags}")
