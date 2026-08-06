# Phase B2 miniQMT 看门狗 + 进程观测端点 实施计划（process-gateway-phase-b2-guard）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **状态：PENDING——执行前完成 §0 裁定门**（客户端启动路径需人工确认）。

**Goal:** miniQMT 客户端进程由独立看门狗保活（进程不在 → 拉起；在但未登录/陈旧 → 钉钉 WARN），并新增 `GET /api/v1/ops/processes` 一屏观测端点（引擎 pid/端口/锁/客户端/队列/网关态），消灭「网关静默断 9 小时」类不可见故障。

**Architecture:** `ops/miniqmt_guard.py` 复用 `ops/process_topology.py`（B1 已抽共享）+ `infra.notifier` 钉钉通道；端点复用 `trading_supervisor.status()` 并在 API 层组装队列大小/网关态。

**Tech Stack:** Python 3.10、FastAPI、apscheduler（可选 5min 循环）、`netstat`/PowerShell、`infra.notifier`。

## Global Constraints

- 全中文注释；TDD；Windows 限制诚实标注；不误杀（客户端在但未登录 → 只 WARN 不 kill）。
- **不假装活**：文件陈旧 ≠ 已登录，WARN 文案必须区分「进程在/未登录/陈旧」。
- `QUANTER_TESTING=1` 时 guard 与端点不触发真实拉起/告警。

## 0. 裁定门（执行前必须确认）

> **已裁定（2026-08-06 · 用户整体采纳默认）**：
> G1=`QMT_CLIENT_EXE` env（缺省从 QMT_USERDATA_PATH 反推安装目录）；
> G2=userdata 非空 + `down_queue_win_*` 存在 + mtime≤5min；
> G3=独立 5min schtasks `QuanterMiniQmtGuard`；G4=人工勾「自动登录」一次（SOP 文档）。

| # | 问题 | 默认建议 |
|---|---|---|
| G1 | XtMiniQmt.exe 绝对路径与启动参数（`linkMini`） | 从 QMT_USERDATA_PATH 反推安装目录或显式 env `QMT_CLIENT_EXE` |
| G2 | 登录就绪判据（哪些文件/目录算「已登录」） | userdata 目录非空 + `down_queue_win_*` 存在，mtime 阈值 5min |
| G3 | guard 调度方式 | 独立 5min schtasks（与 engine 解耦） |
| G4 | 自动登录配置 SOP 由谁执行 | 人工勾选一次「自动登录」，guard 只负责重启客户端 |

## File Structure

| 文件 | 动作 |
|---|---|
| `ops/miniqmt_guard.py` | 新建：探测/拉起/陈旧 WARN/队列兜底 |
| `presentation/server/api/v1/ops.py` | 新建：`GET /api/v1/ops/processes` |
| `presentation/server/main.py` | 挂载 ops router（require_write） |
| `ops/manage_ops_schtasks.py` | 注册 QuanterMiniQmtGuard 5min 任务 |
| `tests/ops/test_miniqmt_guard.py` | guard 单测 |
| `tests/presentation/test_ops_api.py` | 端点单测 |

---

## Task B2-1: guard 核心探测

- [ ] Step 1: 写失败测试——客户端进程不在 → `ensure_client()` 调 `Popen` 拉起；进程在但 userdata 陈旧 → 返回 WARN 文案不拉起
- [ ] Step 2: 实现——复用 `process_topology.client_status`；`is_stale()` 用 `_client_staleness_diag` 同源逻辑（broker/qmt.py）
- [ ] Step 3: 验证——`pytest tests/ops/test_miniqmt_guard.py`
- [ ] Step 4: Commit `feat(ops): B2-1 miniQMT guard 探测/拉起/陈旧判定`

## Task B2-2: 钉钉 WARN + 队列兜底

- [ ] Step 1: 写失败测试——陈旧 5 分钟 → `notify_risk_event(WARN)` 被调一次（节流）
- [ ] Step 2: 实现——复用 `infra.notifier` `fire_and_forget`；残留 `down_queue_win_*` 清理（复用 `broker.qmt._cleanup_session_files` 语义）
- [ ] Step 3: Commit `feat(ops): B2-2 guard 陈旧 WARN + 队列兜底`

## Task B2-3: /api/v1/ops/processes 端点

- [ ] Step 1: 写失败测试——`GET /api/v1/ops/processes` 返回 status 字段（port_holder_pid/pid_file_pid/lock_held/engine_pids/client/queue_size/gateway_mode/git/started_at）
- [ ] Step 2: 实现——router 调 `trading_supervisor.status()` + 队列大小扫描 + `get_status()` 网关态；main.py 挂载
- [ ] Step 3: 验证——`pytest tests/presentation/test_ops_api.py tests/test_trading_api.py`
- [ ] Step 4: Commit `feat(api): B2-3 /api/v1/ops/processes 进程拓扑一屏视图`

## Task B2-4: guard 调度注册 + SOP

- [ ] Step 1: `manage_ops_schtasks` 注册 `QuanterMiniQmtGuard`（5min，复用 A4 PowerShell 范式）
- [ ] Step 2: runbook 增「客户端自动登录配置 SOP」（G4）
- [ ] Step 3: Commit `feat(ops): B2-4 guard 5min 调度 + 自动登录 SOP`

## Task B2-5: 引擎失踪自愈 + 失败告警（2026-08-06 code-review 后正式纳入）

> 背景：08-06 引擎多次被外部终止（无 traceback），schtasks RestartOnFailure 因权限未注册；
> guard 顺带检查引擎，失踪即 `schtasks /Run` 拉起（≤5min 自愈），拉起失败必须 CRITICAL 钉钉。
> 已实现：commit 366d7c57（自愈）+ 告警腿（`run_once` CRITICAL）。

- [x] Step 1: `ensure_engine`（8000 无监听 → schtasks /Run；env `QUANTER_GUARD_DISABLE_ENGINE=1` 维护期关闭）
- [x] Step 2: 拉起失败 → `notify_risk_event(CRITICAL)`（不静默）
- [ ] Step 3: 验收——引擎缺失 ≤5min 拉起；拉起失败钉钉 CRITICAL

---

## 验收

1. 客户端进程不在 → 5 分钟内拉起（guard 日志 + 端点可见）。
2. 在但未登录/陈旧 → 5 分钟内钉钉 WARN（不假装活）。
3. `/api/v1/ops/processes` 一屏含进程拓扑 + 网关态 + 队列大小。
4. `QUANTER_TESTING=1` 下 guard/端点不触真实拉起与告警。
