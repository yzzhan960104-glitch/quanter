# Phase B2 L2 sid 自动轮换 实施计划（process-gateway-phase-b2-l2-sid-rotation）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **状态：PENDING——执行前完成 §0 裁定门**（spec §4.4 的三级自愈 L2）。

**Goal:** preferred sid 被占 / connect 返 -1 时，引擎自动轮换到「userdata 未占用」的 sid 并重连，成功后将实际 sid 写入 runtime SSoT（`logs/engine_session.json` + `state_store.account.session_id`），`.env` 保持 preferred 不变——把「session 漂移」从静默故障变成自愈事件。

**Architecture:** 在 `broker/qmt.py` connect 的 attempt 循环外新增「sid 轮换层」：扫描 `down_queue_win_*` 得到在用 sid 集合 → preferred 起有界递增找空闲 → 清理残留 → 重连；单实例锁仍以 preferred 为键（引擎身份），轮换只改 trader 会话 sid。

**Tech Stack:** Python 3.10、`broker/qmt.py`、`scripts/qmt_clear_session_lock.py` 扫描逻辑、`trading/single_instance.py`。

## Global Constraints

- 全中文注释；TDD；Windows 限制诚实标注。
- **不自动改 `.env`**（spec §4.4 原则）：轮换只写 runtime SSoT，preferred 不变。
- 轮换有界（默认 100 次）；全部失败 → L3 钉钉 + fail-closed。
- `QUANTER_TESTING=1` 时不触发轮换（测试用 mock）。

## 0. 裁定门（执行前必须确认）

| # | 问题 | 默认建议 |
|---|---|---|
| L1 | 轮换上限 | 100（preferred..preferred+99） |
| L2 | 是否允许跨到其它账号 sid 区间 | 不允许（同账号区间内轮换） |
| L3 | runtime SSoT 是否同步写 `state_store.account.session_id` | 是（观测对照） |
| L4 | 轮换成功是否告警 | INFO 日志；连续 3 次轮换成功升级 WARN |

## File Structure

| 文件 | 动作 |
|---|---|
| `broker/qmt.py` | connect 加 sid 轮换层 + `_find_free_session_id` |
| `trading/single_instance.py` | 锁键仍 preferred（不动） |
| `scripts/qmt_clear_session_lock.py` | 提取 `list_session_locks` 复用（或迁移共享） |
| `logs/engine_session.json` | 运行时实际 sid（新写入口） |
| `tests/trading/test_qmt_gateway.py` | 轮换单测 |
| `tests/trading/test_sid_rotation.py` | 新建：扫描/找空闲/写 SSoT |

---

## Task L2-1: 扫描在用 sid

- [ ] Step 1: 写失败测试——tmp userdata 放 `down_queue_win_123456` / `lock_down_queue_win_123458` → `_used_session_ids()` 返回 {123456, 123458}
- [ ] Step 2: 实现——复用 `qmt_clear_session_lock.list_session_locks` 逻辑（抽到 `broker/qmt.py` 或共享模块）
- [ ] Step 3: Commit `feat(qmt): L2-1 在用 sid 扫描`

## Task L2-2: 找空闲 sid + 轮换重连

- [ ] Step 1: 写失败测试——preferred 123459 被占 → `_find_free_session_id` 返回 123460；connect -1 后自动用新 sid 重试且成功
- [ ] Step 2: 实现——`connect` 在既有两轮 attempt 前/后加轮换层：首轮 preferred → -1 → 清理 → 轮换重试（有界）
- [ ] Step 3: 验证——`pytest tests/trading/test_qmt_gateway.py tests/trading/test_sid_rotation.py`
- [ ] Step 4: Commit `feat(qmt): L2-2 sid 自动轮换 + connect -1 自愈`

## Task L2-3: runtime SSoT 写入 + 观测

- [ ] Step 1: 写失败测试——轮换成功 → `logs/engine_session.json` 含 `{"preferred":123459,"actual":123460,"rotated_at":...}`；`state_store.account.session_id` 更新（L3 通过时）
- [ ] Step 2: 实现——`_write_runtime_session()`；supervisor `_read_runtime_session` 已就绪（B1）
- [ ] Step 3: Commit `feat(qmt): L2-3 实际 sid runtime SSoT`

## Task L2-4: 失败升级 L3

- [ ] Step 1: 写失败测试——轮换耗尽 → 钉钉 CRITICAL + 不裸连
- [ ] Step 2: 实现——`_alert_critical`（engine 通道）+ 保持 `_lock_down`
- [ ] Step 3: Commit `feat(qmt): L2-4 轮换耗尽 fail-closed + 告警`

---

## 验收

1. preferred 被占/connect -1 → 5 分钟内自动换 sid 恢复 live（无人工）。
2. `.env` 不变；runtime SSoT 与端点 `actual_sid` 可见。
3. 轮换耗尽 → CRITICAL + 拒绝 connect（不裸连、不撞其它 sid）。
4. 单实例锁仍以 preferred 为键，双引擎启动被串行化。
