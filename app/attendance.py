# -*- coding: utf-8 -*-
"""统一考勤采集：一次取数，多业务消费。

为什么单独成模块，而不是塞进 approvals / tasks：
    考勤和审批是**两条独立事实**。审批说「这个人今天请假」，考勤说「这个人今天
    打了卡」；两者可以同时成立（半天假），也可以都不成立（旷工）。
    混进 members.status 会让所有组合失效，所以这里只往 attendance_records 写。

取数策略（按当前真实权限，不是设想）：
    1) 有 open_id → employee_no 映射时，走应用身份按工号批量查（真正的一次全员）;
    2) 没有映射时，走用户身份查授权者本人（飞书只返回他自己）。
    第 2 种是降级，不是全员采集 —— 界面上必须如实显示，不能把一个人的记录
    冒充成部门数据。

    ⚠ 全员化只差一个权限：contact:user.employee_id:readonly（读员工工号）。
      应用拿到后 sync_identity() 自动回填映射，再同步即切到第 1 种，
      业务代码零改动。

为什么状态里没有「旷工」的自动推断：
    查不到记录只能说明「没采到」，不能说明「没上班」——打卡机延迟、跨天班、
    外出打卡都可能让记录晚到。ABSENT_CONFIRMED 只在飞书明确返回 Absent 时才给。
"""
import time
from datetime import date, datetime, timedelta

from . import config, db, feishu

# 与 QuickLaunch 侧的判据保持同一套口径，避免两个服务对同一条记录得出不同结论
BAD_RESULTS = {"Todo", "Absent", "NoNeedCheck", ""}

CHECKED_IN = "CHECKED_IN"
NOT_YET = "NOT_YET"
CHECKIN_PENDING = "CHECKIN_PENDING"
CHECKIN_UNKNOWN = "CHECKIN_UNKNOWN"
ABSENT_CONFIRMED = "ABSENT_CONFIRMED"
NOT_APPLICABLE = "NOT_APPLICABLE"

STATUS_TEXT = {
    CHECKED_IN: "已打卡",
    NOT_YET: "待打卡",
    CHECKIN_PENDING: "待同步",
    CHECKIN_UNKNOWN: "结果未知",
    ABSENT_CONFIRMED: "记录缺勤",
    NOT_APPLICABLE: "今日不考核",
}


# ------------------------------------------------------------------ 时间
def today(now=None):
    """业务日期一律按本机时区（生产机器已设 Asia/Shanghai）。"""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _ts(value):
    """飞书打卡时间返回的是字符串秒戳，个别版本给毫秒，统一成秒。"""
    if value in (None, ""):
        return None
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v // 1000 if v > 10_000_000_000 else v


# ------------------------------------------------------------------ 解析
def parse_user_tasks(payload):
    """把 user_task_results 摊平成记录列表。

    只取业务需要的字段：record_id、result、check_time。
    照片 URL / Wi-Fi / 经纬度一概不落库（隐私，看板也用不上）。
    """
    rows = []
    for entry in (payload or {}).get("user_task_results") or []:
        name = entry.get("employee_name") or ""
        for rec in entry.get("records") or []:
            cin = rec.get("check_in_record") or {}
            cout = rec.get("check_out_record") or {}
            employee_no = (cin.get("user_id") or cout.get("user_id")
                           or rec.get("employee_no") or "")
            if not employee_no:
                continue
            rows.append({
                "employee_no": str(employee_no),
                "employee_name": name,
                "check_in_record_id": rec.get("check_in_record_id") or cin.get("record_id") or "",
                "check_in_result": rec.get("check_in_result") or cin.get("check_result") or "",
                "check_in_time": _ts(cin.get("check_time")),
                "check_out_record_id": rec.get("check_out_record_id") or cout.get("record_id") or "",
                "check_out_result": rec.get("check_out_result") or cout.get("check_result") or "",
                "check_out_time": _ts(cout.get("check_time")),
            })
    return rows


def classify(rec, day_status="normal"):
    """单条记录 → 前端状态。rec 为 None 表示今天还没采到任何记录。

    day_status 来自审批（normal/away/leave）：请假/出差当天本来就不该打卡，
    标成「今日不考核」，而不是让卡片显示「待打卡」吓人一跳。
    """
    if day_status in ("away", "leave"):
        return NOT_APPLICABLE
    if not rec:
        return CHECKIN_PENDING
    result = (rec.get("check_in_result") or "").strip()
    if result == "Absent":
        return ABSENT_CONFIRMED
    if result == "Todo":
        return NOT_YET
    if rec.get("check_in_record_id") and rec.get("check_in_time"):
        return CHECKED_IN
    return CHECKIN_UNKNOWN


# ------------------------------------------------------------------ 映射
def identity_map():
    """open_id → employee_no。空表代表权限未到位（不是数据错误）。"""
    with db.session() as conn:
        rows = conn.execute("SELECT open_id, employee_no FROM attendance_identity").fetchall()
    return {r["open_id"]: r["employee_no"] for r in rows}


def save_identity(open_id, employee_no, source="boot"):
    if not open_id or not employee_no:
        return False
    with db.session() as conn:
        conn.execute("""
            INSERT INTO attendance_identity (open_id, employee_no, source, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(open_id) DO UPDATE SET
                employee_no=excluded.employee_no,
                source=excluded.source,
                updated_at=excluded.updated_at
        """, (open_id, employee_no, source, int(time.time())))
    return True


def sync_identity():
    """尝试用通讯录补全 open_id → employee_no。

    需要应用具备 contact:user.employee_id:readonly。没有权限时接口只回
    name/open_id，本函数返回 missing 列表而不是报错——这是「还没批」而不是「坏了」。
    """
    with db.session() as conn:
        members = [dict(r) for r in conn.execute("SELECT open_id, name FROM members")]
    if not members:
        return {"ok": False, "missing": [], "error": "members 为空"}

    filled, missing, err = 0, [], None
    for m in members:
        data, e = feishu.app(f"/open-apis/contact/v3/users/{m['open_id']}",
                             params={"user_id_type": "user_id"})
        if e:
            err = e
            missing.append(m["open_id"])
            continue
        user = (data or {}).get("user") or {}
        no = user.get("employee_no") or user.get("user_id")
        if no:
            save_identity(m["open_id"], str(no), "contact")
            filled += 1
        else:
            missing.append(m["open_id"])
    return {"ok": filled > 0, "filled": filled, "missing": missing,
            "error": err or ("读不到工号：应用缺 contact:user.employee_id:readonly")}


# ------------------------------------------------------------------ 写库
def _upsert(row, day, run_id, source):
    """(日期, 工号) 是主键：同一天重复采集只更新，不产生第二行。"""
    with db.session() as conn:
        conn.execute("""
            INSERT INTO attendance_records
                (attendance_date, employee_no, open_id, check_in_record_id, check_in_result,
                 check_in_time, check_out_record_id, check_out_result, check_out_time,
                 fetched_at, source, run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(attendance_date, employee_no) DO UPDATE SET
                open_id=COALESCE(excluded.open_id, attendance_records.open_id),
                check_in_record_id=excluded.check_in_record_id,
                check_in_result=excluded.check_in_result,
                check_in_time=excluded.check_in_time,
                check_out_record_id=excluded.check_out_record_id,
                check_out_result=excluded.check_out_result,
                check_out_time=excluded.check_out_time,
                fetched_at=excluded.fetched_at,
                source=excluded.source,
                run_id=excluded.run_id
        """, (day, row["employee_no"], row.get("open_id"), row["check_in_record_id"],
              row["check_in_result"], row["check_in_time"], row["check_out_record_id"],
              row["check_out_result"], row["check_out_time"], int(time.time()),
              source, run_id))


def _log(ok, scope, message, run_id=None):
    with db.session() as conn:
        conn.execute(
            "INSERT INTO attendance_sync_log (ts, run_id, ok, scope, message) VALUES (?,?,?,?,?)",
            (int(time.time()), run_id, 1 if ok else 0, scope, str(message)[:500]))
    db.log(ok, scope, message)


# ------------------------------------------------------------------ 采集
def _collect_per_user(day):
    """逐人授权采集：每人用自己的 token 查自己。

    为什么这条路径重要：
        它不需要管理员批任何企业级权限——每位成员在看板点过一次授权就同意了。
        代价是覆盖率等于「已授权人数」，所以调用方必须如实展示范围，
        不能拿它冒充全员。
    """
    from . import oauth

    conf = config.load()
    app_id, secret = conf.get("app_id"), conf.get("app_secret")
    if not app_id or not secret:
        return [], ["缺少应用凭证，无法刷新成员 token"], 0

    rows, errors, tried = [], [], 0
    for u in oauth.list_users():
        token, err = oauth.get_valid_token(u["open_id"], app_id, secret)
        if err:
            errors.append(f"{u.get('name') or u['open_id'][:8]}: {err}")
            continue
        tried += 1
        data, e = feishu.attendance_by_user(token, day)
        if e:
            # 最常见的是该成员授权时没有 attendance:task:readonly，
            # 这不是故障，只是他还没同意这一项，如实记下来即可
            errors.append(f"{u.get('name') or u['open_id'][:8]}: {e}")
            continue
        for r in parse_user_tasks(data):
            r["open_id"] = u["open_id"]
            save_identity(u["open_id"], r["employee_no"], "user")
            rows.append(r)
    return rows, errors, tried


def sync(day=None, run_id=None, now=None):
    """采集指定业务日期的考勤。mode 必须如实反映用的是哪条路径。"""
    day = day or today(now)
    mapping = identity_map()

    if mapping:
        nos = sorted({v for v in mapping.values() if v})
        data, err = feishu.attendance_by_app(nos, day)
        if err:
            _log(False, "attendance", f"team 取数失败：{err}", run_id)
            return {"ok": False, "mode": "team", "error": err, "count": 0,
                    "status": "failed", "errors": [err]}
        rows = parse_user_tasks(data)
        rev = {}
        for o, no in mapping.items():
            rev.setdefault(no, o)
        for r in rows:
            r["open_id"] = rev.get(r["employee_no"])
        mode, errors, tried = "team", [], len(nos)
    else:
        rows, errors, tried = _collect_per_user(day)
        mode = "per_user"
        if tried == 0:
            msg = errors[0] if errors else "没有已授权成员"
            _log(False, "attendance", f"per_user 取数失败：{msg}", run_id)
            return {"ok": False, "mode": mode, "error": msg, "count": 0,
                    "status": "failed", "errors": errors}

    with db.session() as conn:
        existing = {r["employee_no"] for r in conn.execute(
            "SELECT employee_no FROM attendance_records WHERE attendance_date=?", (day,))}

    for r in rows:
        _upsert(r, day, run_id, mode)

    new = len([r for r in rows if r["employee_no"] not in existing])
    # 有人被查询但当天没有任何记录，仍然算「采到了」——空结果也是结果
    status = "ok" if rows else "partial"
    msg = f"{mode} 取到 {len(rows)} 条（新增 {new}，覆盖 {tried} 人）"
    if mode == "per_user":
        msg += "；未拿到全员工号，覆盖率=已授权成员"
    if errors:
        msg += f"；{len(errors)} 人失败：{errors[0][:80]}"
    _log(True, "attendance", msg, run_id)
    return {"ok": True, "mode": mode, "count": len(rows), "new": new,
            "status": status, "error": None, "rows": rows,
            "errors": errors, "tried": tried}


# ------------------------------------------------------------------ 查询
def records_for_day(day=None):
    """{open_id: record} 供看板一次性取用。同一天同一个人可能多工号，取最新。"""
    day = day or today()
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM attendance_records WHERE attendance_date=? "
            "ORDER BY fetched_at ASC", (day,))]
    out = {}
    for r in rows:
        key = r.get("open_id") or ("emp:" + r["employee_no"])
        out[key] = r
    return out


def find_self(employee_no=None, open_id=None, day=None):
    """内部接口用：按工号或 open_id 精确取一条，不做模糊匹配。"""
    day = day or today()
    with db.session() as conn:
        if employee_no:
            row = conn.execute(
                "SELECT * FROM attendance_records WHERE attendance_date=? AND employee_no=?",
                (day, employee_no)).fetchone()
        elif open_id:
            row = conn.execute(
                "SELECT * FROM attendance_records WHERE attendance_date=? AND open_id=? "
                "ORDER BY fetched_at DESC LIMIT 1", (day, open_id)).fetchone()
        else:
            return None
    return dict(row) if row else None


def current_mode(rows=None):
    """当前用的是「全员批量」还是「逐人授权」。前端要据此如实标注覆盖范围。"""
    rows = records_for_day() if rows is None else rows
    src = {r.get("source") for r in (rows.values() if isinstance(rows, dict) else rows)}
    src.discard(None)
    if src == {"team"}:
        return "team"
    if src:
        return "per_user"
    log = last_log() or {}
    msg = log.get("message") or ""
    for m in ("team", "per_user"):
        if msg.startswith(m):
            return m
    return "none"


def summary(day=None):
    """日报用的一句话汇总：只在真有数据时才给结论。"""
    day = day or today()
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM attendance_records WHERE attendance_date=?", (day,))]
        members = [dict(r) for r in conn.execute("SELECT open_id, name, status FROM members")]
    if not rows:
        return None
    by_open = {}
    for r in rows:
        if r.get("open_id"):
            by_open[r["open_id"]] = r
    checked = sum(1 for r in by_open.values() if classify(r) == CHECKED_IN)
    total = len(members) if members else len(by_open)
    # 覆盖率必须按「谁被查了」算：per_user 模式下 total 是全部门，
    # 直接用会得到「已打卡 1/11」这种看着像全员缺勤的错误结论。
    scoped = len(by_open) if by_open else len(rows)
    # 关键区分：mode=team 说的是「取数方式」，不等于「覆盖了全员」。
    # 工号映射只有 1 条时，team 模式也只能采到 1 个人。
    return {"checked": checked, "scoped": scoped, "total": total,
            "coverage": current_mode(records_for_day(day)),
            "full": scoped >= total}


def last_log():
    with db.session() as conn:
        row = conn.execute(
            "SELECT ts, ok, scope, message FROM attendance_sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
