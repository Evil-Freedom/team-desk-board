# -*- coding: utf-8 -*-
"""每日摘要：把看板压成一条消息，推到群里。

为什么单独成模块：
    摘要是「给人看的」，和 api.board_payload() 面向前端的结构不一样——
    前端要每个人的完整卡片，群里只需要异常和计数。
    分开写，改推送文案不会牵动接口。
"""
import time
import os
from datetime import datetime

from . import attendance, card, config, db, notify, tasks

HOUR_DEFAULT = 9


def _dept():
    """部门名取配置，改部署只改 config.json，不用动代码。"""
    return config.load().get("department") or "团队"


def _members():
    with db.session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT open_id, name, status, loc FROM members ORDER BY rowid")]


def build_text():
    """生成摘要文本。数据不足时给出可读的说明，不返回空消息。"""
    people = _members()
    if not people:
        return f"【{_dept()} · 今日状态】\n还没有同步到成员数据，请检查服务是否正常。"

    counts = tasks.summary_by_person()

    on_duty = [p for p in people if p["status"] not in ("away", "leave")]
    away = [p for p in people if p["status"] == "away"]
    leave = [p for p in people if p["status"] == "leave"]

    head_parts = []
    if not away and not leave:
        head_parts.append("今天全员在岗")
    else:
        head_parts.append(f"在岗 {len(on_duty)} 人")
        if away:
            head_parts.append(f"出差 {len(away)}")
        if leave:
            head_parts.append(f"休假 {len(leave)}")

    lines = [f"【{_dept()} · 今日状态】", " · ".join(head_parts)]

    def detail(group, tag):
        """休假行的 loc 是假期类型（年假/调休假），不是事由——事由在同步阶段已丢弃。"""
        if not group:
            return None
        items = [f"{p['name']}·{p['loc']}" if p.get("loc") else p["name"] for p in group]
        return f"{tag}：" + "、".join(items)

    for g, tag in ((away, "出差"), (leave, "休假")):
        line = detail(g, tag)
        if line:
            lines.append(line)

    # 负载：只报有在办的，按件数降序，最多 5 个，避免刷屏
    busy = []
    for p in people:
        c = counts.get(p["open_id"]) or {}
        doing = c.get("doing") or 0
        if doing:
            busy.append((doing, c.get("overdue") or 0, p["name"]))
    busy.sort(reverse=True)

    if busy:
        shown = busy[:5]
        parts = []
        for doing, overdue, name in shown:
            seg = f"{name} {doing} 件"
            if overdue:
                seg += f"（逾期 {overdue}）"
            parts.append(seg)
        tail = f"，另有 {len(busy) - len(shown)} 人有在办" if len(busy) > len(shown) else ""
        lines.append("在办：" + "、".join(parts) + tail)
    else:
        lines.append("在办：暂无记录")

    overdue_total = sum(o for _, o, _ in busy)
    if overdue_total:
        lines.append(f"⚠ 逾期合计 {overdue_total} 件")

    # 考勤汇总：只在真有采集数据时加。没有数据就整行不出现，
    # 绝不写「全员未打卡」这种从缺失推出来的结论。
    att = attendance.summary()
    if att:
        if att.get("full"):
            seg = f"考勤（全员）：已打卡 {att['checked']}/{att['total']}"
        else:
            # 覆盖不全时分母只能写「已采集人数」：写成 1/11 会让人
            # 以为十个人没来上班，而事实是我们只查到了一个人
            seg = f"考勤（已采集 {att['scoped']}/{att['total']} 人）：其中已打卡 {att['checked']}"
        lines.append(seg)

    return "\n".join(lines)


def already_sent_today(chat_id):
    today = datetime.now().date()
    with db.session() as conn:
        row = conn.execute(
            "SELECT ts FROM sync_log WHERE scope='digest' ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["ts"]:
        return False
    return datetime.fromtimestamp(row["ts"]).date() == today


def _claim(business_date, force=False):
    """把「今天这条该发」原子地认领下来。

    为什么用 UPDATE ... WHERE 而不是先查后写：
        调度 tick 与手动触发同时走到这里时，只有一方的 rowcount 会是 1；
        先查后写会双双通过检查，群里就能收到两条一样的摘要。
    """
    with db.session() as conn:
        if force:
            conn.execute("UPDATE morning_runs SET digest_status='sending' WHERE business_date=?",
                         (business_date,))
            return True
        cur = conn.execute(
            "UPDATE morning_runs SET digest_status='sending' "
            "WHERE business_date=? AND COALESCE(digest_status,'pending') IN ('pending','failed')",
            (business_date,))
        return cur.rowcount == 1


def _mark(day, status):
    with db.session() as conn:
        conn.execute(
            "UPDATE morning_runs SET digest_status=?, digest_sent_at=? WHERE business_date=?",
            (status, int(time.time()) if status == "sent" else None, day))


def _legacy_sent_today(day):
    """迁移当天的兼容判断：旧机制（固定 9:00）可能已经发过一条。

    sync_log 里今天有成功的 digest 记录，就说明群里已经收到过今天的摘要，
    新机制不能再补一条——用户看到的是「一天两条一模一样的推送」。
    这条判断只在切轨当天真正起作用，之后 morning_runs 自己就能兜住。
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT ts, message FROM sync_log WHERE scope='digest' AND ok=1 "
            "ORDER BY id DESC LIMIT 20").fetchall()
    for r in rows:
        if not r["ts"]:
            continue
        if datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d") != day:
            continue
        if "发送失败" not in (r["message"] or ""):
            return True
    return False


def send_once(business_date=None, run_id=None, text=None):
    """晨间统采完成后发送一次。已发送/正在发送则直接跳过，绝不二次打扰。"""
    day = business_date or attendance.today()
    if _legacy_sent_today(day):
        _mark(day, "skipped")
        db.log(True, "digest", f"{day} 旧机制已推送过，本次跳过")
        return {"ok": True, "skipped": "本日已有推送记录（迁移兼容）"}
    if not _claim(day):
        return {"ok": True, "skipped": "本日已发送或正在发送"}

    conf = config.load()
    chat_id = conf.get("digest_chat_id") or ""
    if not chat_id:
        _mark(day, "skipped")
        return {"ok": False, "error": "config.digest_chat_id 为空"}

    body = text or build_text()
    ok, err = notify.send_to_chat(chat_id, body)
    _mark(day, "sent" if ok else "failed")
    db.log(ok, "digest",
           f"run={run_id or '-'} " + (body.replace("\n", " | ")[:260] if ok else f"发送失败：{err}"))
    return {"ok": ok, "error": err, "text": body, "business_date": day}


def send_daily(force=False):
    """手动/兜底入口（仅 127.0.0.1 可调）。日常推送不走这条。"""
    day = attendance.today()
    if force:
        _claim(day, force=True)
    return send_once(business_date=day)


def due_now():
    """已停用：固定 09:00 推送的判定入口。

    保留函数是给旧调用方一个明确的 False，而不是 AttributeError；
    但**不允许**再有人拿它当调度依据——日报现在只由 morning.tick()
    在统一采集完成后触发。要恢复定时推送，等于推翻 2026-11-25 的决定。
    """
    return False
