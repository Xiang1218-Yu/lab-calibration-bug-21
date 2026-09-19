# 设备校准与异常追踪系统 (CalTrack)

集中管理实验室多台设备的**校准记录**与**运行事件**，关联同一设备的多次校准，
自动识别连续异常并生成待处理问题，问题状态随后续校准结果自动流转。

* **零第三方依赖**：仅使用 Python 标准库（`http.server` + `sqlite3` + `threading`），
  前端为原生 HTML/CSS/JS（无构建步骤）。一条命令即可启动。

## 快速开始

```bash
python3 -m app.main serve
# 打开 http://127.0.0.1:8000
```

首次启动会自动建库（`data/caltrack.db`）并写入一份演示数据（4 台设备、
校准/事件历史、1 个待处理问题、1 个已自动闭环的问题）。

其他命令：

```bash
python3 -m app.main worker        # 只跑后台 worker
python3 -m app.main once          # 处理一次到期任务后退出
python3 -m app.main reseed        # 清空并重建演示数据
python3 -m app.main demo-import   # 入队一个示例导入任务（data/sample_calibrations.csv）
python3 -m unittest discover -s tests   # 运行测试
```

环境变量：`CALTRACK_HOST`、`CALTRACK_PORT`、`CALTRACK_DB`、
`CALTRACK_TASK_TIMEOUT`、`CALTRACK_TASK_MAX_RETRIES`、
`CALTRACK_ANOMALY_THRESHOLD`、`CALTRACK_ANOMALY_WINDOW_MINUTES` 等（见 `app/config.py`）。

## 功能对照

| 需求 | 实现 |
|---|---|
| 设备档案 | `devices` 表 + `/api/devices`（增/改/停用/搜索） |
| 校准记录 | `calibrations` 表 + `/api/calibrations`，按设备/结果/时间范围查询 |
| 运行事件 | `events` 表 + `/api/events`，按设备/等级/时间范围查询 |
| 处理状态管理 | `issues` 表，`open → monitoring → resolved` 状态机，可人工处理/重开 |
| 导入结构化记录 | CSV/TSV/JSON，字段校验、判重（库内 + 文件内）、逐行错误明细 |
| 连续异常自动生成问题 | 规则引擎 `consecutive_anomaly`（阈值/时间窗可配，规则可插拔） |
| 校准结果驱动问题状态 | `pass` 关闭 / `conditional` 转观察 / `fail` 保持或重开 |
| 后台任务：失败重试 | 指数退避重试，超过 `max_attempts` 标记 `failed` |
| 后台任务：超时恢复 | 心跳 + 陈旧 `running` 任务扫描，worker 崩溃后自动回收重试 |
| 后台任务：重复执行保护 | 任务级幂等键（部分唯一索引）+ 全局咨询锁（单飞） |
| 导入批次撤销 | 三类记录分类、两阶段预览/确认、事务+幂等+单飞锁、补偿工作清单 |
| 前端 | 总览 KPI、设备详情、校准历史、异常时间线、待处理问题、导入、任务监控 |

## 导入批次的可审计撤销与补偿

撤销不能简单 `DELETE FROM calibrations WHERE source='import:<id>'`：那会误删同内容
手工记录、无视已被后续状态依赖的记录、也无法恢复问题状态。系统把撤销做成一个
**两阶段、可审计、带补偿** 的流程（`app/services/undo.py`）。

### 三类记录分类（预览时判定，不触碰数据）

* **可直接删除 `deletable`（A）**：记录没有引起任何问题状态变化，也没有后续依赖，
  删除即可。
* **已被后续状态依赖 `state_dependent`（B）**：该记录写入后，设备问题又发生了
  *后续* 状态转换（人工处理/后续校准），盲目回滚会篡改历史。**记录保留**，生成
  待处理补偿项（`dependent_record` / `manual_twin`），撤销结果为 `partial`。
* **已影响问题状态 `state_driving`（C）**：该记录引起的转换仍是问题的**最新一次**
  状态。删除记录并按 `issue_transitions` 中保存的快照把问题**逆向恢复**到此前状态，
  同时写入一条 `action='restored'` 的补偿审计行。

删除谓词带 `source!='manual'` 防护：同内容手工记录永不删除（
`UNIQUE(device_id,content_hash)` 在写入侧兜底，分类器另有 `manual_twin` 防御分支）。

### 一并处理的副作用

* **导入重试历史**：每次尝试写入 `import_attempts`（含 running/succeeded/failed/
  cleaned）。重试前用 `attempt_cleanup` 只清理**上次失败尝试**写入的行并逆向其状态；
  已被后续依赖的转为 `retry_artifact` 补偿项，不再“清零重来”。
* **附件**（`attachments`）：批次产生的附件标记 `revoked` 并把物理文件移入
  `data/quarantine/`（可恢复），不硬删除；手工附件不处理。
* **通知**（`notifications`）：批次发出的状态通知未读则 `retracted`；**已读/已确认**
  的通知无法撤回，生成 `notification_read` 补偿项而不是假装成功。
* **问题状态**：见上；恢复前用 `WHERE id=? AND status=?` 守卫，状态若已漂移则转为
  `issue_state_moved` 补偿项。

### 事务 / 幂等 / 并发锁 / 权限

* **事务**：执行阶段整体包在一个 `BEGIN IMMEDIATE` 事务里（`db.transaction(immediate=True)`），
  嵌套服务调用不会中途 commit；文件移入隔离区在提交后进行，失败会补一条补偿项。
* **幂等**：提交支持 `X-Idempotency-Key`；重复提交返回同一撤销单（`replayed=true`）；
  补偿项靠部分唯一索引 `uq_comp_active` 去重。
* **并发锁**：每批次一把咨询锁 `undo:import:<job_id>`（跨线程/进程单飞），另加
  IMMEDIATE 事务与问题状态条件更新双重防护；批次处于 processing 时拒绝撤销（409）。
* **指纹防陈旧确认**：预览计算状态指纹 `fingerprint` 并返回 `confirm_token`；提交时
  在锁内重算指纹，不一致即拒绝（可 `force=true` 强制作废旧预览）。
* **权限确认**：身份/角色取自 `X-Actor` / `X-Role`（viewer/operator/admin），仅
  `admin` 有 `import:undo`；提交必须显式 `{"confirm": true, "confirm_token": ...}`。

### API

```
POST /api/imports/{id}/undo/preview     # 干跑：返回三类记录 diff、附件/通知、指纹、token
POST /api/imports/{id}/undo/commit      # body: {confirm:true, confirm_token, fingerprint?}
GET  /api/imports/{id}/undo             # 撤销结果与审计信息
GET  /api/compensations[?status=&import_job_id=&kind=]
POST /api/compensations/{id}/resolve    # 人工处理/忽略补偿项
GET  /api/notifications | POST /api/notifications/{id}/read
GET  /api/issues/{id}/transitions       # 问题状态机完整审计轨迹
POST /api/imports/{id}/attachments | GET .../attachments
```

撤销单状态：`previewed → superseded/running → completed | partial | failed`。
存在任何待处理补偿项时结果恒为 `partial`，**不会静默成功**；批次状态置为 `revoked`。

## 数据模型

```
devices 1───* calibrations        devices 1───* events
   │                                   │
   └────────* issues *─────────────────┘
                 │  issue_events (issues *─* events)
                 └─ issue_transitions（状态机审计/逆向日志）
jobs（后台任务）   import_jobs（导入批次 + 逐行错误）
   └─ import_attempts（每次重试的历史尝试）
attachments（批次/校准附件）   notifications（状态通知/撤回）
undo_batches（撤销单：预览/确认/结果）   compensation_items（待处理补偿）
task_locks（单飞锁，含 undo:import:<id>）
```

* **校准结果** `result`：`pass | fail | conditional`
* **事件等级** `severity`：`info | warning | critical`（`warning` 及以上算“异常”）
* **问题状态** `status`：`open（待处理） | monitoring（观察中） | resolved（已处理）`
* **任务状态**：`pending | running | retrying | success | failed | timeout`

判重键 `content_hash = sha256(device_id, calibrated_at, technician, measured_value)`，
库内以 `UNIQUE(device_id, content_hash)` 兜底。

## 状态流转

**问题由异常事件生成**：一台设备在 `ANOMALY_WINDOW_MINUTES`（默认 1440 分钟）内
累计 `ANOMALY_CONSECUTIVE_THRESHOLD`（默认 3）次 warning/critical 事件，且当前无
未关闭问题时，自动创建 `open` 问题；更多异常事件挂到同一问题并升级严重度。

**问题由校准结果驱动**（`app/services/issues.py: apply_calibration`）：

```
            连续异常事件                conditional
   (无问题) ───────────▶ open ─────────────────▶ monitoring
                          ▲  │                        │
                  fail    │  │ pass                   │ pass
        ┌─────────────────┘  ▼                        ▼
        │              resolved ◀─────────────────────┘
        │  (monitoring 时 fail 重开为 open)
        └── 新的连续异常会产生新问题
```

## 后台任务框架（`app/tasks/`）

* **耐久队列**：任务是 `jobs` 表中的一行；入队是事务性的。
* **重复入队保护**：可选 `idempotency_key` + 部分唯一索引，同一逻辑工作在
  未结束前只会有一条任务。
* **单飞执行**：领取任务是原子条件 `UPDATE ... WHERE status IN ('pending','retrying')`，
  只有一个 worker 胜出；导入类任务另持 `task_locks` 咨询锁，保证全局串行。
* **失败重试**：抛错 → `retrying` + 指数退避（5s、10s、20s…），耗尽后 `failed`。
* **超时恢复**：任务在子线程执行并周期心跳；`join(timeout)` 超时则置取消并重试。
  扫描发现心跳陈旧的 `running` 任务（worker 崩溃/卡死）会原子回收（凭旧锁令牌），
  安全地被任意 worker 重复调用。
* **重试安全**：导入任务在开始时清除上一次尝试写入的同批次记录，重试可得到
  确定一致的结果，不会重复计数。

## HTTP API 摘要

```
GET   /api/dashboard
GET   /api/devices[?q=&active_only=]      POST /api/devices
GET   /api/devices/{id}                   PATCH /api/devices/{id}
POST  /api/devices/{id}/active
GET   /api/calibrations[?device_id=&result=&start=&end=]   POST /api/calibrations
GET   /api/events[?device_id=&severity=&start=&end=]       POST /api/events
GET   /api/issues[?device_id=&status=&severity=]
GET   /api/issues/{id}                    POST /api/issues/{id}/resolve|reopen
GET   /api/timeline/{device_id}
POST  /api/imports?fmt=csv|tsv|json&auto_create=&sync=      (body 为文件内容)
GET   /api/imports | /api/imports/{id}
POST  /api/imports/{id}/undo/preview | /api/imports/{id}/undo/commit
GET   /api/imports/{id}/undo
GET   /api/compensations | POST /api/compensations/{id}/resolve
GET   /api/notifications | POST /api/notifications/{id}/read
GET   /api/issues/{id}/transitions
POST  /api/imports/{id}/attachments | GET /api/imports/{id}/attachments
GET   /api/jobs | /api/jobs/{id}
```

所有响应统一为 `{"data": ...}`，错误为 `{"error": "..."}`（4xx/5xx）。

导入 CSV 表头（大小写/中英文别名均可识别）：
`device_code, calibrated_at, result, technician, measured_value, nominal_value, tolerance, unit, notes`
结果值支持 `pass/fail/conditional` 及中文/别名（通过、合格、不合格、条件通过…）。

## 代码结构

```
app/
  config.py            配置与枚举（结果/等级/状态）
  db.py                SQLite 连接、建表、原语
  models.py            领域模型（Device/Calibration/Event/Issue）
  utils.py             时间戳、哈希、JSON
  services/
    devices.py  calibrations.py  events.py  issues.py（规则+状态机+审计）
    importer.py  attachments.py  notifications.py
    undo.py（撤销/补偿引擎）  attempt_cleanup.py（重试安全清理）
    compensation.py（补偿工作清单）  auth.py（角色/权限确认）
  tasks/
    locks.py           跨进程单飞咨询锁
    runner.py          队列、领取、重试、超时回收、worker
    import_task.py     异步导入任务
  api/
    server.py          路由 + JSON API + 静态文件
    seed.py            演示数据
  main.py              CLI 入口
web/                   index.html / styles.css / app.js（SPA）
tests/test_system.py   规则、状态机、导入、任务可靠性测试
```

## 扩展

* **新设备类型 / 事件规则**：在 `app/services/issues.py` 的 `RULES` 列表注册一个
  `fn(event) -> Optional[Issue]` 即可，状态机与前端无需改动。
* **新后台任务**：用 `@task("name")` 注册处理函数并 `runner.enqueue("name", payload)`。
* 时间戳统一存 UTC ISO-8601；服务端、worker 可多实例部署（SQLite WAL + 条件更新保证安全）。
