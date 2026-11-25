# 统一考勤采集与晨间日报

> 2026-11-25 上线。替代「固定 9:00 推送日报」与「QuickLaunch 自己查飞书考勤」两条旧路径。

## 1. 为什么改

| 旧行为 | 问题 |
|---|---|
| 每天 9:00 定时发群摘要 | 数据是 9:00 那一刻的快照，考勤 8:50 才落地，经常发出去就是空的 |
| QuickLaunch 自己调飞书查本人考勤 | 两个消费者各查一遍，配额浪费，且会出现「看板显示已打卡、手机还在等」 |
| 看板只有审批状态 | `normal` 只代表没请假，不代表来了公司 |

现在：一次采集 → 清洗 → 落库 → 分别分发给看板与 QuickLaunch。

## 2. 数据流

```
飞书 OpenAPI（应用身份 / 用户身份）
        │
        ▼
app/attendance.py  ── 采集、解析、去重、落库
        │
        ▼
attendance_records（按 日期 + 工号 唯一）
        │
        ├──► /api/board          部门视图（按角色裁剪字段）
        └──► /internal/attendance/self   本人证据（签名 + 设备绑定）
```

## 3. 取数方式（两条，自动选择）

| 模式 | 条件 | 覆盖 | 说明 |
|---|---|---|---|
| `team` | `attendance_identity` 有 open_id→employee_no 映射 | 映射内全部成员 | 应用身份按工号批量查，一次调用 |
| `per_user` | 无映射 | 已授权成员 | 每人用自己的 user token 查自己，合法且无需管理员审批 |

**当前状态（2026-11-25 实测）**：`per_user`，覆盖 5 人（有 token），成功 1 人（姜芃）。
其余成员授权时未包含 `attendance:task:readonly`，接口返回 `99991679`。

`team` 模式需要应用具备 `contact:user.employee_id:readonly`（读员工工号）。
拿到后 `sync_identity()` 自动回填映射，同步即切换，业务代码零改动。

## 4. 状态机（独立于审批状态）

| 状态 | 含义 | 触发条件 |
|---|---|---|
| `CHECKED_IN` | 已打卡 | 有 record_id 且有 check_time |
| `NOT_YET` | 待打卡 | 飞书返回 `Todo` |
| `CHECKIN_PENDING` | 待同步 | 在采集范围内，但今天还没有记录 |
| `CHECKIN_UNKNOWN` | 结果未知 | 不在采集范围（未授权），或接口失败 |
| `ABSENT_CONFIRMED` | 记录缺勤 | 飞书明确返回 `Absent` |
| `NOT_APPLICABLE` | 今日不考核 | 当天请假/出差（审批状态非 normal） |

**硬规则**：查不到记录 ≠ 旷工。没有 `Absent` 就不出现「缺勤」。
`members.status` 语义（normal/away/leave）未改动，考勤是并列字段。

## 5. 数据表

| 表 | 用途 | 关键约束 |
|---|---|---|
| `attendance_records` | 每人每天一条打卡结果 | PK `(attendance_date, employee_no)`，重复采集不产生新行 |
| `attendance_sync_log` | 每次采集的结果与失败原因 | 带 `run_id`，可与 morning_runs 对齐 |
| `morning_runs` | 每日统采任务 | PK `business_date`，同一天只有一条 |
| `attendance_identity` | open_id ↔ employee_no | 由采集过程自动回填 |

不存照片 URL、Wi-Fi、经纬度——看板用不上，属于个人隐私。

## 6. 晨间统采（morning）

```
事件时间 08:40 → 目标 08:50
事件时间 08:48 → 目标 08:50
事件时间 08:55 → 目标 08:57（事件 + 2 分钟）
```

- 触发源：QuickLaunch 上报的 `checkin_unknown` / `launch_success`
- 执行顺序：人 → 审批 → 任务 → 表格 → 群待办 → **考勤** → 状态 → 日报
- 重试：`08:50 / +5min / +15min`，共 3 次，不无限轮询
- 超期 45 分钟仍无数据 → `attendance_sync_status=unknown`，停止轮询
- 日报：考勤采集成功（`ok`/`partial`）才发，且当天只发一次（`UPDATE ... WHERE` 原子认领）

固定 9:00 已取消：`digest.due_now()` 恒返回 `False`，调度器不再调用它。
迁移当天若旧机制已推送，`_legacy_sent_today()` 会识别并跳过，避免一天两条。

## 7. 内部接口

`GET /internal/attendance/self?attendance_date=&device_id=`

- 认证：`X-QL-Timestamp` / `X-QL-Nonce` / `X-QL-Signature`（HMAC-SHA256）
- 签名覆盖：`timestamp\nnonce\nMETHOD\n完整path含query\nsha256(body)`
- 时间窗 ±300 秒，nonce 窗口内一次性
- `device_id` 必须在 `config.quicklaunch.device_bindings` 登记过
- 不接受任何 `open_id` / `employee_no` 入参——请求方改不了「要谁的」

`POST /internal/morning/plan` 同上签名，body `{device_id, event_time, type}`，同日幂等。

## 8. 运维

```bash
systemctl status team-desk-board
journalctl -u team-desk-board -n 50
# 手工触发一次采集（不启动 HTTP）
sudo -u statusboard env BOARD_DB=/var/lib/team-desk-board/board.db \
  BOARD_CONFIG=/etc/team-desk-board/config.json python3 -c \
  "import sys;sys.path.insert(0,'/opt/team-desk-board');from app import attendance;print(attendance.sync())"
```

配置项（`/etc/team-desk-board/config.json`）：

| 键 | 说明 |
|---|---|
| `morning_target` | 统采目标时刻，默认 `08:50` |
| `quicklaunch.device_bindings` | `{device_id: open_id}`，内部接口只认这里 |
| `internal_token` | 服务间密钥（生产用环境变量 `QL_INTERNAL_TOKEN`） |
| `digest_fallback_hour` | 兜底日报时刻，默认空 = 关闭 |

## 9. 回滚

见 QuickLaunch `docs/rollback.md`。要点：新表是**纯增量**，回滚只需停服务、切回 Windows 实例、`DROP` 四张新表（可选）。
