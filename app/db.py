# -*- coding: utf-8 -*-
"""建表与连接。表结构即数据契约，改字段先改这里。

members   人员（通讯录同步的落地表）
absences  不在岗记录（审批同步的落地表，出差/请假/外勤共用）
sync_log  同步日志，前端用它显示“数据源”与失败原因
"""
import os
import sqlite3
import time
from contextlib import contextmanager

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# VPS 上代码目录只读（root 拥有），数据库必须落在可写的数据目录，
# 所以路径允许用环境变量覆盖；本地不设变量时行为与以前完全一致。
PATH = os.environ.get("BOARD_DB") or os.path.join(BASE, "board.db")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS members (
        open_id     TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        department  TEXT,
        email       TEXT,
        scarf       TEXT,
        doing       INTEGER DEFAULT 0,
        overdue     INTEGER DEFAULT 0,
        status      TEXT DEFAULT 'normal',
        loc         TEXT,
        updated_at  INTEGER
    )""",
    """
    CREATE TABLE IF NOT EXISTS absences (
        id          TEXT PRIMARY KEY,   -- 审批实例 code
        open_id     TEXT NOT NULL,
        kind        TEXT NOT NULL,      -- travel | leave | field
        start_ts    INTEGER NOT NULL,
        end_ts      INTEGER NOT NULL,
        city        TEXT,
        reason      TEXT,
        source      TEXT DEFAULT 'approval',
        raw         TEXT,
        updated_at  INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_abs_open ON absences(open_id, start_ts, end_ts)",
    """
    CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, ok INTEGER, scope TEXT, message TEXT
    )""",
    """
    CREATE TABLE IF NOT EXISTS todos (
        id          TEXT PRIMARY KEY,
        open_id     TEXT NOT NULL,
        title       TEXT NOT NULL,
        status      TEXT DEFAULT 'open',   -- open | done
        due_ts      INTEGER,
        source      TEXT DEFAULT 'manual', -- feishu | manual
        created_at  INTEGER,
        updated_at  INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_todo_person ON todos(open_id, status)",
    """
    CREATE TABLE IF NOT EXISTS users (
        open_id           TEXT PRIMARY KEY,
        name              TEXT,
        access_token      TEXT,
        refresh_token     TEXT,
        expires_at        INTEGER,
        refresh_expires_at INTEGER,
        authorized_at     INTEGER,
        last_sync_at      INTEGER,
        scope             TEXT
    )""",
    # ── 统一考勤采集（2026-11-25 新增） ─────────────────────────────
    # 为什么单独建表而不往 members 上加列：
    #   1) members.status 的语义是「审批推导出的在不在岗」，考勤是另一条事实来源，
    #      混在一列里会让「休假但打了卡」「出差但有记录」这类正常情况无解；
    #   2) 历史考勤要按天留存，members 是每人一行。
    # 只存业务需要的字段：不落照片 URL、Wi-Fi、经纬度——那些属于个人隐私，看板用不上。
    """
    CREATE TABLE IF NOT EXISTS attendance_records (
        attendance_date     TEXT NOT NULL,          -- YYYY-MM-DD（Asia/Shanghai）
        employee_no         TEXT NOT NULL,          -- 稳定员工标识，不是姓名
        open_id             TEXT,
        check_in_record_id  TEXT,
        check_in_result     TEXT,
        check_in_time       INTEGER,
        check_out_record_id TEXT,
        check_out_result    TEXT,
        check_out_time      INTEGER,
        fetched_at          INTEGER,
        source              TEXT,                   -- bot / user / manual
        run_id              TEXT,
        PRIMARY KEY (attendance_date, employee_no)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_att_open ON attendance_records(open_id, attendance_date)",
    "CREATE INDEX IF NOT EXISTS idx_att_record ON attendance_records(check_in_record_id)",
    """
    CREATE TABLE IF NOT EXISTS attendance_sync_log (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       INTEGER,
        run_id   TEXT,
        ok       INTEGER,
        scope    TEXT,
        message  TEXT
    )""",
    # 同一业务日期只允许一条 run：单日一次成功推送靠 PRIMARY KEY 兜底，
    # 不依赖「先查再插」这种竞态写法。
    """
    CREATE TABLE IF NOT EXISTS morning_runs (
        business_date         TEXT PRIMARY KEY,
        run_id                TEXT NOT NULL UNIQUE,
        trigger_source        TEXT,
        trigger_at            INTEGER,   -- 收到待核验任务的时间
        scheduled_at          INTEGER,   -- 动态算出的统一采集时刻
        started_at            INTEGER,
        completed_at          INTEGER,
        attempt               INTEGER DEFAULT 0,
        attendance_sync_status TEXT,     -- ok | partial | failed | skipped
        board_sync_status      TEXT,
        digest_status          TEXT,     -- pending | sent | failed | skipped
        quicklaunch_status     TEXT,
        digest_sent_at         INTEGER,
        note                   TEXT
    )""",
    # open_id ↔ employee_no 映射。当前应用缺 contact:user.employee_id:readonly，
    # 这张表暂时为空、采集自动降级为「本人」。权限批下来后由 sync_identity() 自动回填。
    """
    CREATE TABLE IF NOT EXISTS attendance_identity (
        open_id     TEXT PRIMARY KEY,
        employee_no TEXT NOT NULL,
        source      TEXT,
        updated_at  INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_att_identity_no ON attendance_identity(employee_no)",
    # 一次性公告：主键即幂等键。宁可多发不出去，也不能发两遍。
    """
    CREATE TABLE IF NOT EXISTS announcements (
        announcement_key TEXT PRIMARY KEY,
        status           TEXT NOT NULL,   -- PENDING | SENDING | SENT | FAILED
        message_id       TEXT,
        target_chat_id   TEXT,
        body_version     TEXT,
        created_at       INTEGER,
        sent_at          INTEGER,
        note             TEXT
    )""",
]


def connect():
    conn = sqlite3.connect(PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session():
    """事务上下文：正常提交、异常回滚、无论如何关闭连接。
    注意 with sqlite3.connect() 只提交事务不关连接，会泄漏 fd。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with session() as conn:
        for sql in SCHEMA:
            conn.execute(sql)
        _migrate(conn)


def _migrate(conn):
    """旧库补列：CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sync_log)")}
    if "scope" not in cols:
        conn.execute("ALTER TABLE sync_log ADD COLUMN scope TEXT")
    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "scope" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN scope TEXT")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
    for col, ddl in (("loc", "TEXT"), ("status", "TEXT DEFAULT 'normal'")):
        if col not in mcols:
            conn.execute(f"ALTER TABLE members ADD COLUMN {col} {ddl}")


def log(ok, scope, message):
    with session() as conn:
        conn.execute(
            "INSERT INTO sync_log (ts, ok, scope, message) VALUES (?,?,?,?)",
            (int(time.time()), 1 if ok else 0, scope, str(message)[:500]),
        )


def last_log(scope=None):
    sql = "SELECT ts, ok, scope, message FROM sync_log"
    args = []
    if scope:
        sql += " WHERE scope=?"
        args.append(scope)
    sql += " ORDER BY id DESC LIMIT 1"
    with session() as conn:
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None
