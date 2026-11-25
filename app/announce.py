# -*- coding: utf-8 -*-
"""上线公告：一次性、可验证、不可重复。

为什么单独成模块而不是直接在某个接口里发一条群消息：
    公告只有一次机会。发早了（C 级还没过）等于对着全部门宣布一个没验收完的系统；
    发重了（重试、重启、重复调用）是同一件事通知两遍，比不发更糟。
    所以这里把「该不该发」「发过没有」「发成功没有」三件事都落库。

状态机：
    PENDING --claim--> SENDING --成功--> SENT（带真实 message_id）
                                 \\--失败--> FAILED（可重试，但仍需 preflight）

幂等键固定为 `team-desk-board-public-launch-v1`，主键唯一约束兜底，
不依赖「先查再插」这种竞态写法。
"""
import time

from . import attendance, config, db, notify

KEY = "team-desk-board-public-launch-v1"
BODY_VERSION = "v1"

# 公告正文：与需求给定的版本逐字一致，改动需同步升 BODY_VERSION
TITLE = "📢 运营部状态看板正式开放外网访问"
BODY = """各位同事：

运营部状态看板现已完成正式部署和全部验收，现已支持外网访问。

🌐 看板访问地址：

https://xbyyb.duckdns.org

使用说明：

1. 手机、电脑均可通过浏览器访问。
2. 无需连接公司内网。
3. 使用本人飞书账号登录。
4. 仅获得授权的运营部成员可以查看对应信息。
5. 普通成员和管理员按照原有权限展示内容。

如果遇到无法登录或没有访问权限的情况，请联系看板管理员。

欢迎大家使用！"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    announcement_key TEXT PRIMARY KEY,
    status           TEXT NOT NULL,      -- PENDING | SENDING | SENT | FAILED
    message_id       TEXT,
    target_chat_id   TEXT,
    body_version     TEXT,
    created_at       INTEGER,
    sent_at          INTEGER,
    note             TEXT
)"""


def _ensure():
    with db.session() as conn:
        conn.execute(SCHEMA)


def get(key=KEY):
    _ensure()
    with db.session() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE announcement_key=?",
                           (key,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ 前置条件
def preflight():
    """C 级验收的可程序化部分。任何一条 False 都不许发。

    这里不检查「管理员是否批了权限」这种东西——它无法从代码里读出来，
    只能体现为「覆盖范围是否达到全员」。所以用 attendance 的实际覆盖说话。
    """
    checks = {}
    conf = config.load()

    checks["target_chat"] = bool(conf.get("digest_chat_id"))
    checks["allow_departments"] = bool(conf.get("allow_departments"))

    # 考勤覆盖：必须真的覆盖到全部成员，per_user 局部覆盖不算
    att = attendance.summary()
    checks["attendance_full"] = bool(att and att.get("full"))
    checks["attendance_mode"] = (att or {}).get("coverage") == "team"

    # 今天必须真正跑过一次晨间统采
    day = attendance.today()
    with db.session() as conn:
        run = conn.execute(
            "SELECT attendance_sync_status, digest_status, completed_at "
            "FROM morning_runs WHERE business_date=?", (day,)).fetchone()
    checks["morning_ran"] = bool(run and run["completed_at"])
    checks["morning_attendance_ok"] = bool(run and run["attendance_sync_status"] in ("ok", "partial"))

    # 09:00 固定推送必须已经彻底停用
    from . import digest
    checks["digest_due_now_disabled"] = digest.due_now() is False

    return {"ok": all(checks.values()), "checks": checks, "attendance": att}


# ------------------------------------------------------------------ 发送
def _claim(key):
    """认领发送权。返回 (可发送, 现有记录, 原因)。"""
    _ensure()
    with db.session() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE announcement_key=?",
                           (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO announcements (announcement_key, status, created_at, body_version) "
                "VALUES (?,?,?,?)", (key, "SENDING", int(time.time()), BODY_VERSION))
            return True, None, "首次发送"
        row = dict(row)
        if row["status"] == "SENT":
            return False, row, "已发送过，不再重复"
        if row["status"] == "SENDING":
            # 进程在发送中途挂掉时会留下 SENDING。此时结果不确定，
            # 不能盲目重发；由调用方查群消息确认后再人工/脚本改状态。
            return False, row, "上次发送结果未确认，需先核对群消息"
        cur = conn.execute(
            "UPDATE announcements SET status='SENDING', created_at=? "
            "WHERE announcement_key=? AND status='FAILED'", (int(time.time()), key))
        if cur.rowcount == 1:
            return True, row, "失败重试"
        return False, row, "状态异常，拒绝发送"


def _mark(key, status, message_id=None, note=""):
    with db.session() as conn:
        conn.execute(
            "UPDATE announcements SET status=?, message_id=?, sent_at=?, note=? "
            "WHERE announcement_key=?",
            (status, message_id, int(time.time()) if status == "SENT" else None,
             str(note)[:300], key))


def send_launch(key=KEY, force_preflight=True):
    """发公告。preflight 不过直接拒绝，绝不「先发了再说」。"""
    pf = preflight()
    if force_preflight and not pf["ok"]:
        return {"ok": False, "reason": "preflight_failed", "preflight": pf}

    ok_claim, row, why = _claim(key)
    if not ok_claim:
        return {"ok": row.get("status") == "SENT", "reason": why, "record": row}

    chat_id = config.load().get("digest_chat_id") or ""
    text = f"{TITLE}\n\n{BODY}"
    ok, err, mid = notify.send_to_chat_ex(chat_id, text)
    if ok:
        _mark(key, "SENT", mid, f"chat={chat_id}")
        db.log(True, "announce", f"上线公告已发送 message_id={mid}")
        return {"ok": True, "message_id": mid, "target_chat_id": chat_id,
                "body_version": BODY_VERSION}
    _mark(key, "FAILED", None, err)
    db.log(False, "announce", f"上线公告发送失败：{err}")
    return {"ok": False, "error": err, "reason": "send_failed"}
