# -*- coding: utf-8 -*-
"""群日报的交互式消息卡片（飞书卡片 JSON 2.0）。

设计原则：**内容照搬旧日报，只换呈现方式**。
    旧日报在群里已经形成阅读习惯——「在岗 10 人 · 休假 1」+ 谁休假 + 谁有在办。
    改造只做三件事：加彩色标题条、加分割线、加一个进看板的按钮。
    正文一个字都没重写，避免「换了个样子就不认识了」。

口径（全部读生产库，无硬编码）：
    · 在岗 = status='normal'（审批层面在岗，不是考勤口径）
    · 休假 = status='leave'，loc 是假期类型（年假/调休假）
    · 出差 = status='away'，loc 是城市
    · 在办 = tasks.summary_by_person()

为什么不把「查不到考勤」写成缺勤：
    采集覆盖不到（per_user 只覆盖已授权成员）时，查不到 ≠ 没来。
    完整性只写在副标题（数据未完整（考勤覆盖 1/11）），正文不推任何结论。
"""
from datetime import datetime

from . import attendance, config, db, tasks

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
CARD_VERSION = "v1"
BOARD_URL = "https://xbyyb.duckdns.org"


def _esc(text, limit=40):
    """卡片 markdown 会把 < > & 当结构字符，做最小转义。"""
    s = str(text or "")[:limit]
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def collect(day=None, now=None):
    """汇总卡片需要的全部数字。纯读库，不发网络请求。"""
    now = now or datetime.now()
    day = day or attendance.today(now)

    with db.session() as conn:
        people = [dict(r) for r in conn.execute(
            "SELECT open_id, name, status, loc FROM members")]
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM attendance_records WHERE attendance_date=?", (day,))]

    records = {}
    for r in rows:
        if r.get("open_id"):
            records[r["open_id"]] = r

    normal = [p for p in people if (p["status"] or "normal") == "normal"]
    leave = [p for p in people if p["status"] == "leave"]
    away = [p for p in people if p["status"] == "away"]

    checked = pending = 0
    for p in normal:
        rec = records.get(p["open_id"])
        state = attendance.classify(rec, p["status"]) if rec else attendance.CHECKIN_PENDING
        if state == attendance.CHECKED_IN:
            checked += 1
        else:
            pending += 1

    counts = tasks.summary_by_person()
    busy = []
    for p in people:
        c = counts.get(p["open_id"]) or {}
        doing = c.get("doing") or 0
        if doing:
            busy.append((doing, c.get("overdue") or 0, p["name"]))
    busy.sort(reverse=True)

    cap = attendance.capability()
    return {
        "department": config.load().get("department") or "团队",
        "date": day,
        "weekday": WEEKDAYS[now.weekday()],
        "updated": now.strftime("%H:%M"),
        "updated_full": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(people),
        "normal": len(normal),
        "leave": len(leave),
        "away": len(away),
        "leave_list": [(p["name"], p.get("loc") or "") for p in leave],
        "away_list": [(p["name"], p.get("loc") or "外出") for p in away],
        "busy": busy,
        "overdue_total": sum(o for _, o, _ in busy),
        "checked": checked,
        "pending": pending,
        "capability": cap,
        "complete": bool(people) and cap["full"],
        "synced": bool(people),
    }


def legacy_lines(d=None, day=None, now=None):
    """旧日报的正文行，一字不改地搬过来。

    digest.build_text() 也调用这个函数，保证「卡片」和「文本兜底」
    永远说同样的话——两份文案各写一遍，迟早会漂移。
    """
    d = d or collect(day, now)
    if not d["synced"]:
        return ["还没有同步到成员数据，请检查服务是否正常。"]

    head = []
    if not d["away"] and not d["leave"]:
        head.append("今天全员在岗")
    else:
        head.append(f"在岗 {d['normal']} 人")
        if d["away"]:
            head.append(f"出差 {d['away']}")
        if d["leave"]:
            head.append(f"休假 {d['leave']}")

    lines = [" · ".join(head)]

    def detail(items, tag):
        if not items:
            return None
        seg = [f"{_esc(n, 12)}·{_esc(loc, 10)}" if loc else _esc(n, 12) for n, loc in items]
        return f"{tag}：" + "、".join(seg)

    for group, tag in ((d["away_list"], "出差"), (d["leave_list"], "休假")):
        line = detail(group, tag)
        if line:
            lines.append(line)

    if d["busy"]:
        shown = d["busy"][:5]
        parts = []
        for doing, overdue, name in shown:
            seg = f"{_esc(name, 12)} {doing} 件"
            if overdue:
                seg += f"（逾期 {overdue}）"
            parts.append(seg)
        tail = (f"，另有 {len(d['busy']) - len(shown)} 人有在办"
                if len(d["busy"]) > len(shown) else "")
        lines.append("在办：" + "、".join(parts) + tail)
    else:
        lines.append("在办：暂无记录")

    if d["overdue_total"]:
        lines.append(f"⚠ 逾期合计 {d['overdue_total']} 件")

    return lines


def _status_text(d):
    if not d["synced"]:
        return "数据尚未同步"
    if d["complete"]:
        return "数据完整"
    cap = d["capability"]
    return f"数据未完整（考勤覆盖 {cap['scoped']}/{cap['total']}）"


def build(day=None, now=None):
    """构造卡片：旧日报的正文 + 卡片外壳（标题条 / 分割线 / 按钮）。"""
    d = collect(day, now)
    body = "\n".join(legacy_lines(d))
    subtitle = f"{d['date']} {d['weekday']} · 更新 {d['updated']} · {_status_text(d)}"

    elements = [
        {"tag": "markdown", "content": body},
        {"tag": "hr"},
        {"tag": "markdown",
         "content": f"<font color='grey' size='12'>数据更新于 {d['updated_full']} · "
                    f"口径：审批状态 + 考勤采集</font>"},
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开运营部在线看板"},
            "type": "primary",
            "width": "fill",
            "url": BOARD_URL,
        },
    ]

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"{_esc(d['department'], 20)} · 今日状态"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": "blue",
            "padding": "12px 12px 10px 12px",
        },
        "body": {"elements": elements},
    }


def build_text(day=None, now=None):
    """纯文本兜底：卡片发送失败时至少让群里看到同样的话。"""
    d = collect(day, now)
    head = f"【{d['department']} · 今日状态】"
    return "\n".join([head] + legacy_lines(d) + [BOARD_URL])
