# -*- coding: utf-8 -*-
"""通讯录同步：飞书部门 → members 表。

两种取数路径，按配置自动选择：

1) 正规路径（identity=bot 且有 department_id）
   GET /open-apis/contact/v3/users/find_by_department
   能拿到全部门的人，包括从不聊天的人。生产环境走这条。

2) 兜底路径（identity=user 或没配 department_id）
   lark-cli contact +search-user --has-chatted
   只能拿到「跟当前登录人有会话」的同事。用于没凭证时先跑通链路，
   前端会显示已同步人数，缺的人一眼可见。

同步策略：以飞书为准做增改，本地多出来的人（部门里已移除）直接删掉。
围巾颜色按 open_id 稳定分配，同一个人每次刷新颜色不变。
"""
import time

from . import config, db, feishu

SCARVES = [
    "#ef4444", "#f59e0b", "#10b981", "#3b82f6", "#a855f7", "#06b6d4",
    "#ec4899", "#84cc16", "#6366f1", "#f97316", "#14b8a6", "#8b5cf6",
]


# 部门 ID 解析失败后暂不重试，避免每轮同步都打一次注定失败的接口
_RESOLVE_COOLDOWN = {"at": 0, "reason": ""}


def _brief(err, limit=120):
    """飞书的错误是多行 JSON，日志里只留 code + message 一行。"""
    s = str(err or "")
    code = ""
    msg = ""
    if '"code"' in s:
        try:
            code = s.split('"code"')[1].split(",")[0].strip(" :")
        except Exception:
            code = ""
    if '"message"' in s:
        try:
            msg = s.split('"message"')[1].split('"')[1]
        except Exception:
            msg = ""
    if code or msg:
        return f"[{code}] {msg}"[:limit]
    return s.replace("\n", " ")[:limit]


def _scarf_for(open_id):
    """按 open_id 取模，保证同一人颜色稳定。"""
    h = sum(ord(c) for c in (open_id or ""))
    return SCARVES[h % len(SCARVES)]


def _from_department(dept_id, identity):
    rows, page = [], None
    while True:
        params = {"department_id": dept_id, "page_size": 50, "user_id_type": "open_id"}
        if page:
            params["page_token"] = page
        data, err = feishu.get("/open-apis/contact/v3/users/find_by_department",
                               params=params, as_=identity)
        if err:
            return None, err
        for u in (data.get("items") or []):
            rows.append({
                "open_id": u.get("open_id"),
                "name": u.get("name") or "未命名",
                "department": dept_id,
                "email": u.get("email") or "",
            })
        page = data.get("page_token")
        if not data.get("has_more") or not page:
            break
    return rows, None


def _from_chat(chat_id, identity="bot"):
    """用示例部门群的成员名单当人员来源。

    为什么优先走这条：
        find_by_department 要求「部门」本身在应用的数据范围内，
        而数据范围只授权了「人」时，部门接口一律返回 40004。
        群成员接口只要 im:chat.members:read，名单天然就是部门名单，
        而且附带姓名，不需要额外的姓名权限。
    """
    rows, page = [], None
    while True:
        params = {"member_id_type": "open_id", "page_size": 100}
        if page:
            params["page_token"] = page
        data, err = feishu.get(f"/open-apis/im/v1/chats/{chat_id}/members",
                               params=params, as_=identity)
        if err:
            return None, err
        for u in (data.get("items") or []):
            mid = u.get("member_id")
            if not mid or not mid.startswith("ou_"):
                continue          # 跳过机器人（id 以 cli_ 开头）
            rows.append({
                "open_id": mid,
                "name": u.get("name") or "未命名",
                "department": "",
                "email": "",
            })
        page = data.get("page_token")
        if not data.get("has_more") or not page:
            break
    return rows, None


def _resolve_department_id(dept_name, identity="bot", max_depth=5):
    """按部门名在组织架构里反查 open_department_id。

    为什么需要这一步：
        find_by_department 只认 ID，而配置里通常只写了部门名「示例部门」。
        目标部门通常挂在多级组织下面（如「公司 › 业务线 › 部门组 › 示例部门」），
        所以要从根节点开始逐层找。

    查到后会写回 config 缓存，之后每次同步直接用，不再遍历组织架构。
    失败时返回 (None, 原因)，调用方退回 search 兜底，不会中断同步。
    """
    if not dept_name:
        return None, "未配置部门名"

    queue = [("0", 0)]
    seen = set()

    while queue:
        parent, depth = queue.pop(0)
        if depth > max_depth or parent in seen:
            continue
        seen.add(parent)

        page = None
        while True:
            params = {"department_id_type": "open_department_id", "page_size": 50}
            if page:
                params["page_token"] = page
            data, err = feishu.get(
                f"/open-apis/contact/v3/departments/{parent}/children",
                params=params, as_=identity)
            if err:
                return None, err
            for d in (data.get("items") or []):
                if (d.get("name") or "").strip() == dept_name:
                    return d.get("open_department_id"), None
                child = d.get("open_department_id")
                if child:
                    queue.append((child, depth + 1))
            page = data.get("page_token")
            if not data.get("has_more") or not page:
                break

    return None, f"组织架构里没有名为「{dept_name}」的部门"


def _from_search(dept_name):
    data, err = feishu.run(["contact", "+search-user", "--has-chatted"], as_="user")
    if err:
        return None, err
    rows = []
    for u in (data.get("users") or []):
        if dept_name and (u.get("department") or "") != dept_name:
            continue
        rows.append({
            "open_id": u.get("open_id"),
            "name": u.get("localized_name") or u.get("name") or "未命名",
            "department": u.get("department") or dept_name,
            "email": u.get("email") or "",
        })
    return rows, None


def sync_members():
    conf = config.load()
    dept_name = conf.get("department") or ""
    identity = conf.get("identity") or "bot"
    dept_id = conf.get("department_id")

    # 没配部门 ID 时先自动反查一次：权限批下来就不用再手工填配置
    now = int(time.time())
    if not dept_id:
        # 解析失败往往是「权限还没发布」，5 分钟一次的重试没有意义，
        # 冷却 10 分钟：权限一通过最多等 10 分钟就自动生效，又不会刷满日志
        if now - _RESOLVE_COOLDOWN["at"] > 600:
            resolved, rerr = _resolve_department_id(dept_name, "bot")
            if resolved:
                dept_id = resolved
                config.save({"department_id": resolved})
                _RESOLVE_COOLDOWN.update(at=0, reason="")
            else:
                _RESOLVE_COOLDOWN.update(at=now, reason=_brief(rerr))
                db.log(False, "contacts", f"部门 ID 解析失败（已降级）：{_RESOLVE_COOLDOWN['reason']}")

    rows, err, mode = None, None, ""

    # 三级降级，越靠前数据越全：
    #   1. 群成员   —— 只要 im:chat.members:read，名单即部门名单
    #   2. 部门接口 —— 需要部门在数据范围内（当前常报 40004）
    #   3. 搜索兜底 —— 只能拿到聊过天的人，用于全都没配时先跑通
    chat_id = (conf.get("im_todo") or {}).get("chat_id") or conf.get("digest_chat_id")
    if chat_id:
        rows, chat_err = _from_chat(chat_id, identity)
        mode = "群成员"
        if chat_err:
            db.log(False, "contacts", f"群成员取数失败，降级：{_brief(chat_err)}")
            rows = None

    if rows is None and dept_id:
        rows, dept_err = _from_department(dept_id, identity)
        mode = "部门"
        if dept_err:
            db.log(False, "contacts", f"部门取数失败，降级：{_brief(dept_err)}")
            rows = None

    if rows is None:
        rows, err = _from_search(dept_name)
        mode = "搜索兜底"

    if rows is not None:
        err = None

    if err:
        db.log(False, "contacts", err)
        return {"ok": False, "error": err}

    now = int(time.time())
    with db.session() as conn:
        for r in rows:
            if not r["open_id"]:
                continue
            conn.execute("""
                INSERT INTO members (open_id, name, department, email, scarf, updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(open_id) DO UPDATE SET
                    name=excluded.name,
                    department=excluded.department,
                    email=excluded.email,
                    updated_at=excluded.updated_at
            """, (r["open_id"], r["name"], r["department"] or dept_name,
                  r["email"], _scarf_for(r["open_id"]), now))

        if rows:
            keep = [r["open_id"] for r in rows if r["open_id"]]
            marks = ",".join("?" * len(keep))
            conn.execute(f"DELETE FROM members WHERE open_id NOT IN ({marks})", keep)

    msg = f"同步 {len(rows)} 人（{mode}）"
    db.log(True, "contacts", msg)
    return {"ok": True, "count": len(rows), "mode": mode}
