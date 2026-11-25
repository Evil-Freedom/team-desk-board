# -*- coding: utf-8 -*-
"""群日报的交互式消息卡片（飞书卡片 JSON 2.0）。

为什么不再发纯文本：
    文本摘要在群里是一大段「在岗 9 人 · 休假 2 …」，手机上一屏塞满，
    真正想知道的「今天到底几个人在」要自己数。卡片把它压成三个数字 +
    一条入口按钮，扫一眼就够，细节留在线看板。

口径（全部来自生产库，没有任何硬编码）：
    · 应到    = members 表行数
    · 请假    = status='leave'
    · 出差外勤 = status='away'
    · 审批在岗 = status='normal'
    · 考勤已确认 = 在岗人员中 state=CHECKED_IN 的
    · 待确认  = 在岗人员中既没确认、也查不到明确结果的

为什么不把「待确认」算成缺勤：
    采集覆盖不到（per_user 只覆盖已授权成员）时，查不到 ≠ 没来。
    所以宁可显示「待确认 9」，并在副标题写明「数据未完整」，也不给一个假结论。
"""
import time
from datetime import datetime

from . import attendance, config, db

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
CARD_VERSION = "v1"
BOARD_URL = "https://xbyyb.duckdns.org"


def _esc(text, limit=40):
    """卡片里的文本做最小转义：markdown 组件会把 < > & 当结构字符。"""
    s = str(text or "")[:limit]
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def collect(day=None, now=None):
    """汇总卡片需要的全部数字。纯读库，不发网络请求。"""
    now = now or datetime.now()
    day = day or attendance.today(now)

    with db.session() as conn:
        people = [dict(r) for r in conn.execute(
            "SELECT open_id, name, status, loc FROM members")]
        urows = [dict(r) for r in conn.execute(
            "SELECT open_id, scope, expires_at, refresh_expires_at, refresh_token FROM users")]

    normal = [p for p in people if (p["status"] or "normal") == "normal"]
    leave = [p for p in people if p["status"] == "leave"]
    away = [p for p in people if p["status"] == "away"]

    records = attendance.records_for_day(day)
    checked = 0
    pending = 0
    for p in normal:
        rec = records.get(p["open_id"])
        state = attendance.classify(rec, p["status"]) if rec else attendance.CHECKIN_PENDING
        # 记录存在但状态是 UNKNOWN（比如只有 result 没有 record_id）也算未确认
        if state == attendance.CHECKED_IN:
            checked += 1
        else:
            pending += 1

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
        "checked": checked,
        "pending": pending,
        "leave_names": [p["name"] for p in leave],
        "away_names": [(p["name"], p.get("loc") or "外出") for p in away],
        "capability": cap,
        "complete": bool(people) and cap["full"],
        "synced": bool(people),
    }


def _stat_column(value, label, color):
    """三列核心统计里的一列。数字大、标签小，移动端一行能放下三列。"""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "vertical_align": "top",
        "elements": [{
            "tag": "markdown",
            "text_align": "center",
            "content": f"<font color='{color}' size='28'>**{value}**</font>\n"
                       f"<font color='grey' size='12'>{_esc(label, 12)}</font>",
        }],
    }


def build(day=None, now=None):
    """构造卡片 JSON。返回 dict，调用方序列化后作为 msg_type=interactive 的 content。"""
    d = collect(day, now)

    if not d["synced"]:
        # 连成员都没同步到时不要发一张全是 0 的卡片
        status_text = "数据尚未同步"
        body_note = "成员数据还没同步到，这条只是占位，请检查服务是否正常。"
    elif d["complete"]:
        status_text = "数据完整"
        body_note = "考勤覆盖全部成员，以下为实时统计。"
    else:
        cap = d["capability"]
        status_text = f"数据未完整（考勤覆盖 {cap['scoped']}/{cap['total']}）"
        body_note = ("部分成员的考勤尚未采集到，未采集的人计入「待确认」，"
                     "不代表缺勤。详情以在线看板为准。")

    subtitle = f"{d['date']} {d['weekday']} · 更新 {d['updated']} · {status_text}"

    elements = [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "default",
            "columns": [
                _stat_column(d["total"], "今日应到", "blue"),
                _stat_column(d["normal"], "审批在岗", "green"),
                _stat_column(d["pending"], "待确认", "orange"),
            ],
        },
        {"tag": "markdown", "content": body_note, "text_size": "notation"},
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": (f"**请假 {d['leave']}** · **出差/外勤 {d['away']}** · "
                        f"**考勤已确认 {d['checked']}**"),
        },
    ]

    # 次要状态列表：只列姓名与类型，不出现打卡时间、record_id 等证据
    if d["leave_names"]:
        elements.append({
            "tag": "markdown",
            "content": "请假：" + "、".join(_esc(n, 12) for n in d["leave_names"][:12]),
            "text_size": "notation",
        })
    if d["away_names"]:
        seg = "、".join(f"{_esc(n, 12)}·{_esc(loc, 10)}" for n, loc in d["away_names"][:12])
        elements.append({"tag": "markdown", "content": "出差/外勤：" + seg,
                         "text_size": "notation"})

    elements += [
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
            "title": {"tag": "plain_text", "content": f"{_esc(d['department'], 20)} · 今日在岗概览"},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": "blue",
            "padding": "12px 12px 10px 12px",
        },
        "body": {"elements": elements},
    }


def build_text(day=None, now=None):
    """纯文本兜底：卡片发送失败时至少让群里看到数字。"""
    d = collect(day, now)
    return "\n".join([
        f"【{d['department']} · 今日在岗概览】",
        f"{d['date']} {d['weekday']} · 更新 {d['updated']}",
        f"应到 {d['total']} · 审批在岗 {d['normal']} · 待确认 {d['pending']}",
        f"请假 {d['leave']} · 出差外勤 {d['away']} · 考勤已确认 {d['checked']}",
        f"数据：{'完整' if d['complete'] else '未完整（考勤覆盖 %d/%d）' % (d['capability']['scoped'], d['capability']['total'])}",
        BOARD_URL,
    ])
