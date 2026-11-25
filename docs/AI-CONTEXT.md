# 团队状态看板 · AI 上下文包

> 生成日期：2026-11-25，对应本地提交 `1292172`。
> 用途：把本项目的完整技术上下文一次性交给外部模型（GPT 等），使其能在不追问、
> 不猜测的前提下追加需求、修改代码、编写补丁。
>
> **脱敏声明**：本文所有部门名、人名、内网地址、飞书 ID / token / chat_id 均为占位值。
> 真实值只存在于本地 `config.json` 与 `board.db`，两者均在 `.gitignore` 内，永不入库。
> 你（外部模型）不需要真实值也能完成开发；需要新增配置项时，只写进 `config.example.json`。

---

## 0. 先读这一段（三条硬约束）

1. **零第三方 Python 依赖**。项目只用标准库：`http.server` / `sqlite3` / `threading` /
   `urllib` / `json`。目标机器 `pip` 被网络策略拦截，装不了任何包。
   → 你提出的方案不得引入 `requests`、`flask`、`fastapi`、`APScheduler`、`pydantic`、`aiohttp` 等。
   → 唯一的外部可执行程序是 `lark-cli`（飞书官方 CLI），已经装好，用 `subprocess` 调。
2. **单进程**。HTTP 服务与定时调度器跑在同一个 Python 进程里（`ThreadingHTTPServer` +
   后台线程）。没有 worker、没有队列、没有 Redis。
3. **SQLite 是唯一状态**。`board.db` 单文件，四张表。没有 ORM，SQL 直接写在模块里。

改动任何东西之前，先读完第 9 节「已知坑与禁区」和第 11 节「改完怎么验证」。

---

## 1. 项目定位

一个跑在公司内网的「团队状态墙」，把三件事汇总到一页：

| 维度 | 数据来源 | 结论 |
|---|---|---|
| 在不在 | 飞书审批（请假 / 出差 / 外勤） | `normal` / `away` / `leave` |
| 几件事 | 飞书任务 + 多维表格 + 群消息命令 + 看板手工录入 | `doing` / `overdue` 计数 |
| 忙不忙 | 上面两者合成 | 前端六态：空闲 / 正常 / 忙碌 / 超载 / 出差 / 休假 |

每天 9:00 往群里推一条文本摘要。规模：十余人，单部门，单租户。

---

## 2. 技术栈

| 层 | 实现 |
|---|---|
| HTTP | `http.server.ThreadingHTTPServer` + `SimpleHTTPRequestHandler` 子类（`app/api.py`） |
| 存储 | SQLite 单文件 `board.db`，`sqlite3` 标准库，`db.session()` 上下文管理器 |
| 调度 | `threading.Thread` + `Event.wait(interval)`（`app/scheduler.py`） |
| 飞书 | 双通道：`lark-cli` 子进程（首选）+ `urllib` 直连 OpenAPI（兜底），全部集中在 `app/feishu.py` |
| 前端 | 单文件 `index.html`（1420 行），原生 JS + 手写 SVG + `vendor/anime.min.js`，无构建步骤 |
| 部署 | Windows 计划任务 `StatusBoard` → `pythonw.exe server.py`，监听 `0.0.0.0:8900` |

---

## 3. 目录结构与文件职责

```
team-desk-board/
├── server.py              入口：db.init() → 启动调度器 → 起 HTTP。日志自己写 logs/server.log
├── index.html             看板前端（唯一页面，含全部 SVG/样式/脚本）
├── landing.html           对外入口页（介绍 + 打开看板按钮）
├── config.json            ★ 运行时配置，含凭据，gitignore（模板见 config.example.json）
├── board.db               ★ SQLite 数据，含 token，gitignore
├── app/
│   ├── api.py             HTTP 路由 + 鉴权 + 响应组装（业务一律下沉）
│   ├── feishu.py          唯一出网层：lark-cli 子进程 / urllib 直连，token 换发
│   ├── oauth.py           飞书 OAuth：授权 URL、换 token、刷新（refresh_token 一次性）、userinfo
│   ├── contacts.py        人员同步（三级降级：群成员 → 部门接口 → 搜索兜底）
│   ├── approvals.py       审批 → absences（两条路径：应用身份 / 按人授权）
│   ├── tasks.py           待办：飞书任务整批覆盖 + 手工补录 + 统计
│   ├── bitable.py         多维表格待办（整批覆盖 + 勾选后写回表格）
│   ├── im_todo.py         群消息 `#待办` 命令抽取（轮询最近 3 小时）
│   ├── status.py          纯函数：absences + 当前时间 → (status, loc)。不联网、不读配置
│   ├── digest.py          每日群摘要的文本生成与发送判定
│   ├── notify.py          飞书消息发送（应用身份 send_as_bot）
│   ├── scheduler.py       定时循环：sync_all() + digest.due_now()
│   ├── config.py          配置读写（未配置键用 DEFAULTS 兜底）
│   └── db.py              建表、迁移补列、事务、同步日志
├── vendor/anime.min.js    anime.js v3.2.2（MIT），唯一前端依赖
├── docs/preview.png       样图（构造数据）
├── docs/AI-CONTEXT.md     ← 本文件
├── logs/server.log        运行日志（gitignore）
├── flashwatch.py          闪窗排查工具（Windows，记录弹窗归属进程）
├── start.bat / set-static-ip.bat / restore-dhcp.bat
└── tunnel/                localtunnel 隧道（可选，公网访问，当前停用）
```

**分层规则（务必遵守）**：`api.py` 只做参数校验与响应拼装；每个业务模块自己管自己的 SQL；
`status.py` 必须是纯函数（它被单测 rely on，任何网络/配置访问都会破坏可测试性）。

---

## 4. 数据模型（SQLite）

### 4.1 `members` — 人员（通讯录同步的落地表）

| 列 | 型 | 说明 |
|---|---|---|
| `open_id` | TEXT PK | 飞书用户 open_id |
| `name` | TEXT | 姓名 |
| `department` | TEXT | 部门（`allow_departments` 鉴权靠它） |
| `email` | TEXT | 可空 |
| `scarf` | TEXT | 该成员围巾颜色（前端用，按 open_id 稳定哈希生成） |
| `doing` | INT | 冗余列，历史遗留，实际计数以 `tasks.summary_by_person()` 为准 |
| `overdue` | INT | 同上 |
| `status` | TEXT | `normal` / `away` / `leave`，由 `status.apply_all()` 写入 |
| `loc` | TEXT | **语义随状态变化**：`away` → 城市简称；`leave` → 假期类型；`normal` → NULL |
| `updated_at` | INT | 秒 |

### 4.2 `absences` — 不在岗记录（出差 / 请假 / 外勤共用一张表）

| 列 | 型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 审批实例 code |
| `open_id` | TEXT | 申请人 |
| `kind` | TEXT | `travel` / `leave` / `field` |
| `start_ts` / `end_ts` | INT | 秒级时间戳，闭区间判定 `start_ts <= now <= end_ts` |
| `city` | TEXT | 出差地点原始路径（`中国/浙江省/金华市/义乌市`），由 `status.short_place()` 压缩 |
| `reason` | TEXT | **`kind=leave` 时存「假期类型」**（年假 / 调休假 / 病假），不是申请人手写事由；`kind=travel` 时存说明 |
| `source` | TEXT | `approval`（默认） |
| `raw` | TEXT | 原始审批 JSON，**截断到 4000 字符**（会破坏 JSON 完整性，重解析要容错） |

> **`reason` 的语义变更（2026-11-12）**：旧版把 `leaveGroupV2` 控件里申请人手填的
> `reason`（如「治疗陪同」「家里有事」）写进这一列，导致看板和群推送泄露隐私。
> 现在只取控件里的 `name`（假期类型），事由在同步阶段丢弃、不落库。
> 你若要「恢复显示事由」，等于推翻一项已确认的隐私口径，不要擅自做。

### 4.3 `todos` — 待办（四个来源合表）

| 列 | 型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 前缀决定来源：`fs_`飞书任务 / `mn_`手工 / `im_`群消息 / `bt_`多维表格 |
| `open_id` | TEXT | 归属人 |
| `title` | TEXT | 标题，截断 200 字 |
| `status` | TEXT | `open` / `done` |
| `due_ts` | INT | 截止时间，可空；`due_ts < now AND status='open'` 即逾期 |
| `source` | TEXT | `feishu` / `manual` / `im` / `bitable` |
| `created_at` / `updated_at` | INT | 秒 |

**覆盖策略（关键）**：

| source | 同步方式 | 后果 |
|---|---|---|
| `feishu` | 每轮 `DELETE ... WHERE open_id=? AND source='feishu'` 再整批插入 | 飞书里完成/删除，看板自动消失 |
| `bitable` | 每轮 `DELETE ... WHERE source='bitable'` 再整批插入（全表） | 表格删行，看板消失；看板勾选必须写回表格，否则下轮被覆盖 |
| `manual` | 只增不减 | 长期保留，靠人自己勾 |
| `im` | `INSERT OR IGNORE`（id 即消息 id） | 天然幂等，不会把已勾掉的覆盖回未完成 |

### 4.4 `users` — 已授权成员（OAuth token 仓库）

`open_id` PK / `name` / `access_token` / `refresh_token` / `expires_at` /
`refresh_expires_at` / `authorized_at` / `last_sync_at` / `scope`

> `refresh_token` 是**一次性**的，刷新后必须立刻保存新的。`oauth.get_valid_token()` 已用锁串行化。
> cookie 里只有 `uid`，它证明「这个人走过授权」，不证明「token 还能用」——
> `api._identity()` 会同时校验 `expires_at` 与 `refresh_expires_at`。

### 4.5 `sync_log` — 同步日志

`id` / `ts` / `ok` / `scope` / `message`。`scope` ∈ `contacts`、`approvals`、`tasks`、
`bitable`、`im_todo`、`digest`、`notify`、`scheduler`、`post`、`sync`。
前端「数据源」状态和 `/api/health` 都读这张表的最后一条。

---

## 5. 数据流

```
        ┌── contacts.sync_members()      群成员 → members（含姓名/部门/围巾色）
        │
审批 ───┼── approvals.sync_absences()    应用身份一次拉全员；成功后不再走按人路径
        │   approvals.sync_all_authorized()  退化路径：每人用自己的 token 拉自己
        ↓
     absences ──→ status.apply_all() ──→ members.status / members.loc
        │                                      │
任务 ───┼── tasks.sync_all_authorized()         │
表格 ───┼── bitable.sync_records()              │
群消息 ─┴── im_todo.poll_commands()             │
        ↓                                       ↓
      todos ──→ tasks.summary_by_person() ──→ /api/board ──→ index.html
                                            └──→ digest.build_text() ──→ 群消息
```

**顺序不可调换**（`api.sync_all()` 里写死）：

```
contacts（人） → approvals（不在岗） → 审批退化路径（如需要）
              → tasks → bitable → im_todo → status.apply_all()
```

`status.apply_all()` 必须最后跑，它依赖前面所有结果。新人当天没被 contacts 同步到，
后续所有步骤都会漏掉这个人。

**一轮同步的完整链路**：`scheduler._loop()` 启动即跑一次 `api.sync_all()`，
之后每 `config.sync_minutes`（默认 5）分钟一次；同一轮内再判断 `digest.due_now()`。

---

## 6. HTTP 接口

鉴权：除 `OPEN_PATHS = {"/login", "/oauth/callback", "/api/health", "/api/digest", "/api/announce", "/demo"}`
外，一律要 `uid` cookie 且该 open_id 在 `users` 表、token 未双双过期；
`config.allow_departments` 非空时还会校验部门。

静态白名单 `STATIC_ALLOW = ("/index.html", "/favicon.ico", "/vendor/")`
——`config.json`、`board.db`、`app/`、`logs/` 一律 404，别为了让某个新资源能访问就放宽成整站静态。

| 方法 | 路径 | 入参 | 出参要点 |
|---|---|---|---|
| GET | `/api/board` | — | `{department, members[], updated_at, sync_ok, sync_message, approvals{}}`，前端唯一数据源 |
| GET | `/api/absences` | — | `{items[], count}`，最近 200 条，排查用 |
| GET | `/api/health` | — | `{ok, ts, contacts: 上条日志, approvals: 上条日志, approvals_configured}`，**匿名可访问** |
| GET | `/api/me` | — | `{ok, open_id, name, is_admin}` |
| GET | `/api/todos?open_id=` | query | 某人全部待办 |
| GET | `/api/users` | — | 已授权成员清单 |
| GET | `/api/config` | — | 全量配置，`app_secret` 打码为 `***` |
| GET | `/login` | — | 302 到飞书授权页，`scope=offline_access task:task:read`。**注意：README 提到的 `?force=1` 强制重授权在代码里并未实现**（`api.do_GET` 的 `/login` 分支不读该参数，`oauth.authorize_url()` 也不加 `prompt`），要重授权目前得清 cookie 或换账号 |
| GET | `/demo` | — | 演示页，无需登录，前端用 `DEMO` 常量假数据 |
| POST | `/api/sync` | — | 触发一次全量同步，返回各模块结果 |
| POST | `/api/todos` | `{open_id, title, due_ts?}` | 新增手工待办；**只能给自己或管理员名单内的人加** |
| POST | `/api/todos/toggle` | `{id, status?}` | 勾选/取消；`bt_` 开头的会写回多维表格；回传该人最新 `doing/overdue` |
| POST | `/api/todos/delete` | `{id}` | 删除 |
| POST | `/api/config` | 部分配置键 | 只覆盖传入且非空的键（`config.save()` 语义） |
| POST | `/api/digest` | `{force?}` | **仅 127.0.0.1 可调**，立即发群摘要 |
| POST | `/api/announce` | `{url}` | **仅 127.0.0.1 可调**，把地址推给 `notify_open_ids`（隧道遗留） |

`members[]` 单项结构（前端契约，改字段等于改前端）：

```json
{
  "open_id": "ou_xxx", "name": "张三", "role": "成员",
  "status": "normal|away|leave",
  "doing": 3, "overdue": 1,
  "scarf": "#06b6d4", "loc": null, "lead": false,
  "species": "ape|cat", "synced": true
}
```

---

## 7. 前端契约（index.html）

单文件，1420 行，无构建。分区顺序：CSS → 场景 SVG 构造函数 → 徽章/指标 → 卡片 →
轮询/渲染 → 待办抽屉 → boot。

### 7.1 六态是前端算的，不是后端给的

后端只给 `status` + `doing` + `overdue`，前端 `loadOf(m)` 推导：

| 卡片档位 | 判定 | 表现 |
|---|---|---|
| 空闲 `idle` | `normal` 且 `doing=0` | 慢呼吸 |
| 正常 `normal` | `normal` 且 1–4 件 | 常规节奏，偶尔转头 |
| 忙碌 `busy` | `normal` 且 ≥5 件 | 节奏加快 + 汗滴 |
| 超载 `overload` | `normal` 且 `overdue>0` | 最快节奏 + 冒烟 + 红边脉冲 |
| 出差 `away` | `status=away` | 空椅子 + 行李箱 + 屏幕 AWAY |
| 休假 `leave` | `status=leave` | 空椅子 + 枕头 + 屏幕 OFF |

`badge(m)` 的文字徽章与 `loadOf()` 的动画档位**必须一致**：
在岗 ≥5 件显示「忙碌」、有逾期显示「超载」，否则会出现「写着正常、小人在跑忙碌节奏」。

### 7.2 `applyBoard()` 是差量更新（最容易破坏的地方）

- 人数不变时**只改 class / 文案，绝不重建 SVG**——重建会让动画从头播。
- 档位是**渲染时烘进 SVG** 的（`.actor-back` 的 class、`.scene[data-load]`、
  `.screen[data-doing]`）。`patch()` 必须同时改这三处，否则「徽章变了、小人节奏没变」。
- 角色动效共用一套 `work` keyframes，换档只改 `animation-duration`，因此换档平滑不重播。

### 7.3 轮询与刷新

`POLL_MS = 10000`（10 秒）；`visibilitychange` + `focus` 立即补拉；
`stamp()` 用最近一次成功拉取时间 `LAST_OK`，超过两轮标黄。

### 7.4 待办抽屉

点卡片上的「在办 N」打开，读 `/api/todos?open_id=`；勾选调 `/api/todos/toggle`，
用回传的 `doing/overdue` 就地校正卡片，不重拉 `/api/board`。

### 7.5 演示模式

`/demo` 与 `isDemo()`：不走 `/api/board`，直接用页面内 `DEMO` 常量（九人构造数据，
覆盖六态）。生成 README 样图就是靠复制 `index.html` + 覆盖 `window.fetch`。

---

## 8. 配置项（config.json）

| 键 | 必填 | 说明 |
|---|---|---|
| `app_id` / `app_secret` | ✔ | 飞书企业自建应用凭据 |
| `department` | ✔ | 部门显示名，用于日报标题与默认鉴权 |
| `approvals[]` | ✔ | `[{code, kind, label, fields{}}]`，`kind` ∈ `leave`/`travel`/`field` |
| `im_todo.chat_id` | ✔ | 抽 `#待办` 命令的群；空则复用 `digest_chat_id` |
| `digest_chat_id` | ✖ | 日报推送群；空 = 不推 |
| `digest_hour` | ✖ | 默认 9。判定窗口是 `[hour, hour+2)`，错过就等明天 |
| `bitable` | ✖ | `{base_token, table_id, url}`；空 = 跳过多维表格源 |
| `allow_departments` | ✖ | 非空时只允许这些部门访问 |
| `todo_admins` | ✖ | 能代改他人待办的**姓名**名单 |
| `bootstrap_lead` | ✖ | 负责人姓名，排序置顶 |
| `species` | ✖ | `{姓名: "cat"|"ape"}`，角色形象 |
| `sync_minutes` | ✖ | 默认 5 |
| `oauth_scope` | ✖ | 默认 `offline_access task:task:read` |
| `notify_open_ids` | ✖ | 隧道地址变更通知对象 |

环境变量：`HOST`、`PORT`（默认 `0.0.0.0` / `8900`）；
`BOARD_NO_DIGEST=1` 让该实例闭嘴不发日报（云端与本机双跑时用）。

---

## 9. 已知坑与禁区

### 9.1 禁区（改动会直接推翻既有决策）

1. **不要把请假事由放回 `absences.reason` 或 `members.loc`**。2026-11-12 明确要求只推假期类型。
2. **不要放宽 `STATIC_ALLOW`**。它挡住 `config.json` 与 `board.db` 被 HTTP 直接下载。
3. **不要在 `_do_post` 里写 `deny = self._json(...)`**。`_json` 立即写响应头并结束请求，
   这么写会让整段路由一律 403（历史上踩过一次）。
4. **不要给 `api.py` 加业务逻辑**。SQL 放回各模块，`api.py` 只做校验与拼装。
5. **不要引第三方包**（见第 0 节）。

### 9.2 实测踩过的坑

| 现象 | 真因 |
|---|---|
| `refresh_token` 两小时就失效 | 授权 URL 没带 `offline_access`；该 scope 在开放平台后台勾不出来，只能显式传 |
| 通讯录报 `40004 no dept authority` | 数据范围只授权「人」时，部门接口要求「部门」也在范围内。本项目用群成员当名单绕过 |
| 计划任务过几分钟自己死掉（`0x8007042B`） | 计划任务默认「使用电池时停止」。已关闭两项电池限制 + 失败重启 3 次 + 每 5 分钟重复触发器当看门狗 |
| 每 5 分钟闪一次黑窗 | `python` 而非 `pythonw` 启动，或调 `lark-cli`（`.CMD`）漏了 `CREATE_NO_WINDOW` |
| 看板勾了待办，下轮又变回未完成 | 多维表格源整批覆盖。`bitable.update_status()` 已做写回，`bt_` 前缀才会触发 |
| 群里发 `#待办` 后机器人自己抓成真待办 | 只认 `sender_type in (None, "user")`，排除机器人自己发的教程消息 |
| 审批 `raw` 重解析报 `Unterminated string` | `raw` 入库时截断到 4000 字符，不是合法 JSON，解析必须 try/except |
| 页面「未连上后端」但 `/api/health` 正常 | 前端缓存；硬刷新。或直接用 `file://` 打开（此时 fetch 必然失败） |
| README 写 `/login?force=1` 可强制重授权，实际无效 | 代码未实现该分支，`oauth.authorize_url()` 也没有 `prompt` 参数。要修就改 `api.do_GET` 的 `/login` 分支 + `authorize_url()` |

### 9.3 数据一致性注意

- 审批撤销后本地记录**不会**自动删除（`sync_absences` 对已存在的实例直接 skip）。
  需要反向对账时要新写逻辑，不要以为「重跑同步」能修。
- 同理，改了 `_extract()` 的字段映射，**历史记录不会自动重算**——
  必须另写一次性回填脚本（参考 `_local/repair_leave_reason.py` 的做法：从 `absences.raw` 重解析）。
- `members.doing/overdue` 是冗余列，真实计数来自 `tasks.summary_by_person()`。

---

## 10. 常见需求怎么加（扩展指南）

| 需求 | 落点 |
|---|---|
| 新增一个待办来源（如 Jira / 禅道） | 新建 `app/xxx.py`，实现 `sync_xxx()` 往 `todos` 写，`id` 用新前缀（如 `jd_`）；在 `api.sync_all()` 里追加一行；在 `tasks.summary_by_person()` 无需改动（它按表统计） |
| 新增一种不在岗类型（如「外派」） | `status.py` 的 `AWAY_KINDS` / `LEAVE_KINDS` 加 kind；`config.approvals[].kind` 加映射；`digest.py` 的分组加一行 |
| 改群推送文案 | 只改 `app/digest.py` 的 `build_text()`。它不碰接口，改文案不影响前端 |
| 推到别的渠道（企微 / 钉钉 / webhook） | 新建 `app/notify_xxx.py`，在 `digest.send_daily()` 里并联调用；`notify.py` 只负责飞书，别把它改成万能网关 |
| 看板加一个新指标（如「本周完成数」） | 后端：`tasks` 加统计函数 → `/api/board` 的 `members[]` 加字段；前端：`metrics()` 渲染。**两处都要改**，否则前端读到 undefined |
| 新增页面 / 资源 | 放进 `STATIC_ALLOW` 白名单的精确路径，不要用 `"/"` 通配 |
| 加定时任务 | `scheduler._loop()` 里加一段 `try/except`（异常必须自己吞掉，否则整个调度线程死掉） |
| 改角色形象 | `index.html` 的 `LOOK` 表 + `actorFor()`；`species` 配置控制用 `ape` 还是 `cat` |

---

## 11. 改完怎么验证

```bash
# 1) 语法
python -m py_compile app/*.py server.py

# 2) 纯函数单测（不联网）
python -c "import sys; sys.path.insert(0,'.'); from app import status, im_todo; \
print(status.short_place('中国/浙江省/金华市/义乌市')); \
print(im_todo.parse_command('#待办 @张三 整理周报', 'ou_a', {'张三':'ou_b'}))"

# 3) 数据层验证（不发送任何消息）
python -c "import sys; sys.path.insert(0,'.'); from app import digest; print(digest.build_text())"

# 4) 服务健康
curl -s http://127.0.0.1:8900/api/health

# 5) 重启（Windows 计划任务部署）
schtasks /end /tn "StatusBoard" && schtasks /run /tn "StatusBoard"
```

**改完必须重启服务**：`pythonw` 进程已加载旧模块，不重启等于没改。
`status.apply_all()` 由调度器每 5 分钟跑一次，等不及就手动调一次。

**发布纪律**：改完先本地 `git commit`，**不要自动推 GitHub**（用户明确要求，
等他一句「上传」再推）。提交前必看 `git status --short`——
历史上 `git add -A` 把含真实姓名与城市的看板截图推上过公开仓库，只能删库重建。

---

## 12. 当前行为基线（2026-11-25）

- 人员：十余人，单部门；部门名、人名均为脱敏占位
- 摘要格式（实际输出）：

```
【示例部门 · 今日状态】
在岗 9 人 · 休假 2
休假：张三·年假、李四·调休假
在办：王五 6 件、赵六 1 件
```

- 「在岗人数」= 总人数 − 出差 − 休假；「在办」只列 `doing>0` 的人，降序，最多 5 个，
  超出显示「另有 N 人有在办」；逾期合计 >0 时追加一行 `⚠ 逾期合计 N 件`
- 假期只显示类型（年假 / 调休假 / 病假 / 福利假期 / 育儿假），**不显示事由**
- 出差显示城市简称（`浙江省/金华市/义乌市` → `浙江义乌`）
- 前端六态、10 秒轮询、差量更新三者已联动（改档位必须同步三处属性）

---

## 13. 给外部模型的协作约定

如果你要在这个项目上加需求，请遵守：

1. **先定位再动手**：任何改动先说明落在哪个文件、哪个函数、影响哪条数据流。
2. **复用优先**：`db.session()`、`config.load()`、`feishu.get_with_token()`、`tasks.summary_by_person()`
   已存在，不要重写一遍。
3. **给全量可运行代码**，不要伪代码、不要 `TODO`。
4. **注释写「为什么」而不是「做什么」**——现有代码风格如此，保持一致。
5. **中文注释与中文字符串**，变量/函数名用英文。
6. **改完自检**：边界条件（空数据、token 过期、raw 截断）、异常吞掉（调度线程不能死）、
   安全（新增接口默认要登录，本机专属接口要校验 `client_address`）。
7. **不要虚构**：本项目没有的东西（队列、缓存、ORM、CI、Docker）一律不要假设存在。

---

<sub>本文件由项目真实代码与运行数据整理，未含任何凭据、真实人员或内网地址。</sub>
