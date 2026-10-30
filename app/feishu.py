# -*- coding: utf-8 -*-
"""飞书访问层：整个项目唯一出网的地方。

两条通道，按场景选：

1) lark-cli domain 命令（首选）
   官方 CLI 封装，鉴权、分页、错误码都由它处理。
   适合：通讯录搜索、审批定义查询、实例详情。

2) 直连 OpenAPI（lark-cli 覆盖不到接口时的兜底）
   lark-cli 的 `api` 子命令在当前环境对所有端点返回 404，
   而「按审批定义拉全量实例」只有原生接口提供，
   所以这里用标准库 urllib 直连，只封装 token 换取和 GET，
   不引入任何第三方 HTTP 库，也不重复实现分页以外的能力。
   限流、重试、错误码翻译仍走飞书返回的 code/msg 原样透出。

调用方约定：
    成功 → (dict, None)
    失败 → (None, "人类可读的原因")
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CLI = shutil.which("lark-cli") or "lark-cli"

# 服务以 pythonw 运行，自身没有控制台。lark-cli 是 .CMD，必须经命令解释器启动，
# 而每次启动都会新建一个控制台窗口——表现就是每隔几分钟闪一个黑窗口。
# CREATE_NO_WINDOW 让子进程彻底不建窗口；非 Windows 上取 0，无副作用。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 60
BASE = "https://open.feishu.cn"


def _argv(args):
    """Windows 下 lark-cli 是 .CMD，必须经 cmd /c 才能执行。"""
    if CLI.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", CLI] + args
    return [CLI] + args


def _write_tmp(payload):
    """参数走临时文件，绕开命令行转义问题（中文、引号、空格）。"""
    fd, path = tempfile.mkstemp(suffix=".json", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def call(method, path, params=None, body=None, as_=None):
    args = ["api", method, path]
    tmps = []

    if params:
        f = _write_tmp(params)
        tmps.append(f)
        args += ["--params", "@" + f]
    if body:
        f = _write_tmp(body)
        tmps.append(f)
        args += ["--data", "@" + f]
    if as_:
        args += ["--as", as_]

    try:
        p = subprocess.run(_argv(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT,
                           creationflags=NO_WINDOW)
        raw = (p.stdout or "").strip()
        if not raw:
            return None, (p.stderr or "无输出，可能未登录").strip()[:300]
        try:
            payload = json.loads(raw)
        except Exception:
            return None, raw[:300]

        if not payload.get("ok"):
            err = payload.get("error") or {}
            msg = err.get("message") or err.get("subtype") or "飞书接口返回失败"
            code = err.get("code")
            return None, f"{msg}" + (f" (code={code})" if code else "")
        return payload.get("data") or {}, None
    except subprocess.TimeoutExpired:
        return None, "飞书接口超时"
    except Exception as e:
        return None, repr(e)[:300]
    finally:
        for f in tmps:
            try:
                os.unlink(f)
            except OSError:
                pass


def get(path, params=None, as_=None):
    return call("GET", path, params=params, as_=as_)


def post(path, body=None, params=None, as_=None):
    return call("POST", path, params=params, body=body, as_=as_)


def run(args, as_=None):
    """执行任意 lark-cli 子命令（如 contact +search-user），同样返回 (data, error)。"""
    argv = list(args) + (["--as", as_] if as_ else [])
    try:
        p = subprocess.run(_argv(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT,
                           creationflags=NO_WINDOW)
        raw = (p.stdout or "").strip()
        if not raw:
            return None, (p.stderr or "无输出").strip()[:300]
        payload = json.loads(raw)
        if not payload.get("ok"):
            err = payload.get("error") or {}
            return None, err.get("message") or "命令失败"
        return payload.get("data") or {}, None
    except Exception as e:
        return None, repr(e)[:300]


# ------------------------------------------------------------------ 直连通道
class Tenant:
    """应用身份直连客户端：换一次 token 缓存 2 小时，之后只发 GET。

    只在 lark-cli 覆盖不到的接口上使用，避免把整个项目变成手写 HTTP 客户端。
    """

    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expire_at = 0
        self._lock = threading.Lock()

    def _fetch_token(self):
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE}/open-apis/auth/v3/tenant_access_token/internal",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or "换取 tenant_access_token 失败")
        return payload["tenant_access_token"], int(payload.get("expire") or 7200)

    def token(self):
        with self._lock:
            if self._token and time.time() < self._expire_at - 120:
                return self._token
            tok, ttl = self._fetch_token()
            self._token, self._expire_at = tok, time.time() + ttl
            return tok

    def get(self, path, params=None):
        try:
            tok = self.token()
        except Exception as e:
            return None, f"获取应用凭证失败：{e}"

        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except Exception:
                return None, f"HTTP {e.code}"
        except Exception as e:
            return None, repr(e)[:300]

        if payload.get("code") != 0:
            msg = payload.get("msg") or "接口返回失败"
            code = payload.get("code")
            # 权限类错误单独标注，前端能直接提示去开权限
            if code == 99991672:
                msg = "应用缺少权限：" + msg.split("。")[0]
            return None, f"{msg} (code={code})"
        return payload.get("data") or {}, None


_client = None
_client_key = None


def tenant(app_id, app_secret):
    """按凭证缓存客户端，避免每次调用都重新换 token。"""
    global _client, _client_key
    key = (app_id, app_secret)
    if _client is None or _client_key != key:
        _client, _client_key = Tenant(app_id, app_secret), key
    return _client


def get_with_token(access_token, path, params=None):
    """用某个用户的 access_token 调接口（用户授权后读他自己的数据）。"""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, repr(e)[:300]

    if payload.get("code") != 0:
        return None, f"{payload.get('msg') or '接口失败'} (code={payload.get('code')})"
    return payload.get("data") or {}, None


def identity():
    """当前 CLI 用的是谁的身份，前端用它提示“只能看到自己的数据”。"""
    data, err = get("/open-apis/authen/v1/user_info", as_="user")
    if err or not data:
        return None, err
    return data.get("data") or data, None
