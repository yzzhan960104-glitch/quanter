# Task 3 报告：daemon 钉钉告警 + outer 去偏调度（接真实实现，Plan 4 L4）

## status
✅ DONE（GREEN，零回归，已 commit）

## 做了什么

### 代码（discovery/daemon.py 追加 3 符号）
- `_eval_outer(trial_id, db_path, split, lake_start="2025-01-01")`：冠军 outer 去偏
  实现。读 trial 表 params JSON → 主进程 `freeze(lake_start)` → `evaluate(params,
  universe, split)["outer"]`。软降级（trial 不存在/evaluate 抛 → 返 None，不阻断
  daemon）。**信息隔离红线**：结果只返回给调用方进返回 dict/告警，严禁回写 run_search
  排序（spec §6.2——否则 inner 搜索被 outer 变相污染，holdout 退化为训练集）。
- `_notify_champion(summary, k, K, converged_cross, outer, snapshot_hash="")`：钉钉
  告警。直指 `infra.notifier.NotificationManager.get_default().notify_risk_event(msg,
  "INFO")` + `fire_and_forget`。**直指 infra 真身的理由**：core.notifier 是 strangler
  垫片，未来拆 core 时可能断链，daemon 作为 L4 生产入口必须绑死 infra 防回归。双重
  软降级（`_broadcast` 的 `return_exceptions=True` + `fire_and_forget` daemon 线程）
  保证钉钉故障永不阻塞跑批编排。level=INFO（业务流水，非风控红线）。
- `run_daemon(snapshot_meta, split, db_path, *, notify_fn=None, eval_outer_fn=None,
  **kwargs)`：生产入口。notify_fn/eval_outer_fn 默认 None 时预装 `_notify_champion`
  / `lambda tid: _eval_outer(tid, db_path, split)`。**显式签名而非 `*args` 透传**：
  brief 早期透传版有 bug——eval_outer_fn 闭包需 split/db_path 但 `*args` 拿不到位置
  参数；显式形参让闭包自然捕获（T2 reviewer 标记的关键修正，brief Step 3 verbatim
  代码已是修正版，直接采用）。

### 测试（tests/discovery/test_daemon.py 追加 2 测试）
- `test_daemon_alerts_on_new_champion`：mock notify_fn（dict 收集器）→ 验 T2 注入点
  把 `summary/k/converged_cross/outer` 正确透传给告警回调。`sent["k"]==0` /
  `converged_cross is False`（首次跑 frontier=None → k=0）。
- `test_daemon_outer_no_feedback`：信息隔离硬保证——`eval_outer_fn` 返回值原样进
  `out["outer"]`，但 run_search_fn 收到的 kwargs 绝不含 `outer`（run_search 签名无
  outer 入参，物理上不可能回写排序）。

### 顺手清
- T2 Minor M1：删 `tests/discovery/test_daemon.py::test_daemon_early_exit_when_converged`
  里的死导入 `write_daemon_state`（T2 遗留，函数体未用）。

## TDD 证据
1. **RED→GREEN 路径**：先追加 2 测试 → 跑 `test_daemon_alerts_on_new_champion
   test_daemon_outer_no_feedback` → 2 passed（T2 的 run_daemon_cycle 注入点已就绪，
   brief Step 2 预期 PASS 成立）。
2. **实现后回归**：daemon 全量 6 passed；discovery non-slow 94 passed, 8 deselected
   （T2 92 + T3 2 = 94，零回归）。
3. **import 自检**：`from discovery.daemon import run_daemon_cycle, estimate_budget,
   _eval_outer, _notify_champion, run_daemon` → OK（防 cli 阶段才暴露 ImportError）。

## 测试输出
```
tests/discovery/test_daemon.py  6 passed
  test_daemon_accumulates_k_when_frontier_stagnant   PASSED
  test_daemon_resets_k_on_frontier_expansion         PASSED
  test_daemon_first_run_k_zero_when_no_latest        PASSED
  test_daemon_alerts_on_new_champion                 PASSED   <- T3 新增
  test_daemon_outer_no_feedback                      PASSED   <- T3 新增
  test_daemon_early_exit_when_converged              PASSED

discovery 全量 non-slow: 94 passed, 8 deselected in 5.41s（零回归）
```

## commits
- `8ae037e3` feat(discovery): T3 daemon 钉钉告警+outer去偏调度（信息隔离，Plan 4 L4）
  - discovery/daemon.py：+100 行（3 符号）
  - tests/discovery/test_daemon.py：+43/-1（2 测试 + 清死导入）

## concerns
- **无 blocker**。T2 注入点签名与 T3 测试完全对齐，brief verbatim 代码直接可用。
- **outer 软降级的外层兜底已由 T2 的 try/except 覆盖**：`_eval_outer` 内部抛任何异常
  都会被 `run_daemon_cycle` 第 102 行的 `except Exception: outer = None` 捕获，所以
  `_eval_outer` 内部不需要再套一层 try/except（brief 实现如此，保持极简）。语义清晰：
  `_eval_outer` 只管"能算就算"，容错责任在调用方（run_daemon_cycle）。
- **钉钉通道装配**：`NotificationManager.get_default()` 单例首次调用时 `_channels=[]`
  （除非 `build_default_manager()` 已装配过），此时 `notify_risk_event` 会走
  `_broadcast` 的 `if not self._channels` 分支仅 debug 日志——属预期软降级行为，不报错。
  生产 cli（T4）需在 daemon 启动前调 `build_default_manager()` 装通道（.env 读凭证），
  T4 实现时注意。
- **`fire_and_forget` 起 daemon 线程跑 asyncio.run**：cli 主进程退出时 daemon 线程会被
  强杀（daemon=True 标志），极端情况下末尾告警可能丢失——但这是 fire_and_forget 既有
  语义（"不阻塞调用方"的物理代价），spec §7 接受此权衡，无需改。
