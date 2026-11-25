# -*- coding: utf-8 -*-
"""配置读写。只做存取，不含业务判断。

配置文件 config.json 结构：
{
  "department": "示例部门",
  "sync_minutes": 5,
  "feishu_domain": "feishu",
    "base_url": "http://127.0.0.1:8900",
  "identity": "bot",              // bot = 企业应用身份（拉全员）；user = 当前登录身份（只够调试）
  "bootstrap_lead": "",             // 负责人姓名，用于把负责人排到树的第一层
  "approvals": [                   // 审批定义 → 业务语义的映射，一张表配几条就能跑
    {
      "code": "XXXXXXXX",          // 审批定义 code（飞书后台可见）
      "kind": "leave",             // leave 请假 | travel 出差 | field 外勤
      "fields": {                  // 表单字段名 → 标准字段
        "start": "开始时间",
        "end": "结束时间",
        "city": "出差城市",
        "reason": "请假事由"
      }
    }
  ]
}
"""
import json
import os
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 配置文件含凭据，生产上放在 /etc（600），不跟代码放一起
PATH = os.environ.get("BOARD_CONFIG") or os.path.join(BASE, "config.json")

DEFAULTS = {
    "department": "示例部门",
    "department_id": "",
    "sync_minutes": 5,
    "feishu_domain": "feishu",
    "base_url": "http://127.0.0.1:8900",
    "identity": "bot",
    "bootstrap_lead": "",
    # 能改所有人待办的姓名名单。默认空 = 只有本人能改自己的。
    "todo_admins": [],
    "species": {},
    "allow_departments": ["示例部门"],
    "notify_open_ids": [],   # 隧道地址变化时推送给这些人（open_id）
    "digest_chat_id": "",   # 每日摘要推送的群 chat_id
    "digest_hour": 9,        # 已停用：日报改由晨间统采完成后触发（保留仅为兼容旧配置）
    "approvals": [],
    # ── 晨间统采（2026-11-25） ───────────────────────────────────
    # 目标采样时刻。真实触发还要求「当天收到过个人待核验事件」，
    # 没有事件就不跑、也不发日报——不允许退回固定时间推送。
    "morning_target": "08:50",
    # 可选兜底：没有个人事件时的部门日报时间（HH:MM）。默认空 = 关闭。
    # 打开后日志会标注 trigger_source=fallback，不会伪装成正常统采。
    "digest_fallback_hour": "",
    # QuickLaunch 设备 → 飞书 open_id 绑定。内部接口只认这里登记过的设备，
    # 请求方无法通过传参读别人。格式：{"<device_id>": "ou_xxx"}
    "quicklaunch": {"device_bindings": {}},
    # 服务间共享密钥（HMAC）。优先读环境变量 QL_INTERNAL_TOKEN，这里仅作本地调试后备。
    "internal_token": "",
}

_lock = threading.Lock()


def load():
    with _lock:
        if not os.path.exists(PATH):
            _write(dict(DEFAULTS))
            return dict(DEFAULTS)
        try:
            with open(PATH, encoding="utf-8") as f:
                return {**DEFAULTS, **json.load(f)}
        except Exception:
            return dict(DEFAULTS)


def save(patch):
    """只覆盖传入的键，空字符串视为“不修改”，避免误清空凭证。"""
    with _lock:
        conf = load()
        for k, v in patch.items():
            if k in DEFAULTS and v not in (None, ""):
                conf[k] = v
        _write(conf)
        return conf


def _write(conf):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
