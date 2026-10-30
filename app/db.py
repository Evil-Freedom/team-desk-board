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
PATH = os.path.join(BASE, "board.db")

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
