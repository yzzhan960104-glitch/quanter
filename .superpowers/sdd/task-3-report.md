# Task 3 实现报告 — gap4 接线（engine + __main__）

> 注：本报告覆盖旧的「Task 3 daemon 钉钉告警」报告（不同任务编号体系）。本次为 **gap4 position_book 接线 Task 3**。

## 改动概览（3 处 Modify）

| # | 文件 | 改动 | 行为 |
|---|------|------|------|
| 1 | `trading/engine.py` `_post_close` | 读账本 + 注入 local_positions | `position_book.get_local_positions()` → 传 `post_close(today, local_positions=...)`；空 dict 直传（不转 None） |
| 2 | `trading/engine.py` `_handle_order_update` | c 连后追加第四连 d | `if direction in ("BUY","SELL")` → `position_book.apply_fill(...)`；独立 try-except 软降级 |
| 3 | `trading/__main__.py` `_run_forever` | `eng.start()` 前插入 `init_db()` | `position_book.init_db()` 必须先于 cron 启动 |

## 测试新增（4 个，追加到 `tests/trading/test_engine.py` 末尾）

1. `test_post_close_reads_position_book` — 账本非空 → 注入 post_close
2. `test_post_close_empty_book_passes_empty_dict` — 账本空 → 传 `{}` 非 None
3. `test_handle_order_update_writes_book` — BUY 成交 → apply_fill 被调一次，方向 "BUY"
4. `test_handle_order_update_book_failure_soft_degrades` — apply_fill 抛异常 → 不阻断 a/b/c 三连

## TDD 证据

### Step 2 红灯（实现前）
```
tests/trading/test_engine.py::test_post_close_reads_position_book FAILED
  assert None == {'300001.SZ': 100.0}  ← _post_close 仍传 None
tests/trading/test_engine.py::test_handle_order_update_writes_book FAILED
  Expected 'apply_fill' to have been called once. Called 0 times.  ← 第四连未接
2 failed
```

### Step 6 绿灯（实现后 · 全量回归）
```
tests/trading/test_engine.py ... 18 passed   # 新 4 + 既有 14
```

### 跨测试文件回归（保险）
```
test_engine.py + test_engine_order_update_handler.py + test_engine_eod_injection.py
+ test_engine_stoploss_inject.py + test_main.py + test_position_book.py
==> 37 passed
```
- `test_engine_order_update_handler.py`（_handle_order_update 三连原测试）全过 → 第四连追加位置正确，未破坏 a 日志/b 通知/c 止盈语义
- `test_main.py`（__main__ import + callable）全过 → init_db 注入未破坏启动契约

## Commit

- hash: `0e1e25e3`
- branch: `master`
- files: 3 changed, 121 insertions(+), 1 deletion(-)
- message:
  ```
  feat(trading): gap4 接线 — _post_close 读账本 + _handle_order_update 写账本

  - _post_close 读 position_book.get_local_positions() 注入 local_positions（空 dict 直传）
  - _handle_order_update 第四连 apply_fill（BUY/SELL only，独立 try-except 软降级）
  - __main__ 启动期 position_book.init_db()
  ```

## 自审清单

- [x] **语言审查**：所有新增/修改代码块均带中文注释（含物理意图 + 风控拷问）
- [x] **反魔法审查**：未引入新依赖；仅调用既有 `position_book` 三个公开函数
- [x] **边界审查**：
  - `_post_close` 空 dict 直传（保守，对齐 spec —— broker-only drift 不漏报）
  - `_handle_order_update` 方向 None 不写账本（保守，对齐 c 连不挂止盈 —— 不猜方向误记）
  - 第四连独立 try-except（apply_fill 失败不阻断 a/b/c 三连 + 不冒泡到回调链调用方）
  - `__main__` init_db 在 eng.start() 之前（cron 启动即可能读写账本，建表必须先就绪）
- [x] **回归审查**：test_engine.py 18 + 跨文件 37 全过，零回归
- [x] **既有测试无回归**：test_engine_order_update_handler.py 三连原测试全过，证明第四连追加位置（c 连 try 块结束之后、方法 return 之前）未破坏既有 a/b/c 语义

## 风控拷问（Grill Me · gap4 接线特有风险）

1. **回调链路重入与部分成交**：第四连在 `if direction in ("BUY","SELL")` 块内，与 c 连 `_tp_placed` 幂等标记**完全解耦**——部分成交重推时，c 连因 `symbol in _tp_placed` 跳过（防超卖），但第四连仍每次执行（apply_fill 自身有 order_id UNIQUE 幂等约束，重复写会被 SQLite 拒，由 try-except 兜底软降级）。两层幂等机制独立，互不干扰。
2. **账本写失败不阻断实盘**：apply_fill 抛 `RuntimeError("db locked")` 等异常被独立 try-except 捕获，止盈仍正常挂——避免「账本坏了 → 止盈漏挂 → 持仓裸奔」级联灾难。test_handle_order_update_book_failure_soft_degrades 锁此契约。
3. **空账本 vs 跳过对账**：live 下账本空（如新部署首日未跑完整成交链路）但 broker 有持仓（外部单/手工单）时，`local={}` 直传让 reconcile 报 only_broker drift；若误转 None，post_close 内部走跳过分支 → 漏报 → 隐性敞口。test_post_close_empty_book_passes_empty_dict 锁此契约。

## Concerns

无。Task 3 接线闭环，e2e 第 3 步（本 task）已就位，可进入 e2e 第 4 步（review_report 已于 Task 2 完成，e2e 测试组装待后续任务）。
