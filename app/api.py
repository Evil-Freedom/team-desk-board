# -*- coding: utf-8 -*-
"""HTTP 层：路由 + 响应组装。业务逻辑一律下沉到 modules，这里只做参数校验。

接口清单
    GET  /api/board      看板数据（前端唯一数据源）
    GET  /api/absences   不在岗记录（排查用）
    GET  /api/config     读取配置（app_secret 打码）
    POST /api/config     写配置（只覆盖传入的键）
    POST /api/sync       手动触发一次全量同步
    GET  /api/health     健康检查：各模块最后一次同步结果
"""
import json
import os
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler

from . import approvals as approvals_mod
from . import config, contacts, db, oauth, status
from . import tasks as tasks_mod
from . import bitable as bitable_mod
from . import im_todo as im_todo_mod
from . import digest as digest_mod
from . import notify

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lan_hint():
    """取本机局域网 IP，用于在通知里附带内网地址。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def board_payload():
    conf = config.load()
    lead_name = conf.get("bootstrap_lead") or ""
    species = conf.get("species") or {}

    with db.session() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM members")]

    counts = tasks_mod.summary_by_person()

    # 负责人排最前，其余按姓名，保证每次刷新顺序稳定
    rows.sort(key=lambda m: (0 if m["name"] == lead_name else 1, m["name"]))

    members = []
    for m in rows:
        members.append({
            "open_id": m["open_id"],
            "name": m["name"],
            "role": "成员",
            "status": m.get("status") or "normal",
            "doing": (counts.get(m["open_id"]) or {}).get("doing", 0),
            "overdue": (counts.get(m["open_id"]) or {}).get("overdue", 0),
            "scarf": m.get("scarf"),
            "loc": m.get("loc"),
            "lead": m["name"] == lead_name,
            "species": species.get(m["name"], "ape"),
            "synced": True,
        })

    c_log = db.last_log("contacts") or {}
    a_log = db.last_log("approvals") or {}

    return {
        "department": conf.get("department"),
        "members": members,
        "updated_at": c_log.get("ts"),
        "sync_ok": bool(c_log.get("ok")),
        "sync_message": c_log.get("message") or "",
        "approvals": {
            "ok": bool(a_log.get("ok")),
            "message": a_log.get("message") or "尚未同步",
            "configured": bool(conf.get("approvals")),
        },
    }


def absences_payload():
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, open_id, kind, start_ts, end_ts, city, reason, source FROM absences "
            "ORDER BY start_ts DESC LIMIT 200")]
    return {"items": rows, "count": len(rows)}


def health_payload():
    return {
        "ok": True,
        "ts": int(time.time()),
        "contacts": db.last_log("contacts"),
        "approvals": db.last_log("approvals"),
        "approvals_configured": bool(config.load().get("approvals")),
    }


def sync_all():
    """一次全量同步：人 → 审批 → 状态。顺序不能反，状态依赖前两步的结果。

    审批走「按人授权」：每个人授权一次，服务用他自己的 token 拉他自己的单据，
    汇总进同一张 absences 表。不需要管理员批 tenant 级权限。
    没有授权的人，退回应用身份尝试（若应用有权限则能覆盖全员）。
    """
    c = contacts.sync_members()

    # 审批同步：先试应用身份（一次拉全员），没权限再退回按人授权。
    # 这样管理员一批准 approval:instance，下一轮自动切过去，不用改代码。
    auth = approvals_mod.sync_absences()
    if not auth.get("ok"):
        auth["fallback"] = approvals_mod.sync_all_authorized()
    t = tasks_mod.sync_all_authorized()

    # 另外两个待办源，各自独立：一个挂了不影响另一个。
    #   bitable  一张共享表，负责人统筹用
    #   im_todo  群里发 #待办 命令，随手记
    bt = bitable_mod.sync_records()
    im = im_todo_mod.poll_commands()

    changed = status.apply_all()
    return {"contacts": c, "approvals": auth, "tasks": t,
            "bitable": bt, "im_todo": im, "status_changed": changed}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)

    # -------------------------------------------------- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _handle_callback(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (q.get("code") or [""])[0]
        err = (q.get("error") or [""])[0]
        if err:
            return self._html(f"<h3>授权被拒绝</h3><p>{err}</p><p><a href='/'>返回</a></p>", 400)
        if not code:
            return self._html("<h3>缺少 code</h3><p><a href='/login'>重新授权</a></p>", 400)

        if (q.get("state") or [""])[0] not in ("board", ""):
            return self._html("<h3>state 校验失败</h3><p>请从 /login 重新发起授权</p>", 400)

        conf = config.load()
        app_id, secret = conf.get("app_id") or "", conf.get("app_secret") or ""

        token, err = oauth.exchange_code(
            app_id, secret, code, self._self_url() + "/oauth/callback")
        if err:
            return self._html(f"<h3>换取凭证失败</h3><p>{err}</p>", 400)

        info, err = oauth.userinfo(token["access_token"])
        if err:
            return self._html(f"<h3>读取用户信息失败</h3><p>{err}</p>", 400)

        open_id = info.get("open_id") or info.get("sub")
        name = info.get("name") or info.get("en_name") or ""
        oauth.save_token(open_id, name, token)

        r = approvals_mod.sync_for_user(open_id, token["access_token"])
        status.apply_all()

        self._cookie = f"uid={open_id}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"
        return self._html(
            f"<h3>授权成功</h3><p>{name}，已同步 {r.get('fresh', 0)} 条审批记录。</p>"
            f"<p><a href='/'>回到看板</a></p>")

    def _html(self, body, code=200):
        html = ("<html><head><meta charset='utf-8'><title>授权</title></head>"
                "<body style='font:14px/1.7 system-ui;padding:48px;max-width:520px'>"
                f"{body}</body></html>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        cookie = getattr(self, "_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(html)

    # 无需登录即可访问：登录流程本身，加上只允许本机调用的内部接口（各自在 handler 里校验来源）
    OPEN_PATHS = {"/login", "/oauth/callback", "/api/health", "/api/digest", "/api/announce", "/demo"}

    def _identity(self):
        """返回 (open_id, name, dept) 或 None。

        认谁：cookie 里的 open_id 必须在 users 表里（说明走过飞书授权）。
        部门从 members 表读——那是通讯录同步下来的事实。
        通讯录权限没开时 dept 可能为 None，此时放行但标记为待核实，
        等权限开通后会自动收紧。
        """
        uid = self._uid()
        if not uid:
            return None
        now = int(time.time())
        with db.session() as conn:
            u = conn.execute(
                "SELECT open_id, name, expires_at, refresh_expires_at, refresh_token "
                "FROM users WHERE open_id=?", (uid,)).fetchone()
            if not u:
                return None

            # cookie 只证明「这个人来过」，不证明「凭据还能用」。
            # access 过期、且没有可用的 refresh_token 时，必须重新走一次 OAuth——
            # 否则看板照常打开，但所有需要 token 的同步都在后台静默失败，
            # 表现为「看起来正常、数据一直不动」。
            access_ok = (u["expires_at"] or 0) > now
            refresh_ok = bool(u["refresh_token"]) and (u["refresh_expires_at"] or 0) > now
            if not access_ok and not refresh_ok:
                return None

            m = conn.execute("SELECT department FROM members WHERE open_id=?", (uid,)).fetchone()
        return {"open_id": u["open_id"], "name": u["name"], "dept": (m["department"] if m else None)}

    def _todo_admins(self):
        """能改所有人待办的名单（姓名）。

        为什么需要这个：负责人得替不在电脑前的同事补录，也得能修正别人记错的条目，
        否则只能眼看着数据不准。名单放在 config.todo_admins，加人不用改代码。
        """
        return config.load().get("todo_admins") or []

    def _can_edit(self, me, target_open_id):
        """能不能改 target 的待办：本人，或管理员名单里的人。

        之前的实现是前端只认「本人」，后端完全不校验——
        两头都错：合法的代录被挡住，非法的写入反而没人拦。
        """
        if not me or not target_open_id:
            return False
        if me["open_id"] == target_open_id:
            return True
        return me["name"] in self._todo_admins()

    def _self_url(self):
        """按当前请求推断本站地址。

        为什么要动态算：
        内网用 http://<局域网IP>:8900，公网走隧道是 https://<域名>，
        两者都要能完成 OAuth 回调，写死一个就会让另一个失效。
        规则：Host 带非 443 端口视为内网 http，否则视为隧道 https。
        """
        host = self.headers.get("Host") or "127.0.0.1:8900"
        proto = self.headers.get("X-Forwarded-Proto")
        if not proto:
            proto = "http" if (":" in host and not host.endswith(":443")) else "https"
        return f"{proto}://{host}"

    # 只允许这几个前缀被静态访问，其余（config.json / board.db / app/ / logs/ 等）一律 404
    STATIC_ALLOW = ("/index.html", "/favicon.ico", "/vendor/")

    def _static_allowed(self, path):
        return any(path == p or path.startswith(p) for p in self.STATIC_ALLOW)

    def _deny_page(self):
        self._redirect("/login")

    def _uid(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "uid":
                return v.strip()
        return ""

    MAX_BODY = 64 * 1024

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > self.MAX_BODY:
            self.rfile.read(n)
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -------------------------------------------------- 路由
    def do_GET(self):
        path = self.path.split("?")[0]

        if path not in self.OPEN_PATHS:
            ident = self._identity()
            if not ident:
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": "未登录"}, 401)
                return self._deny_page()

            allow = config.load().get("allow_departments") or []
            if allow and ident["dept"] and ident["dept"] not in allow:
                return self._json(
                    {"ok": False, "error": f"你不是{'/'.join(allow)}成员，无权访问"}, 403)

        if path == "/api/board":
            return self._json(board_payload())
        if path == "/api/absences":
            return self._json(absences_payload())
        if path == "/api/health":
            return self._json(health_payload())
        if path == "/login":
            conf = config.load()
            url = oauth.authorize_url(
                conf.get("app_id") or "", self._self_url() + "/oauth/callback", "board",
                scope=conf.get("oauth_scope") or "")
            return self._redirect(url)

        if path == "/oauth/callback":
            return self._handle_callback()

        if path == "/api/todos":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            open_id = (q.get("open_id") or [""])[0]
            if not open_id:
                return self._json({"ok": False, "error": "缺少 open_id"}, 400)
            return self._json({"ok": True, "items": tasks_mod.list_by_person(open_id)})

        if path == "/api/me":
            uid = self._uid()
            if not uid:
                return self._json({"ok": False, "error": "未登录"})
            with db.session() as conn:
                row = conn.execute("SELECT open_id, name FROM users WHERE open_id=?",
                                   (uid,)).fetchone()
            if not row:
                return self._json({"ok": False, "error": "未授权"})
            return self._json({"ok": True, "open_id": row["open_id"], "name": row["name"],
                               "is_admin": row["name"] in self._todo_admins()})

        if path == "/api/users":
            return self._json({"items": oauth.list_users()})

        if path == "/api/config":
            c = config.load()
            c["app_secret"] = "***" if c.get("app_secret") else ""
            return self._json(c)

        if path == "/demo":
            # 演示页：不登录也能看，用固定数据跑动画
            self.path = "/index.html"
            return super().do_GET()

        if path in ("/", ""):
            self.path = "/index.html"
            path = "/index.html"
        if not self._static_allowed(path):
            return self._json({"ok": False, "error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        """兜住所有未捕获异常。

        任何一个分支抛错都会让 http.server 直接断开连接，
        浏览器只看到「无法访问」，日志里也留不下堆栈，排查全靠猜。
        """
        try:
            self._do_post()
        except Exception as e:
            db.log(False, "post", repr(e))
            try:
                self._json({"ok": False, "error": repr(e)[:200]}, 500)
            except Exception:
                pass

    def _do_post(self):
        path = self.path.split("?")[0]
        if path not in self.OPEN_PATHS and not self._identity():
            return self._json({"ok": False, "error": "未登录"}, 401)
        body = self._read_body()

        if path == "/api/sync":
            try:
                return self._json(sync_all())
            except Exception as e:
                db.log(False, "sync", repr(e))
                return self._json({"ok": False, "error": repr(e)}, 500)

        if path == "/api/digest":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"ok": False, "error": "仅允许本机调用"}, 403)
            return self._json(digest_mod.send_daily(force=bool(body.get("force"))))

        if path == "/api/announce":
            client = self.client_address[0]
            if client not in ("127.0.0.1", "::1"):
                return self._json({"ok": False, "error": "仅允许本机调用"}, 403)
            url = body.get("url") or ""
            if not url.startswith(("http://", "https://")):
                return self._json({"ok": False, "error": "url 不合法"}, 400)
            conf = config.load()
            lines = [f"内网：http://{lan_hint()}:8900"] if lan_hint() else None
            return self._json(notify.announce_url(url, lines))

        if path in ("/api/todos", "/api/todos/toggle", "/api/todos/delete"):
            me = self._identity()
            # 注意：这里只能构造数据，不能调 self._json——
            # _json 会立即写响应头并结束请求，写成 deny = self._json(...)
            # 会让整段路由一律返回 403，连合法请求也进不去。
            DENY = {"ok": False, "error": "只能改自己的待办；需要代录请找负责人"}

            if path == "/api/todos":
                target = body.get("open_id")
                if not self._can_edit(me, target):
                    return self._json(DENY, 403)
                tid, err = tasks_mod.add_manual(target, body.get("title"), body.get("due_ts"))
                if err:
                    return self._json({"ok": False, "error": err}, 400)
                return self._json({"ok": True, "id": tid})

            if path == "/api/todos/toggle":
                tid = body.get("id")
                owner = tasks_mod.owner_of(tid)
                if not self._can_edit(me, owner):
                    return self._json(DENY, 403)
                ok, new_status = tasks_mod.toggle(tid, body.get("status"))
                # 多维表格来源的要写回表格，否则下轮同步会覆盖回未完成
                synced = bitable_mod.update_status(tid, new_status) if new_status else True
                # 顺带回传这个人的新计数：调用方拿到就能就地校正，
                # 不必为了一个数字再拉一次全量 board
                c = tasks_mod.summary_by_person().get(owner) or {}
                return self._json({"ok": ok, "status": new_status, "synced": synced,
                                   "open_id": owner,
                                   "doing": c.get("doing", 0),
                                   "overdue": c.get("overdue", 0)})

            tid = body.get("id")
            if not self._can_edit(me, tasks_mod.owner_of(tid)):
                return self._json(DENY, 403)
            return self._json({"ok": tasks_mod.remove(tid)})

        if path == "/api/config":
            conf = config.save(body)
            conf["app_secret"] = "***" if conf.get("app_secret") else ""
            return self._json({"ok": True, "config": conf})

        return self._json({"ok": False, "error": "未知接口"}, 404)

    def log_message(self, fmt, *args):
        pass
