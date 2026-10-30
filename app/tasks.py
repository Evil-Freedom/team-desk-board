# -*- coding: utf-8 -*-
"""待办：飞书任务自动同步 + 看板内手工补录。

两条来源合并成一张 todos 表，看板只认这张表：
    source=feishu  每次同步整批覆盖（任务被删/完成，这里跟着消失）
    source=manual  长期保留，只由人自己勾完成或删除

为什么不把飞书任务和手工记录分两张表：
    看板只需要「这个人手上还有几件事」，来源对显示没意义；
    合表后统计只有一条 SQL，也不用处理两边重复计数。

每天变化怎么体现：
    飞书任务每轮同步都会刷新（完成即消失）；
    手工记录不自动过期，靠人每天勾掉——这是「每天维护」的落点。
"""
import time
import uuid

from . import db, feishu

OPEN, DONE = "open", "done"


# ------------------------------------------------------------------ 飞书任务
def _list_feishu_tasks(token):
    """拉当前用户未完成的飞书任务。v2 不可用时回退 v1。"""
    for path, params in (
        ("/open-apis/task/v2/tasks", {"page_size": 100, "completed": "false", "user_id_type": "open_id"}),
        ("/open-apis/task/v1/tasks", {"page_size": 100, "completed": "false"}),
    ):
        data, err = feishu.get_with_token(token, path, params)
        if err:
            continue
        items = data.get("items")
        if items is not None:
            return items, None
    return None, "飞书任务接口不可用"


def _to_ts(due):
    """飞书任务的截止时间可能是对象、毫秒戳或数字字符串，统一成秒。"""
    raw = due.get("timestamp") if isinstance(due, dict) else due
    if raw in (None, ""):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v // 1000 if v > 10_000_000_000 else v


def sync_feishu_tasks(open_id, token):
    """整批覆盖该用户的飞书任务。返回同步条数。"""
    items, err = _list_feishu_tasks(token)
    if err:
        return 0, err

    now = int(time.time())
    rows = []
    for t in items:
        tid = t.get("guid") or t.get("id") or t.get("task_id")
        if not tid:
            continue
        due_ts = _to_ts(t.get("due"))
        rows.append((f"fs_{tid}", open_id, t.get("summary") or t.get("title") or "未命名任务", due_ts, now))

    with db.session() as conn:
        # 先清掉旧的飞书任务，再整批写入：任务被删或完成，这里自然消失
        conn.execute("DELETE FROM todos WHERE open_id=? AND source='feishu'", (open_id,))
        for r in rows:
            conn.execute("""
                INSERT INTO todos (id, open_id, title, status, due_ts, source, created_at, updated_at)
                VALUES (?,?,?, 'open', ?, 'feishu', ?, ?)
            """, (r[0], r[1], r[2], r[3], r[4], r[4]))
    return len(rows), None


# ------------------------------------------------------------------ 手工补录
def add_manual(open_id, title, due_ts=None):
    if not open_id or not (title or "").strip():
        return None, "缺少 open_id 或标题"
    now = int(time.time())
    tid = "mn_" + uuid.uuid4().hex[:12]
    with db.session() as conn:
        conn.execute("""
            INSERT INTO todos (id, open_id, title, status, due_ts, source, created_at, updated_at)
            VALUES (?,?,?, 'open', ?, 'manual', ?, ?)
        """, (tid, open_id, title.strip()[:200], due_ts, now, now))
    return tid, None


def toggle(todo_id, status=None):
    """切换完成状态，返回 (是否成功, 新状态)。

    返回新状态是为了让调用方能把它写回来源系统——
    多维表格来源的待办如果不写回，下一轮同步会把状态覆盖回去。
    """
    with db.session() as conn:
        row = conn.execute("SELECT status FROM todos WHERE id=?", (todo_id,)).fetchone()
        if not row:
            return False, None
        new = status if status in (OPEN, DONE) else (DONE if row["status"] == OPEN else OPEN)
        conn.execute("UPDATE todos SET status=?, updated_at=? WHERE id=?",
                     (new, int(time.time()), todo_id))
    return True, new


def remove(todo_id):
    with db.session() as conn:
        conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    return True


def owner_of(todo_id):
    """这条待办属于谁。用于「能不能改」的权限判断，找不到返回 None。"""
    if not todo_id:
        return None
    with db.session() as conn:
        row = conn.execute("SELECT open_id FROM todos WHERE id=?", (todo_id,)).fetchone()
    return row["open_id"] if row else None


def list_by_person(open_id):
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM todos WHERE open_id=? ORDER BY status, due_ts IS NULL, due_ts",
            (open_id,))]
    return rows


# ------------------------------------------------------------------ 统计
def summary_by_person():
    """返回 {open_id: {"doing": n, "overdue": n}}，供看板一次性取用。"""
    now = int(time.time())
    out = {}
    with db.session() as conn:
        rows = conn.execute("""
            SELECT open_id,
                   SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS doing,
                   SUM(CASE WHEN status='open' AND due_ts IS NOT NULL AND due_ts < ? THEN 1 ELSE 0 END) AS overdue
            FROM todos GROUP BY open_id
        """, (now,)).fetchall()
    for r in rows:
        out[r["open_id"]] = {"doing": r["doing"] or 0, "overdue": r["overdue"] or 0}
    return out


def sync_all_authorized():
    """遍历已授权成员，各拉一次飞书任务。调度器每轮调用。"""
    from . import config, oauth

    conf = config.load()
    app_id, secret = conf.get("app_id"), conf.get("app_secret")
    if not app_id or not secret:
        return {"ok": False, "error": "缺少应用凭证"}

    total, fresh, errors = 0, 0, []
    for u in oauth.list_users():
        token, err = oauth.get_valid_token(u["open_id"], app_id, secret)
        if err:
            errors.append(f"{u.get('name') or u['open_id']}: {err}")
            continue
        total += 1
        n, err = sync_feishu_tasks(u["open_id"], token)
        if err:
            errors.append(f"{u.get('name') or u['open_id']}: {err}")
        else:
            fresh += n

    return {"ok": not errors, "users": total, "tasks": fresh, "errors": errors}
