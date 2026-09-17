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
| 前端 | 总览 KPI、设备详情、校准历史、异常时间线、待处理问题、导入、任务监控 |

## 数据模型

```
devices 1───* calibrations        devices 1───* events
   │                                   │
   └────────* issues *─────────────────┘
                 │  issue_events (issues *─* events)
jobs（后台任务）   import_jobs（导入批次 + 逐行错误）   task_locks（单飞锁）
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
    devices.py  calibrations.py  events.py  issues.py（规则+状态机）  importer.py
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
