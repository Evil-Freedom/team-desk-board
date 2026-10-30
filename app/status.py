# -*- coding: utf-8 -*-
"""状态推导：不在岗记录 + 当前时间 → 每人今日状态。

这一层是纯计算，不访问网络、不读配置，便于单测。
审批里的"出差"和"请假"只决定「在不在岗」；
"忙不忙"由待办数量决定，属于 tasks 模块，不在这里混算。
"""
import time

from . import db

LEAVE_KINDS = {"leave"}
AWAY_KINDS = {"travel", "field"}

_SUFFIXES = ("特别行政区", "自治区", "省", "市", "区", "县", "旗")

# 自治区去掉「自治区」后仍是民族全称，压缩成日常叫法
_AUTONOMOUS = {
    "广西壮族": "广西",
    "新疆维吾尔": "新疆",
    "宁夏回族": "宁夏",
    "内蒙古": "内蒙古",
    "西藏": "西藏",
}

# 末级是这些时，退一级用所属的地级市
_DISTRICT = ("区", "县", "旗")


def _trim(name):
    for suf in _SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name


def _province(name):
    trimmed = _trim(name)
    return _AUTONOMOUS.get(trimmed, trimmed)


def short_place(raw):
    """飞书地点路径 → 「省+市」。

    飞书出差控件给的是完整区划，如 中国/浙江省/金华市/义乌市，
    看板写全称会撑爆卡片，压成「浙江义乌」既认得出又短。

    规则：
      · 末级是县级市/地级市 → 用末级    浙江省/金华市/义乌市 → 浙江义乌
      · 末级是区/县/旗       → 退一级    广东省/深圳市/南山区 → 广东深圳
      · 省市同名（直辖市）   → 只留一个  北京市/北京市/朝阳区 → 北京
    """
    if not raw or not isinstance(raw, str):
        return "外出"

    parts = [p.strip() for p in raw.split("/") if p.strip() and p.strip() != "中国"]
    if not parts:
        return "外出"

    last = parts[-1]
    go_up = last.endswith(_DISTRICT) and len(parts) >= 2
    target = parts[-2] if go_up else last

    prov = _province(parts[0]) if len(parts) >= 2 else ""
    city = _trim(target)

    if prov and prov != city:
        return prov + city
    return city or "外出"


def derive(absences, now=None):
    """单人：返回 (status, loc)。status ∈ normal / away / leave。"""
    now = now or int(time.time())
    active = [a for a in absences if a["start_ts"] <= now <= a["end_ts"]]
    if not active:
        return "normal", None

    leave = next((a for a in active if a["kind"] in LEAVE_KINDS), None)
    if leave:
        return "leave", leave.get("reason") or "请假"

    away = next((a for a in active if a["kind"] in AWAY_KINDS), None)
    if away:
        return "away", short_place(away.get("city"))

    return "normal", None


def apply_all(now=None):
    """把推导结果写回 members 表。返回变更人数，供日志用。"""
    now = now or int(time.time())
    with db.session() as conn:
        members = [dict(r) for r in conn.execute("SELECT open_id, status, loc FROM members")]
        rows = [dict(r) for r in conn.execute("SELECT * FROM absences")]

    by_person = {}
    for a in rows:
        by_person.setdefault(a["open_id"], []).append(a)

    changed = 0
    with db.session() as conn:
        for m in members:
            status, loc = derive(by_person.get(m["open_id"], []), now)
            if status != m["status"] or loc != m["loc"]:
                conn.execute("UPDATE members SET status=?, loc=?, updated_at=? WHERE open_id=?",
                             (status, loc, now, m["open_id"]))
                changed += 1
    return changed
