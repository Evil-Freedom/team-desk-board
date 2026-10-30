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

from . import config, db, notify, tasks

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

    return "\n".join(lines)


def already_sent_today(chat_id):
    today = datetime.now().date()
    with db.session() as conn:
        row = conn.execute(
            "SELECT ts FROM sync_log WHERE scope='digest' ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["ts"]:
        return False
    return datetime.fromtimestamp(row["ts"]).date() == today


def send_daily(force=False):
    conf = config.load()
    chat_id = conf.get("digest_chat_id") or ""
    if not chat_id:
        return {"ok": False, "error": "config.digest_chat_id 为空"}

    if not force and already_sent_today(chat_id):
        return {"ok": True, "skipped": "今天已发过"}

    text = build_text()
    ok, err = notify.send_to_chat(chat_id, text)
    db.log(ok, "digest", text.replace("\n", " | ")[:300] if ok else f"发送失败：{err}")
    return {"ok": ok, "error": err, "text": text}


def due_now():
    """到点了吗：落在 [设定小时, 设定小时+2) 区间内，且今天还没发。

    为什么限制窗口而不是「过了就补」：
        服务晚上重启时，过了 9 点会立刻补发一条，
        群里突然冒出「今日状态」很突兀。窗口内启动才发，错过就等明天。
    """
    # 线上发布的副本设 BOARD_NO_DIGEST=1：云端和本机各有一份调度器，
    # 都开的话群里每天会收到两条一样的推送，云端这份闭嘴
    if os.environ.get("BOARD_NO_DIGEST"):
        return False

    conf = config.load()
    hour = int(conf.get("digest_hour") or HOUR_DEFAULT)
    now = datetime.now()
    if not (hour <= now.hour < hour + 2):
        return False
    chat_id = conf.get("digest_chat_id") or ""
    if not chat_id:
        return False
    return not already_sent_today(chat_id)
