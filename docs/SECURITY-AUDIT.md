# 权限与安全审计（2026-11-25 第二轮）

> 范围：team-desk-board 全部 HTTP 入口、会话机制、服务间接口、数据暴露面。
> 方法：读代码 + 真实角色打请求 + 单元测试固定行为。

## 1. 已修复的 4 个高危问题

| # | 问题 | 攻击路径 | 修复 | 回归 |
|---|---|---|---|---|
| 1 | OAuth 回调无条件写 `users` 表 | 任何飞书账号点一次 `/login` 即成为授权用户，可读全部门数据 | 回调阶段校验「在 members 名单」+「部门命中 allow_departments」，否则 403 页面且不落库 | 手动验证 + `test_authz` |
| 2 | 部门校验 fail-open | `allow and dept and dept not in allow`：白名单为空 / dept 为空 / 通讯录未同步 → 全部放行 | 改 fail-closed：白名单缺失、部门无法核实、部门不匹配 一律拒绝并 `db.log` | `test_authz` 4 项 |
| 3 | 敏感接口无角色限制 | `/api/users`、`/api/config`、`/api/absences` 全员可读；`POST /api/config`、`POST /api/sync` 任何登录用户可写；`/api/todos?open_id=` 可读他人待办；**POST 分支只校验登录、不过部门闸门** | 前三个限 `todo_admins`；待办按「本人或负责人」；POST 与 GET 共用 `_authorize()` | `test_authz` 8 项 |
| 4 | Cookie 明文可冒充 | `uid=ou_xxx` 无签名，而 `/api/board` 返回全员 open_id → 填别人的 id 即以对方身份访问 | `uid=<open_id>.<ts>.<hmac-sha256>`；密钥缺失拒绝一切 Cookie；HTTPS 下带 `Secure` | `test_session` 8 项 |

## 2. 逐项回答审计清单

**`/api/board` 是否仍暴露全员 open_id？**
是，仍返回。评估：open_id 是飞书租户内的用户标识，企业内本就半公开（通讯录可见）；
修复 #4 之后，「知道 open_id」不再等于「能冒充」。字段里没有邮箱、手机号、精确打卡时间
（精确时间仅 `todo_admins` 可见）。判定：**保留**，属可接受设计，而非漏洞。
若后续要彻底隐藏，需给前端引入 opaque member key（改动 6 处前端 + 3 个接口），当前不做。

**30 天 Cookie 的权限撤销是否及时？**
及时。`_identity()` 每次请求实时查 `users` + `members`，没有任何身份缓存；
`config.load()` 也不缓存，改 `todo_admins` / `allow_departments` 立即生效。
Cookie 只是「凭据载体」，权限判定不依赖它。

**成员调离运营部后能否立即失权？**
最迟 5 分钟（`members` 同步周期）。同步时 `contacts.sync_members()` 会把不在名单的人
从 `members` 表删除 → 下一次请求 `_authorize()` 因查不到 dept 而 403。
不依赖 Cookie 过期。

**普通成员能否修改管理员配置？**
不能。`POST /api/config`、`POST /api/sync` 均校验 `_is_admin`；实测 403。

**QuickLaunch 是否只能读本人记录？**
是。`/internal/attendance/self` 只接受 `device_id`，服务端用 `device_bindings` 反查 open_id；
传 `open_id` / `employee_no` 参数无效（`test_internal_api` 有专项用例）。
签名覆盖完整 path（含 query），改参数即 401；nonce 重放 401；未绑定设备 403。

**日报机器人是否只读必要聚合？**
是。`digest.build_text()` 只取姓名、审批状态、待办计数、考勤计数；
不包含 record_id、精确打卡时间、设备信息、邮箱。

**数据库/配置/Token/备份能否被下载？**
不能。`STATIC_ALLOW = ("/index.html", "/favicon.ico", "/vendor/")` 精确白名单；
实测 `/board.db`、`/config.json`、`/app/api.py`、`/logs/server.log`、`/.git/config` 全部 302/404。

## 3. 剩余风险（如实记录）

| 风险 | 等级 | 说明 |
|---|---|---|
| open_id 对登录成员可见 | 低 | 见上，无法用于提权 |
| 服务间密钥与内部接口同机 | 低 | 仅 `172.18.0.1` / 回环可达，不出网 |
| 单机 SQLite 无备份策略自动化 | 中 | 目前靠手工快照；建议加 cron 每日 `sqlite3 .backup` |
| 公告仅一次机会 | 中 | 已用主键幂等 + SENDING 状态防重，但「SENDING 悬挂」需人工核对群消息 |

## 4. 测试基线

```
python -m unittest discover -s tests -t .   # 64 项
cd vps && python -m unittest discover -s mailer/tests -t .   # 81 项（QuickLaunch 侧）
```

权限专项：`tests/test_authz.py`（16 项）、`tests/test_session.py`（8 项）、
`tests/test_internal_api.py`（12 项）。
