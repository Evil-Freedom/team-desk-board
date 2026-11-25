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
    """文本版日报。

    实现搬到 card.legacy_lines()，这里只做委托：卡片正文和文本兜底必须是
    同一份文案，两份各写一遍迟早会漂移（改了卡片忘了文本，群里就两套说法）。
    """
    return card.build_text()


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
