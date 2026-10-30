# -*- coding: utf-8 -*-
"""状态墙后端。

模块职责（严格单向依赖，不互相回指）：

    config    读写配置，无业务逻辑
    db        建表与连接，无业务逻辑
    feishu    唯一调用 lark-cli 的出口，只负责发请求和解析 JSON
    contacts  通讯录同步：飞书部门 → members 表
    approvals 审批同步：飞书审批实例 → absences 表（含字段映射）
    status    状态推导：absences + 当前时间 → 每人今日状态（纯函数，不碰网络）
    api       HTTP 路由，只做参数校验和响应组装
    scheduler 定时触发，不处理数据
"""

__all__ = ["config", "db", "feishu", "contacts", "approvals", "status", "api", "scheduler"]
