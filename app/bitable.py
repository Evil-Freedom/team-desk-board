# -*- coding: utf-8 -*-
"""多维表格待办同步。

为什么单独一个源头：
    飞书任务只能读「本人名下」的，负责人看不到全貌；
    多维表格是一张全员共用的表，负责人能一眼看全部、能排序、能替人改。
    两者互补，所以不是替换关系。

数据流：
    飞书多维表格（人在里面改） → 每轮同步整批覆盖 → todos 表
    看板只读不改写，避免两边同时写造成冲突。

列名不写死：
    按「事项/标题」「负责人/执行人」「状态」「截止日期」等多组别名匹配，
    改表头不影响同步，只要语义对得上。
"""
import json
import time
from datetime import datetime

from . import config, db, feishu

OPEN, DONE = "open", "done"

# 这些状态视为已完成；其余一律算未完成
DONE_WORDS = {"已完成", "完成", "已结束", "已归档", "done", "Done", "DONE", "closed"}

TITLE_KEYS = ("事项", "任务", "内容", "标题", "title", "Title", "Name", "name")
OWNER_KEYS = ("负责人", "执行人", "指派给", "owner", "Owner", "assignee")
STATUS_KEYS = ("状态", "进度", "status", "Status")
DUE_KEYS = ("截止日期", "截止时间", "截止", "deadline", "Deadline", "due", "Due")


def _to_ts(raw):
    """多维表格的日期列返回 ISO 字符串（带时区），统一成秒时间戳。"""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v // 1000 if v > 10_000_000_000 else v
    try:
        s = str(raw).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def _cell(row, fields, keys):
    """按列名取值。

    返回的是矩阵（每行一个数组），列顺序由 fields 决定，
    所以必须按名字查下标，不能假设固定位置。
    """
    for k in keys:
        if k in fields:
            i = fields.index(k)
            if i < len(row):
                return row[i]
    return None


def _text(v):
    """把单元格值压成字符串。

    单选/多选返回数组，人员返回 [{id,name}]，文本返回 str，
    这里统一处理，调用方不用关心列类型。
    """
    if v is None:
        return ""
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return v[0].get("name") or ""
        return "".join(str(x) for x in v)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or ""
    return str(v)


def _person_id(v):
    """人员列直接给 open_id，正好和 members 表对得上。"""
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0].get("id") or ""
    if isinstance(v, dict):
        return v.get("id") or ""
    return ""


def update_status(todo_id, status):
    """把完成状态写回多维表格。

    为什么必须写回：
        sync_records 是整批覆盖，看板上勾一下只改本地，
        下一轮同步会把它改回未完成——用户会觉得「勾了没用」，比不能勾更糟。

    只有 bt_ 开头（来自表格）的待办需要写回；手工/群消息的不碰。
    """
    if not str(todo_id).startswith("bt_"):
        return True

    conf = config.load()
    bt = conf.get("bitable") or {}
    base_token, table_id = bt.get("base_token"), bt.get("table_id")
    if not base_token or not table_id:
        return True

    rid = todo_id[3:]
    label = "已完成" if status == DONE else "未开始"
    body = {"records": [{"record_id": rid, "fields": {"状态": label}}]}

    _, err = feishu.run([
        "base", "+record-batch-update",
        "--base-token", base_token,
        "--table-id", table_id,
        "--json", json.dumps(body, ensure_ascii=False),
    ], as_="user")

    if err:
        # 写回失败不算致命：本地已经改了，用户当轮就看到效果，
        # 只是下一轮同步会被表格旧值覆盖，所以记一笔日志便于排查
        db.log(False, "bitable", f"状态写回失败 {rid}: {str(err)[:100]}")
        return False
    return True


def sync_records():
    """读多维表格全部记录，整批覆盖 source='bitable' 的待办。"""
    conf = config.load()
    bt = conf.get("bitable") or {}
    base_token = bt.get("base_token")
    table_id = bt.get("table_id")

    if not base_token or not table_id:
        return {"ok": False, "error": "未配置多维表格（config.bitable）"}

    # JSON 格式下 --limit 上限是 200（不是 2000），超过就分页取。
    # 11 人的待办远到不了 200，但分页逻辑留着，免得以后加人再加代码。
    payload, matrix, ids, fields = {}, [], [], []
    offset = 0

    while True:
        data, err = feishu.run(
            ["base", "+record-list",
             "--base-token", base_token,
             "--table-id", table_id,
             "--format", "json",
             "--limit", "200",
             "--offset", str(offset)],
            as_="user")
        if err:
            db.log(False, "bitable", err)
            return {"ok": False, "error": err}

        payload = data or {}
        fields = payload.get("fields") or fields
        matrix += payload.get("data") or []
        ids += payload.get("record_id_list") or []

        if not payload.get("has_more"):
            break
        offset += 200

    now = int(time.time())
    rows, skipped = [], 0

    for i, row in enumerate(matrix):
        rid = ids[i] if i < len(ids) else f"row{i}"

        title = _text(_cell(row, fields, TITLE_KEYS)).strip()
        owner = _person_id(_cell(row, fields, OWNER_KEYS))

        # 缺标题或没指定负责人的行无法归属到人，跳过而不是塞给某个人
        if not title or not owner:
            skipped += 1
            continue

        status_raw = _text(_cell(row, fields, STATUS_KEYS)).strip()
        status = DONE if status_raw in DONE_WORDS else OPEN
        due = _to_ts(_cell(row, fields, DUE_KEYS))

        rows.append((f"bt_{rid}", owner, title[:200], status, due, now))

    with db.session() as conn:
        # 整批覆盖：表格里删掉的行，看板也跟着消失
        conn.execute("DELETE FROM todos WHERE source='bitable'")
        for r in rows:
            conn.execute("""
                INSERT INTO todos (id, open_id, title, status, due_ts, source, created_at, updated_at)
                VALUES (?,?,?,?,?, 'bitable', ?, ?)
            """, (r[0], r[1], r[2], r[3], r[4], r[5], r[5]))

    msg = f"多维表格 {len(rows)} 条"
    if skipped:
        msg += f"，跳过 {skipped} 条（缺事项或负责人）"
    db.log(True, "bitable", msg)
    return {"ok": True, "count": len(rows), "skipped": skipped, "message": msg}
