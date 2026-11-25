# -*- coding: utf-8 -*-
"""飞书消息通知。

两个用途：
1. 隧道地址变化时推给指定的人（历史遗留，隧道已停用）
2. 每天 9:00 把看板摘要推送到群里

为什么用应用身份发：
    用户身份的 im 权限不够（230027），应用的 send_as_bot 是通的。
"""
import json
import urllib.request

from . import config, db, feishu

BASE = "https://open.feishu.cn/open-apis/im/v1/messages"


def _token():
    conf = config.load()
    app_id, secret = conf.get("app_id"), conf.get("app_secret")
    if not app_id or not secret:
        return None, "缺少应用凭证"
    try:
        return feishu.tenant(app_id, secret).token(), None
    except Exception as e:
        return None, f"取 token 失败：{e}"


def _post(receive_id, receive_id_type, msg_type, content_obj):
    """统一的发消息入口。content_obj 是 dict（text 用 {"text":...}，卡片用卡片 JSON）。"""
    token, err = _token()
    if err:
        return False, err, None

    body = json.dumps({
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content_obj, ensure_ascii=False),
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE}?receive_id_type={receive_id_type}",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if payload.get("code") == 0:
            return True, None, ((payload.get("data") or {}).get("message_id") or None)
        return False, payload.get("msg"), None
    except urllib.error.HTTPError as e:
        # 飞书的 400 会在 body 里写清哪个字段不合法（如 "unknown property: xxx"），
        # 只回 "HTTPError 400" 等于把最有用的信息丢掉
        try:
            detail = e.read().decode("utf-8", "replace")
            payload = json.loads(detail)
            msg = payload.get("msg") or detail
            ext = ((payload.get("error") or {}).get("ext") or "")
            return False, f"HTTP {e.code}: {msg} {ext}"[:300], None
        except Exception:
            return False, f"HTTP {e.code}", None
    except Exception as e:
        return False, repr(e)[:200], None


def send_card(chat_id, card):
    """发交互式卡片（msg_type=interactive）。返回 (ok, err, message_id)。"""
    return _post(chat_id, "chat_id", "interactive", card)


def send_card_to(open_id, card):
    """发给个人（用于上线前的私聊试发，不打扰群）。"""
    return _post(open_id, "open_id", "interactive", card)


def _send_ex(receive_id, receive_id_type, text):
    """发送消息，返回 (ok, err, message_id)。

    公告需要持久化真实 message_id 作为「确实发出去了」的证据，
    原来只返回布尔值，事后无法证明是哪一条消息。
    """
    token, err = _token()
    if err:
        return False, err, None

    body = json.dumps({
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE}?receive_id_type={receive_id_type}",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
        if payload.get("code") == 0:
            mid = ((payload.get("data") or {}).get("message_id")) or None
            return True, None, mid
        return False, payload.get("msg"), None
    except Exception as e:
        return False, repr(e)[:200], None


def _send(receive_id, receive_id_type, text):
    ok, err, _mid = _send_ex(receive_id, receive_id_type, text)
    return ok, err


def send_text(open_ids, text):
    """给若干个人发。"""
    ok, last = 0, None
    for oid in open_ids:
        good, err = _send(oid, "open_id", text)
        if good:
            ok += 1
        else:
            last = err
    return ok, last


def send_to_chat(chat_id, text):
    """发到群。机器人必须先被拉进群，否则报 230002。"""
    return _send(chat_id, "chat_id", text)


def send_to_chat_ex(chat_id, text):
    """发到群并返回 message_id（公告需要留证）。"""
    return _send_ex(chat_id, "chat_id", text)


def announce_url(url, extra_lines=None):
    conf = config.load()
    targets = conf.get("notify_open_ids") or []
    if not targets:
        return {"ok": False, "error": "config.notify_open_ids 为空"}

    lines = ["看板地址已更新", url]
    if extra_lines:
        lines += extra_lines
    lines.append("（飞书里点开上面的链接即可，不用记地址）")

    ok, err = send_text(targets, "\n".join(lines))
    db.log(bool(ok), "notify", f"通知 {ok}/{len(targets)} 人" + (f" 错误：{err}" if err else ""))
    return {"ok": ok > 0, "sent": ok, "total": len(targets), "error": err}
