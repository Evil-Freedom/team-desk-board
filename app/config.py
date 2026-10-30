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
PATH = os.path.join(BASE, "config.json")

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
    "digest_hour": 9,        # 几点推（24 小时制）   # 只允许这些部门访问；空列表 = 不限制
    "approvals": [],
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
