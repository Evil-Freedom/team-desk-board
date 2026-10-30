# -*- coding: utf-8 -*-
"""审批同步：飞书审批实例 → absences 表。

为什么以审批为主数据源：
    出差和请假本来就要走审批，人已经录过一遍，再看板里填一次是重复劳动。
    审批通过即事实，所以这里不做人工确认，直接落库。

数据流：
    config.approvals[].code（审批定义）
        ↓ GET /approval/v4/instances            拿实例 code 列表（应用身份）
        ↓ GET /approval/v4/instances/{code}     拿表单详情（已入库的跳过）
        ↓ _extract()                            从原生控件里抽出时间段
    absences(open_id, kind, start_ts, end_ts, city)

关于字段映射：
    飞书的「请假」用 leaveGroupV2 控件，「出差」用 tripGroup 控件，
    它们的 value 是结构化对象，形如
        {"start":"2026-09-21T09:00:00+08:00","end":"...","name":"年假","reason":"看病"}
    所以不需要按控件标题配置映射，直接按结构识别，换一家公司也能用。
    只有非标准表单才需要在 config 里补 fields 覆盖。

已知边界（骨架阶段不做，接真数据时按需补）：
    - 审批被撤销后，本地旧记录不会自动删除，需要加反向对账
    - 一个审批定义里混用多种业务时，需要按 name 字段二次分流
"""
import json
import time
from datetime import datetime

from . import config, db, feishu

PASS_STATUS = {"APPROVED", "PASS"}


# ------------------------------------------------------------------ 取数
def _list_instance_codes(client, approval_code, since_ts, until_ts):
    """飞书的 start_time / end_time 单位是毫秒。"""
    codes, page = [], None
    while True:
        params = {
            "approval_code": approval_code,
            "start_time": str(since_ts * 1000),
            "end_time": str(until_ts * 1000),
            "page_size": 100,
        }
        if page:
            params["page_token"] = page
        data, err = client.get("/open-apis/approval/v4/instances", params)
        if err:
            return None, err
        codes += data.get("instance_code_list") or []
        page = data.get("page_token")
        if not data.get("has_more") or not page:
            break
    return codes, None


def _get_instance(client, code):
    data, err = client.get(f"/open-apis/approval/v4/instances/{code}")
    if err:
        return None, err
    return data, None


# ------------------------------------------------------------------ 解析
def _parse_form(form):
    """form 是 JSON 字符串，转成 [(控件名, value)]。"""
    if isinstance(form, str):
        try:
            form = json.loads(form)
        except Exception:
            return []
    out = []
    for item in form or []:
        out.append((item.get("name") or item.get("custom_id") or item.get("id"),
                    item.get("value")))
    return out


def _find_period(value, depth=0):
    """在任意嵌套结构里找出含 start/end 的那一层 —— 请假和出差控件都长这样。"""
    if depth > 5 or value is None:
        return None
    if isinstance(value, dict):
        if value.get("start") and value.get("end"):
            return value
        for v in value.values():
            hit = _find_period(v, depth + 1)
            if hit:
                return hit
    elif isinstance(value, list):
        for v in value:
            hit = _find_period(v, depth + 1)
            if hit:
                return hit
    return None


def _parse_time(value):
    """兼容 ISO8601、'2026年09月21日 09:00'、'2026-09-21 09:00'、毫秒戳。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10_000_000_000 else v

    text = str(value).strip()
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        pass

    for fmt in ("%Y年%m月%d日 %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return None


def _extract(instance, mapping):
    """一条实例 → 一条 absences 记录；不合规返回 None。"""
    status = (instance.get("status") or "").upper()
    if status not in PASS_STATUS:
        return None

    form = _parse_form(instance.get("form"))

    period = None
    for _name, value in form:
        period = _find_period(value)
        if period:
            break
    if not period:
        return None

    start = _parse_time(period.get("start"))
    end = _parse_time(period.get("end"))
    if not start or not end or end < start:
        return None

    open_id = instance.get("open_id") or instance.get("user_id")
    if not open_id:
        return None

    city = period.get("city") or period.get("destination") or period.get("城市")
    reason = period.get("reason") or period.get("说明")
    label = period.get("name") or ""

    return {
        "id": instance.get("instance_code") or instance.get("code"),
        "open_id": open_id,
        "kind": mapping.get("kind") or "leave",
        "start_ts": start,
        "end_ts": end,
        "city": city if isinstance(city, str) else None,
        "reason": reason if isinstance(reason, str) else (label or None),
        "raw": json.dumps(instance, ensure_ascii=False)[:4000],
    }


def _upsert(rec):
    with db.session() as conn:
        conn.execute("""
            INSERT INTO absences (id, open_id, kind, start_ts, end_ts, city, reason, source, raw, updated_at)
            VALUES (?,?,?,?,?,?,?, 'approval', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                start_ts=excluded.start_ts,
                end_ts=excluded.end_ts,
                city=excluded.city,
                reason=excluded.reason,
                updated_at=excluded.updated_at
        """, (rec["id"], rec["open_id"], rec["kind"], rec["start_ts"], rec["end_ts"],
              rec["city"], rec["reason"], rec["raw"], int(time.time())))


# ------------------------------------------------------------------ 按人授权拉取
def _list_initiated(token, definition_code, since_ts, until_ts):
    """拉「我发起的」审批，返回整条记录（含 summaries），不只要 code。

    两个刻意的选择：
    1) 用 /instances/initiated 而不是 /instances：
       后者只认 tenant token，用户令牌会报 99991668。
    2) 不逐条拉详情：
       initiated 返回的 summaries 已包含「开始时间/结束时间/假期类型/事由」，
       而详情接口 /instances/{code} 同样不接受用户令牌，拉也拉不到。
    """
    items, page = [], None
    while True:
        params = {
            "definition_code": definition_code,
            "start_timestamp": since_ts,
            "end_timestamp": until_ts,
            "page_size": 100,
        }
        if page:
            params["page_token"] = page
        data, err = feishu.get_with_token(
            token, "/open-apis/approval/v4/instances/initiated", params)
        if err:
            return None, err
        items += data.get("instances") or []
        page = data.get("page_token")
        if not data.get("has_more") or not page:
            break
    return items, None


def _extract_summary(item, mapping):
    """从 initiated 的 summaries 里解析时间段。

    summaries 是审批中心渲染好的中文键值对，形如
        [{"key":"假期类型","value":"年假"},{"key":"开始时间","value":"2026年09月21日 09:00"},...]
    用关键词匹配而不是固定 key，换表单也不容易坏。
    """
    if item.get("instance_status") != 2:      # 2 = 已通过
        return None

    kv = {}
    for s in item.get("summaries") or []:
        k = (s.get("key") or "").strip()
        v = s.get("value")
        if k and isinstance(v, str):
            kv[k] = v

    def pick(*words):
        for k, v in kv.items():
            if any(w in k for w in words):
                return v
        return None

    start = _parse_time(pick("开始"))
    end = _parse_time(pick("结束"))
    if not start or not end or end < start:
        return None

    open_id = item.get("initiator") or item.get("open_id")
    if not open_id:
        return None

    return {
        "id": item.get("instance_code"),
        "open_id": open_id,
        "kind": mapping.get("kind") or "leave",
        "start_ts": start,
        "end_ts": end,
        "city": pick("城市", "目的地", "地点"),
        "reason": pick("事由", "原因", "说明") or pick("假期类型"),
        "raw": json.dumps(item, ensure_ascii=False)[:4000],
    }


def sync_for_user(open_id, access_token, days=60):
    """用某个成员自己的授权，拉他自己的请假/出差。

    为什么按人拉：用户身份的接口只能看自己可见的实例，
    想把全部门拼齐，就得每个人各授权一次，再把结果汇总到同一张 absences 表。
    """
    conf = config.load()
    maps = conf.get("approvals") or []
    if not maps:
        return {"ok": False, "error": "未配置审批映射", "fresh": 0}

    now = int(time.time())
    since = now - days * 86400

    with db.session() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM absences")}

    fresh, errors = 0, []
    for m in maps:
        code = m.get("code")
        if not code:
            continue
        items, err = _list_initiated(access_token, code, since, now)
        if err:
            errors.append(f"{m.get('label') or code}: {err}")
            continue
        for inst in items:
            rec = _extract_summary(inst, m)
            if not rec or not rec["id"]:
                continue
            if rec["id"] in known:
                continue
            _upsert(rec)
            known.add(rec["id"])
            fresh += 1

    with db.session() as conn:
        conn.execute("UPDATE users SET last_sync_at=? WHERE open_id=?", (now, open_id))

    return {"ok": not errors, "fresh": fresh, "errors": errors}


def sync_all_authorized(days=60):
    """遍历所有已授权的成员，各拉一次。调度器每分钟调这个。"""
    from . import oauth

    conf = config.load()
    app_id, secret = conf.get("app_id"), conf.get("app_secret")
    if not app_id or not secret:
        return {"ok": False, "error": "缺少应用凭证", "users": 0, "fresh": 0}

    total_users, total_fresh, errors = 0, 0, []
    for u in oauth.list_users():
        token, err = oauth.get_valid_token(u["open_id"], app_id, secret)
        if err:
            errors.append(f"{u.get('name') or u['open_id']}: {err}")
            continue
        total_users += 1
        r = sync_for_user(u["open_id"], token, days)
        total_fresh += r.get("fresh") or 0
        errors += (r.get("errors") or [])[:1]

    msg = f"{total_users} 人已授权，新增 {total_fresh} 条"
    if errors:
        msg += f"，{len(errors)} 个错误：{errors[0][:140]}"
    db.log(not errors, "approvals", msg)
    return {"ok": not errors, "users": total_users, "fresh": total_fresh,
            "errors": errors, "message": msg}


# ------------------------------------------------------------------ 入口
def sync_absences(days=60):
    conf = config.load()
    maps = conf.get("approvals") or []
    if not maps:
        msg = "未配置审批映射：config.approvals 为空"
        db.log(False, "approvals", msg)
        return {"ok": False, "error": msg, "fresh": 0}

    app_id = conf.get("app_id")
    secret = conf.get("app_secret")
    if not app_id or not secret:
        msg = "缺少应用凭证：config.app_id / app_secret 为空，无法以应用身份拉全量实例"
        db.log(False, "approvals", msg)
        return {"ok": False, "error": msg, "fresh": 0}

    client = feishu.tenant(app_id, secret)
    now = int(time.time())
    since = now - days * 86400

    with db.session() as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM absences")}

    fresh, skipped, errors = 0, 0, []
    for m in maps:
        code = m.get("code")
        if not code:
            continue
        codes, err = _list_instance_codes(client, code, since, now)
        if err:
            errors.append(f"{m.get('kind') or code}: {err}")
            continue
        for ic in codes:
            if ic in known:
                skipped += 1
                continue
            inst, err = _get_instance(client, ic)
            if err:
                errors.append(f"{ic}: {err}")
                continue
            rec = _extract(inst, m)
            if rec:
                _upsert(rec)
                fresh += 1
            else:
                skipped += 1

    ok = not errors
    msg = f"新增 {fresh} 条，跳过 {skipped} 条"
    if errors:
        msg += f"，{len(errors)} 个错误：{errors[0][:160]}"
    db.log(ok, "approvals", msg)
    return {"ok": ok, "fresh": fresh, "skipped": skipped, "errors": errors, "message": msg}
