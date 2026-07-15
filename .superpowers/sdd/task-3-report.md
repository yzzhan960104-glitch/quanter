# Task 3 报告：training_loop 状态机编排器（Spec 3 · 3/7）

## 状态
**DONE** — TDD RED→GREEN 全绿，3/3 测试通过（含并发时序），零回归。

## 交付物
- 新增 `caisen/training_loop.py`：`TrainingNotifier` Protocol + `LoopBusyError` + `TrainingLoopOrchestrator`（`start/stop/submit_review/start_daemon/stop_daemon` + `_step_once` 状态分派 + `_handle_running/_handle_analyzing/_handle_awaiting_review` + 内联 `_confirm`/`_apply_confirmed`/`_render_confirm`）。
- 新增 `tests/caisen/test_training_loop.py`：3 测试（核心动线 / concurrency 守卫 / CONFIRMING 累积 cfg 时序）。

## TDD 证据
- **RED（Step1→2）**：`test_start_runs_round_then_awaits_review` 先写 → `ImportError: cannot import name 'training_loop' from 'caisen'`（模块不存在）。
- **GREEN（Step3→4）**：实现后核心动线测试 PASS。
- **RED→GREEN（Step5→6→7）**：追加 concurrency + CONFIRMING 两测试，首跑即 PASS（import 在 Step3 已补齐）。`python -m pytest tests/caisen/test_training_loop.py -v` → **3 passed**。
- **回归**：`tests/caisen/test_training_{loops_db,analyzer,loop}.py` → **16 passed**（Task1/2 不回归）。
- **flaky 排查**：`test_confirm_rerun_accumulates_cfg` 连跑 5 次 + 常规跑均稳定 PASS（详见时序设计）。

## 状态机路径覆盖
| 状态转移 | 实现 | 测试覆盖 |
|---|---|---|
| IDLE → RUNNING | `_step_once` 内 `_prime_if_idle`（start 落 IDLE，首轮 daemon 点火） | test_start_runs（start 后 IDLE，_step_once 进 RUNNING） |
| RUNNING → 提交回测 → 轮询 SUCCESS → ANALYZING → AWAITING_REVIEW | `_handle_running`（create_task 写 PENDING + 轮询 get_task + 链式 `_analyze_and_await`） | test_start_runs（mock get_task 返 SUCCESS，全链路一拍完成） |
| RUNNING → 回测 FAILED/CANCELLED → AWAITING_REVIEW | `_handle_running` 失败分支 | （由 Task5 端到端覆盖，本 task 不展开） |
| ANALYZING → AWAITING_REVIEW（独立兜底） | `_handle_analyzing` 调 `_analyze_and_await` | （重启悬挂态兜底，逻辑同链路分支） |
| AWAITING_REVIEW → CONFIRMING → RUNNING（rerun 累积 cfg） | `_handle_awaiting_review` 阻塞等 ev → `_confirm`（parse+回显+等确认）→ `_apply_confirmed` | test_confirm_rerun_accumulates_cfg |
| concurrency=1 守卫 | `start()` 查 `list_active_loops` 非空 → `LoopBusyError` | test_start_rejects_second_active_loop |

## 时序设计（防 flaky）
`test_confirm_rerun_accumulates_cfg` 的并发唤醒靠两点保证稳定，**不靠加 sleep 硬等**：
1. **测试把 `_POLL_INTERVAL` monkeypatch 成 0.05s**——`_handle_awaiting_review` 与 `_confirm` 两段 `ev.wait(timeout=_POLL_INTERVAL)` 超时响应从 3s 降到 0.05s，唤醒→重等切换在 0.1s 量级完成。
2. **子线程两次 `submit_review` 之间 sleep 0.08s**——给主线程足够时间从第一次 wait 醒来→clear event→进 `_confirm`→到第二次 wait 阻塞点（0.08 > 主线程处理+0.05 wait 超时）。两次唤醒信号各落在对应 wait 窗口内，不串扰。

生产路径 `_POLL_INTERVAL=3.0` 不受测试 monkeypatch 影响（仅作用于该测试进程的模块常量）。

## 关键实现决策
1. **`_handle_running` 链式跑完一轮动线**（SUCCESS→ANALYZING→AWAITING_REVIEW 一气呵成，不拆跨 `_step_once` 周期）：回测成功的收尾分析+推报告是同一轮的原子动线，拆开只会制造 SUCCESS→ANALYZING 的无意义中态悬挂。抽 `_analyze_and_await` 公共方法供 RUNNING 链路 + ANALYZING 兜底分支共用。
2. **IDLE→RUNNING 在 `_step_once` 内联点火**（非 `start()`）：`start()` 只落 IDLE + 起 review event，状态推进统一收敛到 `_step_once`，保持 `start()` 是纯写库（concurrency 守卫已满足）。
3. **回测复用不碰 scheduler**：仅 `replay_tasks_db.create_task`（写 PENDING）+ `get_task`（轮询终态）。Spec1 `ReplayScheduler` 会 poll 到 PENDING 派发，本模块零耦合。
4. **kw 传 fields**：所有 `update_loop(loop_id, status=..., current_cfg=..., pending_review=...)` 均关键字传参（Task1 约定，防 "RUNNING" 被 path 吞掉静默 no-op）。
5. **CONFIRMING 内联 `_handle_awaiting_review`**：parse+回显+等确认同走 `ev.wait` 模型，`__STOP__` 哨兵区分 stop vs 审核文本。
6. **brief 补 import**：顶部 `import json` + `from caisen.replay_tasks_db import _now_iso`（brief Step3 代码漏，实现者按注意补齐）。

## 红线对齐（CLAUDE.md）
- 全中文注释讲"为什么"（IDLE 点火/链式动线/cfg 累积/摘要控量/两段式确认/哨兵唤醒等物理意图均落注）。
- 极简零新依赖（复用 `training_loops_db`/`replay_tasks_db`/`training_analyzer` + stdlib threading/json）。
- 边界：回测任务丢失/FAILED/CANCELLED/parse 失败/到 max_rounds 均显式处置（交人审或 STOPPED，不静默）。

## Concerns
- **reset/stop 分支无单测**：`_apply_confirmed` 的 reset（回 base_cfg）与 stop/max_rounds 分支由 Task5 端到端覆盖，本 task 按 brief Step5 的 3 测试为准（brief 未要求展开）。逻辑已审，但单测覆盖度偏薄——Task5 端到端一轮会一并验证。
- **生产 `_POLL_INTERVAL=3.0` 下 CONFIRMING 唤醒有 ≤3s 延迟**：钉钉回复→submit_review set event 立即唤醒（不等超时），仅 stop 后 wait 残留 ≤3s 才退出——可接受（人审场景无毫秒级要求）。

## Commits
（见提交：feat(training): Task3 loop 状态机+回测复用+notifier注入(concurrency=1)）

## 报告路径
`.superpowers/sdd/task-3-report.md`

---

## Fix 轮（review findings I1/I2/M1/M2）

基线（fix BASE）：`814aa07`。修复 reviewer 发现的 2 个 Important（状态机可控性硬缺陷 + 守卫测试缺位）+ 2 个 Minor（死参数 + TOCTOU 守卫瑕疵）。M3（`_last_report_summary` 脆弱胶水）按 triage 保持现状不动（修法跨 task，不在本 fix 范围）。

### I1（Important）: stop() 无法中断 RUNNING 态回测轮询
- **缺陷**：`_handle_running` 轮询循环只查 daemon 级 `_stop_flag`，但 `stop(loop_id)` 只 `update_loop(status="STOPPED")` + `_wake`——既不 set `_stop_flag`，`_wake` 的 review event 对正在 `get_task` 轮询的 `_handle_running` 无效。后果：长回测（几十分钟~几小时）时你钉钉喊停，DB 标 STOPPED 了但 daemon 仍死等回测自然终态；更糟 SUCCESS 时 `_on_round_success` 无视 STOPPED 继续 append_history + 推报告，污染已停 loop。
- **修法**：`_handle_running` 轮询循环入口加 DB stop 检查，与 `_handle_awaiting_review` 的 wait 循环对称：
  ```python
  while not self._stop_flag.is_set():
      cur = training_loops_db.get_loop(loop_id)
      if cur is None or cur["status"] == "STOPPED":
          return   # 你喊停了，放弃本轮回测轮询（不 append history、不推报告）
      task = replay_tasks_db.get_task(task_id)
      ...（原 SUCCESS/FAILED/CANCELLED 分支不变）
  ```
  stop 响应延迟 ≤ `_POLL_INTERVAL`（3s）。

### I2（Important）: 补 RUNNING 态 stop 中断测试
- **背景**：原 3 测均未覆盖 RUNNING 态 stop 中断（正因无测试 I1 漏网）。
- **修法**：新增 `test_stop_interrupts_running_round`：mock `get_task` 始终返 PENDING（模拟长回测不终态）→ 子线程 `stop(loop_id)` → 断言 `_handle_running` 在 ≤2×`_POLL_INTERVAL` 内退出 + DB status==STOPPED + history 未被 append（无 phantom）。`monkeypatch _POLL_INTERVAL=0.05` 控时序（同 CONFIRMING 测试范式）。

### M1（Minor）: 删 `_clock` 死参数
- **缺陷**：`__init__(self, notifier, clock=time.monotonic)` 存 `self._clock` 但全文无 `self._clock(` 调用——纯死代码，误导维护者。
- **修法**：删 `clock` 参数 + `self._clock = clock` 行；`import time` 全文仅 `_clock` 默认值用到（wait 用的是 `threading.Event.wait`，不依赖 time），连 `import time` 一起删。

### M2（Minor）: start() 守卫 TOCTOU
- **缺陷**：`start()` 在 `with self._lock` 内查 `list_active_loops()`，但锁在 `create_loop` 前释放。两并发 start 可同时过检查 → 两个 IDLE loop 并存 → 都被 daemon 点火成 RUNNING → 两个活跃 loop 同时回测。
- **修法**：把 `init_db` + `create_loop` 移进 `with self._lock:` 块内（check + create 原子）。`init_db` 幂等无副作用，移入锁内确保 create 前表一定就绪。

### 覆盖测试
`tests/caisen/test_training_loop.py::test_stop_interrupts_running_round`（新增，驱动 I1）。原 3 测零改动通过。

### 验证命令 + 输出
```
$ python -m pytest tests/caisen/test_training_loop.py -v
tests/caisen/test_training_loop.py::test_start_runs_round_then_awaits_review PASSED [ 25%]
tests/caisen/test_training_loop.py::test_start_rejects_second_active_loop PASSED [ 50%]
tests/caisen/test_training_loop.py::test_confirm_rerun_accumulates_cfg PASSED [ 75%]
tests/caisen/test_training_loop.py::test_stop_interrupts_running_round PASSED [100%]
============================== 4 passed in 1.86s ==============================

# 连跑 5 次确认时序测试无 flaky（_POLL_INTERVAL=0.05 控时序）：5/5 全绿，耗时 1.24~1.88s

$ python -m pytest tests/caisen/ -q
============================ 184 passed in 10.12s ============================
```
Task1/2/3 零回归（184 全绿）。

## Fix 轮 Commits
（见提交：fix(training): Task3 stop()中断RUNNING回测轮询+TOCTOU守卫+删_clock死参数）
