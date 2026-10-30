# -*- coding: utf-8 -*-
"""从群消息里抽取待办。

为什么用轮询而不是长连接：
    飞书的事件订阅主流做法是 WebSocket 长连接，需要官方 SDK；
    这台机器的 pip 被网络策略挡住，装不了第三方包。
    看板本身 5 分钟同步一次，轮询的延迟完全够用，而且零依赖、挂了能自动恢复。

命令格式（群里直接发）：
    #待办 整理周报              记给自己
    #待办 @某人 整理周报         记给指定的人

为什么要约定格式，而不是自然语言识别：
    判断「哪句话是任务、该归谁」的误判率很高，一旦记错人比不记更糟。
    加个 #待办 前缀只多打四个字符，换来的是 100% 准确的归属。

关于 @ 的三种写法（实测）：
    飞书存消息时会把 @ 换成占位符 @_user_1，真实身份放在顶层 mentions 数组里，
    所以不能只靠正则从正文里抓 open_id。三种形态都要认：
        1. "@_user_1" + mentions[].key  → 目前线上消息的实际格式
        2. <at user_id="ou_xxx">名字</at> → 部分客户端/旧版本
        3. 纯文本 @姓名                   → 复制粘贴时残留
"""
import json
import re
import time

from . import config, db, feishu

OPEN = "open"

# 半角/全角井号都认，手机输入法容易打成全角
CMD_RE = re.compile(r"[#＃]待办\s*")
# 占位符形态：@_user_1
MENTION_KEY_RE = re.compile(r"@_user_\d+")
# 标签形态
AT_TAG_RE = re.compile(r'<at\s+user_id="([^"]+)"\s*>[^<]*</at>')
# 纯文本 @名字
PLAIN_AT_RE = re.compile(r"@([^\s@]+)")

# 每次回看 3 小时：即使服务停了半小时，重启后也能把命令补抓回来，
# 重复的靠 id 去重，不会产生多余待办。
SEEN_WINDOW_MIN = 180


def _clean(text):
    """去掉消息里的零宽字符和多余空白。"""
    if not text:
        return ""
    text = text.replace("\u200b", "").replace("\u2060", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_command(text, sender_id="", name_map=None, mentions=None):
    """解析 #待办 命令。

    返回 (open_id, 标题)；不是命令或没有标题时返回 None。

    name_map  : {姓名: open_id}，用于识别纯文本 @姓名
    mentions  : 消息里的 mentions 数组，用于把 @_user_1 还原成 open_id
    """
    if not text:
        return None

    m = CMD_RE.search(text)
    if not m:
        return None

    body = text[m.end():]
    owner = ""

    # 1) 占位符 + mentions（线上消息的真实格式）
    if mentions:
        by_key = {x.get("key"): (x.get("id") or "") for x in mentions if x.get("key")}
        hit = MENTION_KEY_RE.search(body)
        if hit:
            owner = by_key.get(hit.group(0)) or ""
            body = body[:hit.start()] + " " + body[hit.end():]

    # 2) <at user_id="..."> 标签
    if not owner:
        at = AT_TAG_RE.search(body)
        if at:
            uid = at.group(1)
            if uid.startswith("ou_"):
                owner = uid
            body = body[:at.start()] + " " + body[at.end():]

    # 3) 纯文本 @名字
    if not owner:
        p = PLAIN_AT_RE.search(body)
        if p:
            hit = (name_map or {}).get(p.group(1))
            if hit:
                owner = hit
                body = body[:p.start()] + " " + body[p.end():]

    title = _clean(body)

    # 只有「#待办 @某人」没有内容时不算命令，否则会生成一条空待办
    if not title:
        return None
    if not owner:
        owner = sender_id
    if not owner:
        return None

    return owner, title[:200]


def _chat_id():
    conf = config.load()
    im = conf.get("im_todo") or {}
    # 没单独配就复用日报群——示例部门本来就是同一个群
    return im.get("chat_id") or conf.get("digest_chat_id") or ""


def ingest(items, name_map=None, now=None):
    """把一批消息转成待办，返回 (新增数, 重复数)。

    拆出来是为了能离线测试：不依赖真实群消息，直接喂消息列表即可。
    """
    now = now or int(time.time())
    added, dup = 0, 0

    for it in items or []:
        if it.get("msg_type") != "text" or it.get("deleted"):
            continue

        # 只认真人发的：机器人自己发的公告里常带 #待办 示例，
        # 不排除的话教程刚发出去就会被自己抓成真待办
        sender = it.get("sender") or {}
        if sender.get("sender_type") not in (None, "user"):
            continue

        raw = (it.get("body") or {}).get("content") or "{}"
        try:
            text = (json.loads(raw) or {}).get("text") or ""
        except Exception:
            continue

        parsed = parse_command(text, sender.get("id") or "", name_map, it.get("mentions"))
        if not parsed:
            continue

        owner, title = parsed
        mid = it.get("message_id")
        if not mid:
            continue

        # 归属不存在的人（比如群里有外部联系人）就跳过，不要写脏数据
        if name_map is not None and owner not in set(name_map.values()) and owner != sender.get("id"):
            continue

        with db.session() as conn:
            # OR IGNORE：同一条命令只入库一次，
            # 也不会把用户在看板上勾掉的「已完成」覆盖回未完成
            cur = conn.execute("""
                INSERT OR IGNORE INTO todos
                    (id, open_id, title, status, due_ts, source, created_at, updated_at)
                VALUES (?,?,?, 'open', NULL, 'im', ?, ?)
            """, (f"im_{mid}", owner, title, now, now))
            if cur.rowcount:
                added += 1
            else:
                dup += 1

    return added, dup


def poll_commands():
    """扫最近一段时间的群消息，把命令转成待办。返回 (新增数, 错误)。"""
    chat_id = _chat_id()
    if not chat_id:
        return 0, "未配置群（config.im_todo.chat_id）"

    now = int(time.time())
    data, err = feishu.get(
        "/open-apis/im/v1/messages",
        params={
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(now - SEEN_WINDOW_MIN * 60),
            "end_time": str(now),
            "sort_type": "ByCreateTimeDesc",
            "page_size": 50,
        })
    if err:
        db.log(False, "im_todo", err)
        return 0, err

    with db.session() as conn:
        name_map = {r["name"]: r["open_id"]
                    for r in conn.execute("SELECT name, open_id FROM members")}

    added, dup = ingest((data or {}).get("items") or [], name_map, now)

    if added:
        db.log(True, "im_todo", f"群消息新增 {added} 条")
    return added, None
