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
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler

from . import approvals as approvals_mod
from . import attendance as attendance_mod
from . import config, contacts, db, oauth, status
from . import tasks as tasks_mod
from . import bitable as bitable_mod
from . import im_todo as im_todo_mod
from . import digest as digest_mod
from . import internal_auth
from . import notify
from . import session
from . import morning

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 业务同步与晨间统采共用一把锁：
# 两个线程同时跑 sync_all 会互相覆盖（todos 是整批覆盖写），
# 也会把 contacts 的「删除不在名单里的人」跑成竞态。
SYNC_LOCK = threading.Lock()


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


def board_payload(viewer=None):
    conf = config.load()
    lead_name = conf.get("bootstrap_lead") or ""
    species = conf.get("species") or {}

    with db.session() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM members")]

    counts = tasks_mod.summary_by_person()

    # 负责人排最前，其余按姓名，保证每次刷新顺序稳定
    rows.sort(key=lambda m: (0 if m["name"] == lead_name else 1, m["name"]))

    # 考勤是独立于审批的第二事实来源：这里只贴状态，绝不回写 members.status
    att_day = attendance_mod.today()
    att_rows = attendance_mod.records_for_day(att_day)
    att_scope = attendance_mod.identity_map()
    is_admin = bool(viewer and viewer.get("name") in (conf.get("todo_admins") or []))

    members = []
    for m in rows:
        oid = m["open_id"]
        rec = att_rows.get(oid)
        if rec:
            st = attendance_mod.classify(rec, m.get("status"))
        elif oid in att_scope:
            # 在采集范围内但今天还没记录 —— 待同步，不是缺勤
            st = attendance_mod.CHECKIN_PENDING
        else:
            # 不在采集范围（应用缺工号权限）：如实标未知，不能显示成「待打卡」
            st = attendance_mod.CHECKIN_UNKNOWN

        # 精确打卡时间只给负责人：普通成员看到的是「已打卡/待打卡」这种状态词，
        # 拿不到别人几点几分到的公司。
        precise = st == attendance_mod.CHECKED_IN and rec and is_admin
        ct = rec.get("check_in_time") if rec else None
        members.append({
            "open_id": oid,
            "name": m["name"],
            "role": "成员",
            "status": m.get("status") or "normal",
            "doing": (counts.get(oid) or {}).get("doing", 0),
            "overdue": (counts.get(oid) or {}).get("overdue", 0),
            "scarf": m.get("scarf"),
            "loc": m.get("loc"),
            "lead": m["name"] == lead_name,
            "species": species.get(m["name"], "ape"),
            "synced": True,
            # 向后兼容：老前端不认识这个键会忽略，新前端才渲染考勤行
            "attendance": {
                "state": st,
                "text": attendance_mod.STATUS_TEXT.get(st, ""),
                "check_in_time": ct if precise else None,
                "check_in_hm": (time.strftime("%H:%M", time.localtime(ct))
                                if precise and ct else None),
            },
        })

    c_log = db.last_log("contacts") or {}
    a_log = db.last_log("approvals") or {}
    att_log = attendance_mod.last_log() or {}

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
        "attendance": {
            "date": att_day,
            "scope": attendance_mod.current_mode(att_rows),
            "scoped": len(att_rows),
            "total": len(members),
            "full": len(att_rows) >= len(members),
            "synced": bool(att_log.get("ok")),
            "message": att_log.get("message") or "尚未采集",
            "ts": att_log.get("ts"),
            "checked": sum(1 for x in members
                           if x["attendance"]["state"] == attendance_mod.CHECKED_IN),
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


def sync_all(with_attendance=False, run_id=None, day=None):
    """一次全量同步：人 → 审批 → 状态。顺序不能反，状态依赖前两步的结果。

    审批走「按人授权」：每个人授权一次，服务用他自己的 token 拉他自己的单据，
    汇总进同一张 absences 表。不需要管理员批 tenant 级权限。
    没有授权的人，退回应用身份尝试（若应用有权限则能覆盖全员）。

    with_attendance=True 只由晨间统采传入：考勤是「一天一次」的重活，
    5 分钟一轮的常规同步不该带它，否则等于每小时打 12 次考勤接口。
    """
    with SYNC_LOCK:
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

        att = attendance_mod.sync(day=day, run_id=run_id) if with_attendance else None

        changed = status.apply_all()
        return {"contacts": c, "approvals": auth, "tasks": t,
                "bitable": bt, "im_todo": im, "status_changed": changed,
                "attendance": att}


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
        ck = getattr(self, "_upgrade_cookie", None)
        if ck:
            self.send_header("Set-Cookie", ck)
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
        if not open_id:
            return self._html("<h3>读取用户信息失败</h3><p>返回里没有 open_id</p>", 400)

        # 准入闸门：OAuth 成功 ≠ 有权看看板。
        # 原来这里直接 save_token，等于任何飞书用户点一次登录就进了授权表；
        # 现在要求「在运营部名单里」且「部门命中白名单」才落库。
        allow = conf.get("allow_departments") or []
        with db.session() as conn:
            m = conn.execute("SELECT name, department FROM members WHERE open_id=?",
                             (open_id,)).fetchone()
        dept = (m["department"] or "").strip() if m else ""
        if not allow:
            db.log(False, "auth", f"OAuth 拒绝 {name}：未配置部门白名单")
            return self._html(
                "<h3>看板尚未配置访问范围</h3>"
                "<p>服务端没有设置部门白名单，为安全起见拒绝所有访问。"
                "请联系负责人补齐配置后重试。</p>", 403)
        if not m:
            db.log(False, "auth", f"OAuth 拒绝 {name}：不在成员名单")
            return self._html(
                "<h3>你不在运营部名单内</h3>"
                "<p>这个看板只对运营部成员开放。如果你刚入职，"
                "等名单同步（约 5 分钟）后再试。</p>", 403)
        if dept not in allow:
            db.log(False, "auth", f"OAuth 拒绝 {name}：部门 {dept or '未知'} 不在白名单")
            return self._html(
                f"<h3>无权访问</h3><p>你属于「{dept or '未知部门'}」，"
                f"这个看板仅限「{'/'.join(allow)}」。</p>", 403)

        oauth.save_token(open_id, name, token)

        r = approvals_mod.sync_for_user(open_id, token["access_token"])
        status.apply_all()

        value = session.issue(open_id)
        if not value:
            return self._html(
                "<h3>服务端未配置会话密钥</h3>"
                "<p>board.env 里缺少 QL_INTERNAL_TOKEN / BOARD_SESSION_SECRET，"
                "无法安全签发登录态。请联系负责人补齐后重试。</p>", 500)
        self._cookie = session.cookie_header(value, self._secure_request())
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
        cookie = getattr(self, "_cookie", None) or getattr(self, "_upgrade_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(html)

    # 无需登录即可访问：登录流程本身，加上只允许本机调用的内部接口（各自在 handler 里校验来源）
    OPEN_PATHS = {"/login", "/oauth/callback", "/api/health", "/api/digest", "/api/announce",
                  "/demo", "/internal/attendance/self"}

    def _internal_self(self):
        """QuickLaunch 取「本人」考勤证据。三条硬边界：

        1) 只认服务间签名，不认 cookie、不认匿名；
        2) device_id 必须先在 config.quicklaunch.device_bindings 登记过——
           请求方改参数也读不到别人的记录，服务端不接受任何 open_id/employee_no 入参；
        3) 只回当天一条。不给列表、不给历史、不给导出。
        """
        raw_path = self.path
        ok, reason = internal_auth.verify(self.headers, "GET", raw_path)
        if not ok:
            db.log(False, "internal", f"拒绝内部请求：{reason}")
            return self._json({"ok": False, "error": "unauthorized"}, 401)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        device_id = (q.get("device_id") or [""])[0].strip()
        day = (q.get("attendance_date") or [""])[0].strip() or attendance_mod.today()
        if not device_id:
            return self._json({"ok": False, "error": "缺少 device_id"}, 400)

        bind = ((config.load().get("quicklaunch") or {}).get("device_bindings") or {})
        open_id = bind.get(device_id)
        if not open_id:
            return self._json({"ok": False, "error": "device_not_bound"}, 403)

        rec = attendance_mod.find_self(open_id=open_id, day=day)
        if not rec:
            # 还没采到：这是「待同步」，不是失败。调用方必须继续等，不许当缺勤
            return self._json({"ok": True, "found": False, "attendance_date": day})

        st = attendance_mod.classify(rec)
        return self._json({
            "ok": True,
            "found": True,
            "attendance_date": rec["attendance_date"],
            "employee_no": rec["employee_no"],
            "record_id": rec.get("check_in_record_id") or "",
            "result": rec.get("check_in_result") or "",
            "check_time": rec.get("check_in_time"),
            "state": st,
            "run_id": rec.get("run_id"),
            "fetched_at": rec.get("fetched_at"),
            "source": rec.get("source"),
        })


    def _identity(self):
        """返回 (open_id, name, dept) 或 None。

        认谁：cookie 里的 open_id 必须在 users 表里（说明走过飞书授权）。
        部门从 members 表读——那是通讯录同步下来的事实。
        通讯录权限没开时 dept 可能为 None，此时放行但标记为待核实，
        等权限开通后会自动收紧。
        """
        uid, state = self._session()
        if state not in ("ok", "legacy") or not uid:
            return None
        if state == "legacy":
            # 旧明文 Cookie：本次放行，同时立刻换发签名 Cookie。
            # 换发之后旧格式不再被接受，伪造 open_id 的窗口只在这一个请求内。
            fresh = session.issue(uid)
            if fresh:
                self._upgrade_cookie = session.cookie_header(fresh, self._secure_request())
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

    def _deny(self, msg, code=403):
        """拒绝访问。API 路径给 JSON，页面给可读 HTML。"""
        if self.path.startswith("/api/"):
            return self._json({"ok": False, "error": msg}, code)
        return self._html(
            f"<h3>无权访问</h3><p>{msg}</p>"
            f"<p><a href='/login'>换一个账号登录</a></p>", code)

    def _authorize(self):
        """登录 + 部门双闸门。不通过时已经把响应写好了，调用方直接 return。

        为什么必须 fail-closed：
            原来的判断是 `if allow and dept and dept not in allow`——
            三个条件任一为空就放行。表现是：配置丢了白名单、通讯录没同步到部门、
            dept 为空时，**任何登录用户都能看整个部门的数据**。
            这类「配置坏了反而更宽松」的默认值是最危险的一种。
        """
        ident = self._identity()
        if not ident:
            if self.path.startswith("/api/"):
                self._json({"ok": False, "error": "未登录"}, 401)
            else:
                self._deny_page()
            return None

        allow = config.load().get("allow_departments") or []
        if not allow:
            db.log(False, "auth", f"拒绝 {ident['name']}：服务端未配置部门白名单")
            self._deny("服务端未配置部门白名单，已拒绝访问")
            return None

        dept = (ident.get("dept") or "").strip()
        if not dept:
            # 通讯录没同步到这个人（或权限不足）时不能猜，只能拒
            db.log(False, "auth", f"拒绝 {ident['name']}：无法核实部门")
            self._deny("无法核实你的所属部门，已拒绝访问")
            return None
        if dept not in allow:
            db.log(False, "auth", f"拒绝 {ident['name']}：部门 {dept} 不在白名单")
            self._deny(f"你不是「{'/'.join(allow)}」成员，无权访问")
            return None
        return ident

    def _is_admin(self, ident):
        """管理员只看 config.todo_admins 名单，与「是不是部门成员」无关。"""
        return bool(ident) and ident.get("name") in self._todo_admins()

    def _session(self):
        """返回 (open_id, 状态)。状态 ok/legacy/bad，见 session.verify。"""
        raw = self.headers.get("Cookie") or ""
        value = ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == session.COOKIE_NAME:
                value = v.strip()
                break
        return session.verify(value)

    def _uid(self):
        """兼容旧调用点：只返回 open_id，没有或校验失败返回空串。"""
        open_id, state = self._session()
        return open_id if state in ("ok", "legacy") else ""

    def _secure_request(self):
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

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

        ident = None
        if path not in self.OPEN_PATHS:
            ident = self._authorize()
            if ident is None:
                return

        if path == "/api/board":
            return self._json(board_payload(ident))
        if path == "/internal/attendance/self":
            return self._internal_self()
        if path == "/api/absences":
            # 含全部门的请假类型与出差城市，属于管理视角数据
            if not self._is_admin(ident):
                return self._deny("仅负责人可查看不在岗明细")
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
            # 原来这里没有权限判断：任何登录成员都能列出别人的全部待办标题。
            # 待办是工作内容，属于个人信息，按「本人或负责人」收口。
            if not self._can_edit(ident, open_id):
                return self._deny("只能查看自己的待办；需要代查请找负责人")
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
            if not self._is_admin(ident):
                return self._deny("仅负责人可查看授权清单")
            return self._json({"items": oauth.list_users()})

        if path == "/api/config":
            if not self._is_admin(ident):
                return self._deny("仅负责人可查看配置")
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
        # HTML 不缓存：改版后手机上如果还拿旧页面，会一直看到上一版界面
        if path.endswith(".html"):
            self._no_cache = True
        return super().do_GET()

    def end_headers(self):
        if getattr(self, "_no_cache", False):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _internal_plan(self):
        """QuickLaunch 上报「今天第一个待核验任务」，用来登记晨间统采。

        幂等：同一天无论上报多少次，morning.plan() 都只返回已有那条 run，
        scheduled_at 也不会被后来的上报改晚（先到先算，后来者不该推迟全部门）。
        """
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if 0 < n <= self.MAX_BODY else b""
        ok, reason = internal_auth.verify(self.headers, "POST", self.path, raw)
        if not ok:
            db.log(False, "internal", f"拒绝内部请求：{reason}")
            return self._json({"ok": False, "error": "unauthorized"}, 401)

        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return self._json({"ok": False, "error": "invalid_json"}, 400)

        device_id = str(body.get("device_id") or "").strip()
        bind = ((config.load().get("quicklaunch") or {}).get("device_bindings") or {})
        if not device_id or device_id not in bind:
            return self._json({"ok": False, "error": "device_not_bound"}, 403)

        from datetime import datetime as _dt
        ev_ts = int(body.get("event_time") or time.time())
        ev_dt = _dt.fromtimestamp(ev_ts)
        if attendance_mod.today(ev_dt) != attendance_mod.today():
            # 跨天的迟到事件：不拿它去创建「昨天」的统采
            ev_dt = _dt.now()

        run = morning.plan(trigger_source=str(body.get("type") or "checkin_unknown"), now=ev_dt)
        return self._json({
            "ok": True,
            "run_id": run["run_id"],
            "business_date": run["business_date"],
            "scheduled_at": run["scheduled_at"],
            "already": bool(run.get("started_at")),
        })

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

        # 内部接口要先拿原始 body 验签，不能走 _read_body（它会先把流读空）
        if path == "/internal/morning/plan":
            return self._internal_plan()

        ident = None
        if path not in self.OPEN_PATHS:
            # 写操作同样要过部门闸门：原来只校验「登录」，等于任何飞书用户都能写
            ident = self._authorize()
            if ident is None:
                return
        body = self._read_body()

        if path == "/api/sync":
            if not self._is_admin(ident):
                return self._deny("仅负责人可手动触发同步")
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
            me = ident
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
            if not self._is_admin(ident):
                return self._deny("仅负责人可修改配置")
            conf = config.save(body)
            conf["app_secret"] = "***" if conf.get("app_secret") else ""
            return self._json({"ok": True, "config": conf})

        return self._json({"ok": False, "error": "未知接口"}, 404)

    def log_message(self, fmt, *args):
        pass
