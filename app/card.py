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
    """群卡片里的状态提示：只说业务结论，不说采集过程。

    「考勤覆盖 1/11」这类话对群成员没有意义——他们要看的是今天几个人在，
    不是我们采到了几条记录。技术细节留在 attendance_sync_log 与 /api/health。
    """
    if d["synced"] and d["complete"]:
        return ""                      # 完整就什么都不用说
    return "部分信息暂未更新"


def _stat(value, label, color):
    """四列人数里的一列：数字大、标签小。四列在手机上会等比缩，
    所以标签最多 4 个字（「今日应到」「在岗」「出差」「请假」）。"""
    return {
        "tag": "column",
        "width": "weighted",
        "weight": 1,
        "vertical_align": "top",
        # 参考卡片把统计数字放在浅色圆角块里，而不是裸数字漂浮在空白上
        "background_style": "grey-50",
        "padding": "10px 4px 10px 4px",
        "elements": [
            {"tag": "markdown", "text_align": "center", "text_size": "heading-0",
             "content": f"<font color='{color}'>{value}</font>"},
            {"tag": "markdown", "text_align": "center", "text_size": "notation",
             "content": f"<font color='grey'>{label}</font>"},
        ],
    }


def _cell_row(left, right, right_color=None, weight_left=3, weight_right=2):
    """模块内的一行：左边姓名加粗，右边地点/数量右对齐。

    为什么不用「姓名 · 地点、姓名 · 地点」这种拼串：
        纯文本里左右对齐只能靠全角空格凑，换个字体就散架，
        而且三行以上读的人要自己找分隔符。分栏是让组件做对齐。
    """
    right_md = f"<font color='{right_color}'>{right}</font>" if right_color else right
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "small",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": weight_left,
             "vertical_align": "center",
             "elements": [{"tag": "markdown", "content": f"**{left}**"}]},
            {"tag": "column", "width": "weighted", "weight": weight_right,
             "vertical_align": "center", "horizontal_align": "right",
             # text_align 才是让文字贴右的属性；horizontal_align 只对齐子元素盒子
             "elements": [{"tag": "markdown", "text_align": "right",
                           "content": right_md}]},
        ],
    }


def _module(title, badge, color, rows, tint=None):
    """一个业务模块：浅色底容器 + 标题行（左标题 / 右徽标）+ 多行明细。

    每个模块自己是独立容器（column 带 background_style），
    这样四块在视觉上互相分开，而不是四行连着的文字。
    """
    head = {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "small",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 3, "vertical_align": "center",
             "elements": [{"tag": "markdown",
                           "content": f"<font color='{color}'>**{title}**</font>"}]},
            {"tag": "column", "width": "weighted", "weight": 2, "vertical_align": "center",
             "horizontal_align": "right",
             "elements": [{"tag": "markdown", "text_align": "right",
                           "content": f"<text_tag color='{color}'>{badge}</text_tag>"}]},
        ],
    }
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            # 只用极浅灰：飞书的 background_style 没有 50 号浅色，
            # 深橙/深绿铺满既刺眼又会让同色标题隐身
            "background_style": tint or "grey-50",
            "padding": "10px 12px 10px 12px",
            "elements": [head, {"tag": "hr"}] + list(rows),
        }],
    }


def _overdue_module(d):
    """逾期提醒单独一块：左侧标题 + 负责人，右侧超大红色数字。

    不做成和「在办事项」一样的列表——同一批人在两块里各出现一次，
    看起来像数据重复；这里只回答「今天有几件逾期、谁背的」。
    """
    people = [(n, v) for _doing, v, n in d["busy"] if v]
    pad = "10px 12px 10px 12px"

    if not people:
        return {"tag": "column_set", "flex_mode": "none", "columns": [{
            "tag": "column", "width": "weighted", "weight": 1,
            "background_style": "green-50", "padding": "8px 12px 8px 12px",
            "elements": [{"tag": "markdown", "text_size": "notation",
                          "content": "<font color='grey'>逾期提醒　暂无逾期事项</font>"}],
        }]}

    names = "、".join(_esc(n, 10) for n, _v in people[:3])
    more = f" 等 {len(people)} 人" if len(people) > 3 else ""
    return {"tag": "column_set", "flex_mode": "none", "columns": [{
        "tag": "column", "width": "weighted", "weight": 1,
        "background_style": "red-50", "padding": pad,
        "elements": [{
            "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "small",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 3, "vertical_align": "center",
                 "elements": [{"tag": "markdown", "content": chr(10).join([
                     "<font color='red'>**逾期提醒**</font>",
                     f"<font color='grey'>负责人：{names}{more}</font>",
                 ])}]},
                {"tag": "column", "width": "weighted", "weight": 2, "vertical_align": "center",
                 "horizontal_align": "right",
                 "elements": [{"tag": "markdown", "text_align": "right",
                               "text_size": "heading-0",
                               "content": (f"<font color='red'>{d['overdue_total']}</font>"
                                           f"<font color='red' size='12'> 件</font>")}]},
            ],
        }],
    }]}


def _modules(d):
    """四个业务模块：出差 / 请假 / 在办 / 逾期。没有内容的模块整块不出现
    （逾期例外：零逾期时给一句浅灰提示，避免让人猜「是不是漏了」）。"""
    out = []
    if d["away_list"]:
        rows = [_cell_row(_esc(n, 12), f"<font color='grey'>{_esc(loc, 24) or '外出'}</font>")
                for n, loc in d["away_list"][:8]]
        out.append(_module("出差人员", f"{d['away']} 人", "orange", rows, tint="orange-50"))

    if d["leave_list"]:
        rows = [_cell_row(_esc(n, 12), f"<font color='grey'>{_esc(loc, 24) or '请假'}</font>")
                for n, loc in d["leave_list"][:8]]
        out.append(_module("请假人员", f"{d['leave']} 人", "green", rows, tint="green-50"))

    if d["busy"]:
        total = sum(x[0] for x in d["busy"])
        rows = [_cell_row(_esc(name, 12), f"**{doing} 件**")
                for doing, _ov, name in d["busy"][:8]]
        out.append(_module("在办事项", f"{total} 件", "blue", rows, tint="blue-50"))

    out.append(_overdue_module(d))
    return out


def build(day=None, now=None, data=None):
    """构造卡片：四列人数 → 明细 → 在办 → 入口按钮。

    data 可注入预先收集好的统计（预览不同状态、写测试用），
    不传则现场读库。
    

    刻意不放「数据更新于 xx:xx · 口径…」那种脚注：
    群里没人靠它做判断，占一行反而把正文挤掉。时间放在副标题就够。
    """
    d = data or collect(day, now)
    status = _status_text(d)
    subtitle = f"{d['date']} {d['weekday']} · 更新 {d['updated']}"
    if status:
        subtitle += f" · {status}"

    elements = [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "horizontal_spacing": "default",
            "columns": [
                _stat(d["total"], "今日应到", "blue"),
                _stat(d["normal"], "在岗", "green"),
                _stat(d["away"], "出差", "orange"),
                _stat(d["leave"], "请假", "red"),
            ],
        },
        {"tag": "hr"},
    ]

    if not d["synced"]:
        elements.append({"tag": "markdown",
                         "content": "还没有同步到成员数据，请检查服务是否正常。",
                         "text_size": "notation"})
    else:
        elements += _modules(d)

    elements.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "打开运营部在线看板"},
        "type": "primary_filled",
        "width": "fill",
        "url": BOARD_URL,
    })

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


def build_text(day=None, now=None, data=None):
    """纯文本兜底：卡片发送失败时至少让群里看到同样的话。"""
    d = data or collect(day, now)
    head = f"【{d['department']} · 今日状态】"
    lines = [head] + legacy_lines(d)
    status = _status_text(d)
    if status:
        lines.append(status)
    return "\n".join(lines + [BOARD_URL])
