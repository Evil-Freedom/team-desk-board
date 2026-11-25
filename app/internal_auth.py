# -*- coding: utf-8 -*-
"""服务间认证：看板 ↔ QuickLaunch 内部接口。

为什么不用「共享口令放在 header 里」：
    口令一旦被日志、抓包或误提交带出去就是永久有效；而看板与 ql-mailer 都在
    同一台机器的回环上，成本很低的 HMAC 就能把「重放」和「伪造」同时挡掉。

算法（与 ql-mailer 侧 board_bridge.py 必须逐字节一致）：
    message   = f"{timestamp}\n{nonce}\n{method}\n{path}\n{sha256(body)}"
    signature = hex(hmac_sha256(secret, message))

约束：
    · 时间戳与服务器时间相差超过 300 秒直接拒绝；
    · nonce 在窗口内只能用一次（进程内缓存，重启后窗口内旧签名仍会过期）；
    · 密钥只从环境变量或 config 读，绝不写进仓库。
"""
import hashlib
import hmac
import os
import threading
import time

from . import config

WINDOW_S = 300
_nonces = {}
_lock = threading.Lock()


def secret():
    return os.environ.get("QL_INTERNAL_TOKEN") or (config.load().get("internal_token") or "")


def sign(secret_value, timestamp, nonce, method, path, body=b""):
    """生成签名。body 传 bytes；GET 用空 bytes。"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    msg = f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{digest}".encode("utf-8")
    return hmac.new(secret_value.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _seen(nonce, now):
    """窗口内 nonce 去重。顺手清掉过期项，避免字典无限涨。"""
    with _lock:
        for k, ts in list(_nonces.items()):
            if now - ts > WINDOW_S * 2:
                _nonces.pop(k, None)
        if nonce in _nonces:
            return True
        _nonces[nonce] = now
        return False


def verify(headers, method, path, body=b"", now=None):
    """返回 (ok, reason)。拒绝时 reason 只用于日志，不回给调用方。"""
    sec = secret()
    if not sec:
        return False, "服务端未配置 internal_token"

    ts = str(headers.get("X-QL-Timestamp") or "")
    nonce = str(headers.get("X-QL-Nonce") or "")
    sig = str(headers.get("X-QL-Signature") or "")
    if not (ts and nonce and sig):
        return False, "缺少签名头"

    try:
        ts_i = int(ts)
    except ValueError:
        return False, "时间戳非法"

    now = now or time.time()
    if abs(now - ts_i) > WINDOW_S:
        return False, "时间戳超出窗口"
    if len(nonce) < 8 or len(nonce) > 64:
        return False, "nonce 非法"

    expect = sign(sec, ts, nonce, method, path, body)
    if not hmac.compare_digest(expect, sig):
        return False, "签名不匹配"
    if _seen(nonce, now):
        return False, "nonce 重放"
    return True, ""
