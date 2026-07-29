# Task 3 实现报告 — M2 撤单确认接入（pre_open + stop_loss + 熔断）

> 物理背景：spec M2 · [[qmt-live-smoke-findings]]。QMT `cancel_order` 调用后主推回报
> 延迟 1-2s，原撤单逻辑只数「发起撤单笔数」即返，不确认柜台是否真撤成 → 状态悬空
> （本地以为撤了、柜台没撤，敞口残留）。Task 2 已在 `QmtExecutionGateway` 产出
> `_confirm_cancelled`（轮询 query_orders 直到终态或超时），本 Task 3 把它接入
> `breaker.cancel_all_open_orders` 与三处撤单调用方。

## 改动概览

### 主线（已 merged 至 dbefffe4）

| # | 文件 / 位置 | 改动 | 物理意图 |
|---|-------------|------|----------|
| 1 | `trading/io/breaker.py` `cancel_all_open_orders` | 返回值 `int` → `{"cancelled", "unconfirmed"}`；鸭子类型 `getattr(gw, "_confirm_cancelled", None)` 调确认方法 | 撤单「发起」与「确认到终态」分离；未确认笔数显式暴露给调用方，杜绝状态悬空 |
| 2 | `trading/engine.py:530` `_pre_open` 撤昨日单 | 消费 unconfirmed 口径，`>0` 时 `logger.warning` 告警 | 开盘前撤昨日未成交单，若有未确认 → 显式告警让运维人工复核 |
| 3 | `trading/engine.py:870` `stop_loss_monitor` pending cancel_on | 撤单后调 `_confirm_cancelled`，确认成功才计 `n_pending_cancelled` | 涨幅兑现撤买单时，确认真撤成才计数，避免「本地计撤了、柜台仍 pending → 漏买变漏卖」 |
| 4 | 新测试 `tests/trading/test_breaker_cancel_confirm.py` | 3 用例：超时计 unconfirmed / 全确认 unconfirmed=0 / 无方法向后兼容 | 锁定 T3 行为契约 |
| 5 | `tests/trading/test_circuit_breaker.py` | 4 处断言适配 `int` → `dict` 返回值 | 既有测试跟随返回值结构变化 |
| 6 | `tests/trading/test_stop_loss_monitor_decide_exit.py` | `test_pending_cancel_on_during_wait` 显式 `gw._confirm_cancelled = AsyncMock(return_value=True)` | 锁定 pending cancel 确认闭环 |

### 本轮 review fix（I-1 / I-2 / M-1 / M-3）

| finding | 位置 | 修法 |
|---------|------|------|
| **I-1** 熔断路径 unconfirmed 静默 | `trading/engine.py:1256` `_post_close` 日内熔断撤单 | 取 `_cancel_all_open_orders` 返回值，`unconfirmed>0` 时追加 `logger.critical`（与 :1248 触发告警不重复，此条专报撤单质量口径） |
| **I-2** 5N 秒阻塞无标注 | `trading/io/breaker.py` docstring | 追加「阻塞上界」段：N 笔串行最坏 `5N 秒`（每单 timeout=5s），调用方需评估 N；pre_open/熔断 N 个位数可接受，N 上百需后台 task 或缩短 timeout |
| **M-1** 探针风格不统一 + MagicMock 陷阱 | `trading/engine.py:883` stop_loss pending cancel | `hasattr(gw, "_confirm_cancelled") + await gw._confirm_cancelled` → `getattr(gw, "_confirm_cancelled", None) + await _confirm(...) if _confirm else True`（与 breaker.py 同风格，规避 MagicMock 自动属性 hasattr 恒 True 但不可 await 的陷阱） |
| **M-3** 测试死代码 | `tests/trading/test_breaker_cancel_confirm.py:71` `test_cancel_all_backward_compat` | 删第一行 `gw = MagicMock()`（被下一行 `MagicMock(spec=[...])` 立即覆盖） |

## 测试

### 命令
```bash
F:/quanter/.venv310/Scripts/python.exe -m pytest \
  tests/trading/test_breaker_cancel_confirm.py \
  tests/trading/test_circuit_breaker.py \
  tests/trading/test_stop_loss_monitor_decide_exit.py \
  tests/trading/test_e2e_trading_flow.py -v
```

### 输出（fix 后）
```
tests/trading/test_breaker_cancel_confirm.py::test_cancel_all_counts_unconfirmed_on_timeout PASSED
tests/trading/test_breaker_cancel_confirm.py::test_cancel_all_confirmed_zero_when_all_terminal PASSED
tests/trading/test_breaker_cancel_confirm.py::test_cancel_all_backward_compat_without_confirm_method PASSED
tests/trading/test_circuit_breaker.py ... 9 用例 PASSED
tests/trading/test_stop_loss_monitor_decide_exit.py ... 8 用例 PASSED
  （含 test_pending_cancel_on_during_wait 验证 M-1 getattr 改动）
tests/trading/test_e2e_trading_flow.py ... 5 用例 PASSED

============================= 25 passed in 1.86s ==============================
```

零回归。重点：
- `test_pending_cancel_on_during_wait` 通过 → M-1 getattr 改动不破坏既有 pending cancel 闭环（该测试 :302 显式设 `gw._confirm_cancelled = AsyncMock(return_value=True)`，改后仍命中）
- `test_cancel_all_backward_compat_without_confirm_method` 通过 → M-3 删死代码不影响鸭子类型跳过分支

## Commit

### 主线（已在前序提交）
- `d793e217` feat(trading): M2 撤单确认闭环 `_confirm_cancelled`（轮询终态，超时告警）
- `dbefffe4` feat(trading): M2 撤单确认接入 pre_open+stop_loss（未确认不计成功）

### 本轮 fix
- branch: `fix/trading-execution-resilience`
- message: `fix(trading): T3 review findings（熔断unconfirmed告警+5N标注+探针统一+report重写+死代码）`
- files: `trading/engine.py`, `trading/io/breaker.py`, `tests/trading/test_breaker_cancel_confirm.py`, `.superpowers/sdd/task-3-report.md`

## 自审清单

- [x] **语言审查**：所有新增/修改代码块均带中文注释（含物理意图 + 风控拷问）
- [x] **反魔法审查**：未引入新依赖；仅消费 T2 产出的 `_confirm_cancelled` 与既有 logger
- [x] **边界审查**：
  - 熔断路径 unconfirmed>0 critical 告警（最致命路径不静默）
  - pending cancel 探针用 getattr 规避 MagicMock 自动属性陷阱
  - cancel_all docstring 显式标注 5N 秒阻塞上界，调用方据 N 自行评估
- [x] **回归审查**：25/25 passed，覆盖 4 个改动文件全部测试

## 风控拷问（Grill Me · M2 撤单确认特有风险）

1. **熔断 + 未确认双重灾难**：日内熔断已触发（敞口超阈）时，若撤单又有 unconfirmed，
   意味着「该堵的口子可能没堵上」。I-1 修法用 `logger.critical`（非 warning）追加
   口径，与 :1248 触发告警形成「触发 + 撤单质量」双层告警，运维一眼可见需立即
   人工查柜台真实持仓。不阻塞 emergency_halt（lock_down 仍执行），因为「锁定不再
   下单」与「撤单未确认」是两个独立维度，后者只能人工兜底。
2. **5N 秒阻塞 event loop**：pre_open/熔断 N 通常个位数（昨夜遗留 + 当日未成交），
   5N≈数十秒可接受；但若批量挂单后紧急停机 N 上百，串行 5N 秒会饿死行情/订单回调。
   I-2 docstring 已显式标注此约束，调用方（T8 健康守护 / 未来批量场景）需自行评估
   是否后台 task 化或缩短 timeout。本 task 不改串行结构（熔断场景串行更可控、日志可读）。
3. **MagicMock 自动属性陷阱**：M-1 原写法 `hasattr(gw, "_confirm_cancelled")` 对
   裸 MagicMock 恒 True（自动生成属性），但生成的属性不可 await → TypeError。
   真实 QmtExecutionGateway 挂的是 async method 不触发此 bug，但单测用 MagicMock
   时若忘记 spec 会爆。getattr 默认 None 规避此陷阱，且与 breaker.py 同风格统一。

## Concerns

无。Task 3 闭环，M2 撤单确认已接入三处撤单路径（pre_open / stop_loss pending /
日内熔断），unconfirmed 口径全链路消费。后续 Task 8（健康守护）需注意 5N 秒
阻塞上界标注；Task 9（钉钉 CRITICAL 告警接线）应把 I-1 的 critical 纳入推送通道。
