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


def _send(receive_id, receive_id_type, text):
    token, err = _token()
    if err:
        return False, err

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
            return True, None
        return False, payload.get("msg")
    except Exception as e:
        return False, repr(e)[:200]


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
