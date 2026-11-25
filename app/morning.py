# -*- coding: utf-8 -*-
"""晨间统一采集（MorningSnapshot）：由个人待核验事件驱动的每日一次全量同步。

为什么不再用固定 9:00：
    目标打卡时间是 08:40–08:45，但真实记录落地有延迟。固定 9 点既可能早于
    记录落地（发出去的数据是空的），也可能晚于同事开始问「今天怎么没发」。
    改成「收到第一台上报 → 算一个安全时刻 → 采集完立刻发」，比定时更贴合事实。

时间语义（Asia/Shanghai，全部用本机时区）：
    target = max(当天 target_time(默认 08:50), 事件时间 + 2 分钟)
    → 08:40 上报 → 08:50；08:48 上报 → 08:50；08:55 上报 → 08:57
    这样「早到」不会提前跑（记录还没落地），「晚到」也不会被漏掉。

幂等边界：
    同一 business_date 只有一条 morning_runs（主键约束），
    日报是否已发也记在这条记录上——重复触发不会二次发群。
"""
import threading
import time
from datetime import datetime, timedelta

from . import attendance, config, db, digest

DEFAULT_TARGET = "08:50"
# 记录延迟重试：只在这里列出的时刻重跑同一 run，绝不无限轮询
RETRY_MINUTES = (0, 5, 15)
# 事件时间之后再留 2 分钟，避开「任务刚创建、记录还没写进考勤系统」
EVENT_GRACE_S = 120
# 过了这个点仍未成功，就定性为结果未知，停止轮询
GIVE_UP_AFTER_MIN = 45

_lock = threading.Lock()


# ------------------------------------------------------------------ 时间
def _parse_hhmm(text, default):
    try:
        h, m = str(text or default).split(":")
        return int(h), int(m)
    except Exception:
        h, m = str(default).split(":")
        return int(h), int(m)


def target_at(now, trigger_at=None):
    """算出本次统采应该跑的时刻（epoch 秒）。"""
    conf = config.load()
    h, m = _parse_hhmm(conf.get("morning_target") or DEFAULT_TARGET, DEFAULT_TARGET)
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    ts = base.timestamp()
    if trigger_at:
        ts = max(ts, float(trigger_at) + EVENT_GRACE_S)
    return int(ts)


# ------------------------------------------------------------------ run 表
def get_run(business_date):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM morning_runs WHERE business_date=?",
                           (business_date,)).fetchone()
    return dict(row) if row else None


def plan(trigger_source="checkin_unknown", now=None):
    """登记今天的统采任务。已存在就原样返回（同一日期只允许一条）。"""
    now = now or datetime.now()
    day = attendance.today(now)
    existing = get_run(day)
    if existing:
        return existing

    run_id = "mr-" + day.replace("-", "") + "-" + str(int(time.time()))
    scheduled = target_at(now, now.timestamp())
    try:
        with db.session() as conn:
            conn.execute("""
                INSERT INTO morning_runs
                    (business_date, run_id, trigger_source, trigger_at, scheduled_at,
                     attempt, attendance_sync_status, board_sync_status,
                     digest_status, quicklaunch_status)
                VALUES (?,?,?,?,?,0,'pending','pending','pending','pending')
            """, (day, run_id, trigger_source, int(now.timestamp()), scheduled))
    except Exception:
        # 并发下另一条线程已经插入：以库里那条为准
        pass
    return get_run(day)


def _update(day, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.session() as conn:
        conn.execute(f"UPDATE morning_runs SET {cols} WHERE business_date=?",
                     list(fields.values()) + [day])


# ------------------------------------------------------------------ 执行
def _next_attempt_at(run):
    idx = min(int(run.get("attempt") or 0), len(RETRY_MINUTES) - 1)
    return int(run["scheduled_at"]) + RETRY_MINUTES[idx] * 60


def due(now=None):
    """该不该现在跑：有 run、没完成、到了下一次重试时刻。"""
    now = now or datetime.now()
    day = attendance.today(now)
    run = get_run(day)
    if not run:
        return None
    if run.get("completed_at") and run.get("digest_status") in ("sent", "skipped"):
        return None
    attempt = int(run.get("attempt") or 0)
    if attempt >= len(RETRY_MINUTES):
        return None
    if now.timestamp() < _next_attempt_at(run):
        return None
    if now.timestamp() > int(run["scheduled_at"]) + GIVE_UP_AFTER_MIN * 60:
        return None
    return run


def run_once(run, now=None):
    """执行一次统采：人 → 审批 → 任务 → 表格 → 群待办 → 考勤 → 状态 → 日报。

    顺序与 api.sync_all 完全一致，因为 status.apply_all() 依赖前面全部结果。
    """
    now = now or datetime.now()
    day = run["business_date"]
    attempt = int(run.get("attempt") or 0) + 1
    # 延迟导入：api 模块在本模块之上被导入，顶层反向 import 会拿到半初始化的模块
    from . import api as api_mod

    with _lock:
        _update(day, attempt=attempt, started_at=int(now.timestamp()))

        try:
            result = api_mod.sync_all(with_attendance=True, run_id=run["run_id"], day=day)
        except Exception as e:                       # 调度线程不能死
            db.log(False, "scheduler", repr(e)[:300])
            _update(day, board_sync_status="failed", note=repr(e)[:200])
            return {"ok": False, "error": repr(e)[:200]}

        att = result.get("attendance") or {}
        att_status = att.get("status") or "failed"
        _update(day,
                attendance_sync_status=att_status,
                board_sync_status="ok",
                quicklaunch_status="ready" if att.get("ok") else "pending")

        # 数据不完整就不发摘要：宁可晚一条，也不发一条错的
        if att_status not in ("ok", "partial"):
            _update(day, completed_at=int(time.time()))
            return {"ok": False, "stage": "attendance", "attendance": att}

        sent = digest.send_once(business_date=day, run_id=run["run_id"])
        # 「跳过」和「发送成功」必须分开记：都写成 sent，事后查「今天到底发没发」
        # 就只能靠猜，而这是最容易被投诉的一件事。
        status = "skipped" if sent.get("skipped") else ("sent" if sent.get("ok") else "failed")
        _update(day,
                completed_at=int(time.time()),
                digest_status=status,
                digest_sent_at=int(time.time()) if status == "sent" else None,
                note=(sent.get("skipped") or sent.get("error") or "")[:200] or None)
        return {"ok": bool(sent.get("ok")), "attendance": att, "digest": sent}


def ensure_fallback(now=None):
    """到点仍没有 run 时自建一个。

    为什么需要：run 只由设备上报事件创建。如果某天没人上报（手机没开机、
    没打卡、家里网络断了），当天就会**一条日报都没有**，而且没有任何告警——
    看板看起来正常，群里却整天没动静。日报是固定节奏，不能依赖外部事件。
    """
    now = now or datetime.now()
    day = attendance.today(now)
    if get_run(day):
        return None
    h, m = _parse_hhmm(config.load().get("morning_target") or DEFAULT_TARGET, DEFAULT_TARGET)
    target = datetime.combine(now.date(), datetime.min.time()).replace(hour=h, minute=m)
    if now < target:
        return None
    run = plan(trigger_source="fallback", now=now)
    db.log(True, "scheduler", f"{day} 到点无设备事件，已自建兜底统采")
    return run


def tick(now=None):
    """调度器每秒/每分钟调用：到点就跑，跑完就再也不动。

    被 tick 覆盖的三种情形：
      1) 已发送 → 直接返回；
      2) 到点未跑 → 跑；
      3) 失败且还在重试窗口 → 再跑一次（attempt 自增，最多 len(RETRY_MINUTES) 次）。
    """
    # 先兜底：没有任何 run 时自建，避免"今天没人上报所以没日报"
    ensure_fallback(now)

    run = due(now)
    if not run:
        return None
    return run_once(run, now)


def mark_given_up(now=None):
    """超过重试窗口仍无数据：定性为未知，停轮询，不发个人成功。"""
    now = now or datetime.now()
    day = attendance.today(now)
    run = get_run(day)
    if not run or run.get("completed_at"):
        return None
    if now.timestamp() <= int(run["scheduled_at"]) + GIVE_UP_AFTER_MIN * 60:
        return None
    _update(day, completed_at=int(time.time()),
            attendance_sync_status="unknown",
            digest_status="skipped",
            note="超过核验窗口仍无有效记录")
    db.log(False, "scheduler", f"{day} 统采超时，已停止轮询")
    return get_run(day)
