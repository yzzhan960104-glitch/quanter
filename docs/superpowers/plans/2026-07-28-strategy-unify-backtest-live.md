# 策略模块统一：回测/实盘 识别+执行 单源化 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** 落地 spec `2026-07-28-strategy-unify-backtest-live-design.md` v3——颈线法识别+执行各收敛为单一纯函数（回测/实盘共用），消灭 trailing 数学双份（`simulate_exit:160-173` 内联 vs `compute_stop_price`）+ cancel_on 双口径（回测 high vs 实盘 close），**实盘 stop_loss_monitor 切 decide_exit + tp 价格同源 + 修 P0-3 tp1 分级 + 补 pending cancel_on**，下沉完整性 gate，废弃 caisen 遗产 `check_exit`，拆解 adapter。

**Architecture:** 阶段 0 契约归位（signal/schema 搬 neckline/，机械低风险）→ 阶段 1 识别统一（detect_signal，scan_at/scan_live 共用）→ 阶段 2 执行统一-回测（decide_exit，golden 严守）→ 阶段 3 迁移+废弃（compute_stop_price 归策略层 + 删 check_exit）→ 阶段 4 gate 下沉 + adapter 瘦身 + schema 收尾 → **阶段 5 实盘执行统一（stop_loss_monitor 切 decide_exit + _place_take_profit 价格同源+tp1 分级 + pending cancel_on，最高风险，dry_run+模拟盘验证）**。

**Tech Stack:** Python 标准库 + pandas，pytest（`asyncio.run`，不加 mark.asyncio），TDD。测试用 `.venv310/Scripts/python.exe`。

**Spec:** `docs/superpowers/specs/2026-07-28-strategy-unify-backtest-live-design.md`（v2）

## Global Constraints

- **全中文注释**（CLAUDE.md）：What + Why（交易物理意图/风控红线/回测对齐缺口）。
- **pytest-asyncio strict**：async 测试用 `asyncio.run(...)`，不加 `@pytest.mark.asyncio`。
- **测试用 `.venv310/Scripts/python.exe`**（系统 python 缺 pandas/xtquant）。
- **strangler 红线①**：每步搬迁**逻辑零改动**（除明确标注的 D9 cancel_on high→close）；改路径留 re-export 垫片保调用点不破。
- **等价性 gate（每阶段必过）**：① A2 触发脚本（`trading/tools/trigger_eod_once.py`）前后信号一致 ② `backtest/tools/regression_neckline_golden.py` 冠军 outer calmar 不退化 ③ 全量 `978 passed` 不新增失败。
- **冠军 trailing 关闭 = U2/U3 零风险窗口**：`neckline_disc_20260725_25c602` grace=0/step=0 → trailing 收敛退化固定止损 → 冠军回测零影响（spec R2）。

---

## 阶段 0：契约归位（U1 · 机械低风险）

### Task 1: signal.py + neckline_schema.py 迁 strategies/neckline/

**Spec:** §2 目标 9 + §4.5 · **依赖:** 无 · **验收:** 3 处 import 改 path，全量测试零退化

**Files:**
- Move: `strategies/signal.py` → `strategies/neckline/signal.py`
- Move: `strategies/neckline_schema.py` → `strategies/neckline/schema.py`
- Modify: `trading/compute/plan.py:33`（`from strategies.signal import Signal` → `from strategies.neckline.signal import Signal`）
- Modify: `backtest/optimize/training_analyzer.py:99`、`data/price_loader.py:55`（`neckline_schema` → `strategies.neckline.schema`）
- Modify: `strategies/neckline_method.py:29,33`（adapter 内 import 改 `.neckline.schema` / `.neckline.signal`）
- Re-export 垫片：`strategies/signal.py`/`strategies/neckline_schema.py` 原路径留 `from strategies.neckline.signal import *`（过渡，阶段 4 删）

- [ ] **Step 1:** `git mv strategies/signal.py strategies/neckline/signal.py`；`git mv strategies/neckline_schema.py strategies/neckline/schema.py`
- [ ] **Step 2:** 改 5 处 import（plan.py / training_analyzer / price_loader / adapter 两处）
- [ ] **Step 3:** 原路径建 re-export 垫片（`from strategies.neckline.signal import Signal` 等）
- [ ] **Step 4:** 跑 `test_trading_plan*` + `test_e2e_eod_to_plan` + 全量回归
- [ ] **Step 5:** commit `refactor(strategies): signal/schema 归位 neckline 子包（U1）`

---

## 阶段 1：识别统一（U2）

### Task 2: 抽 detect_signal（method_v0）+ cancel_on 统一 close 口径

**Spec:** §2 目标 1/4 + §4.1 + D4/D9 · **依赖:** Task 1 · **验收:** detect_signal 各分支单测过

**Files:**
- Modify: `strategies/neckline/method_v0.py`（新增 `detect_signal`，收拢 detect + R1 cancel_on + R2 窗口已突破 + 当日突破过滤）
- Test: `tests/test_detect_signal.py`（新建）

**Interfaces:**
```python
def detect_signal(df_upto, id_cfg, exec_cfg, date) -> Signal | None:
    # ATR 全序列（窗口对齐）→ detect_neckline_method（含 R2）→ R1 cancel_on（close 口径，D9）
    # → 当日突破过滤（date == formed_at）→ 装配 Signal
```

- [ ] **Step 1: 写失败测试**
  - `test_detect_signal_normal`：标准颈线突破 → 返 Signal（含 entry=颈线+buy_limit_mult×ATR, atr, rr）
  - `test_detect_signal_cancel_on_close`：close ≥ 颈线+cancel_mult×H → 返 None（D9 close 口径）
  - `test_detect_signal_window_broken`（R2）：窗口内已突破 → None
  - `test_detect_signal_not_today`：formed_at != date → None
  - `test_detect_signal_atr_window_aligned`：atr 用 id_cfg["window"] 非写死 14
- [ ] **Step 2:** 在 method_v0.py 实现 `detect_signal`（从 adapter `neckline_method.py:218-298` 搬识别逻辑；cancel_on 改 close 口径 D9）
- [ ] **Step 3:** 跑测试 pass
- [ ] **Step 4:** commit `feat(strategies): detect_signal 识别单源（收敛 R1/R2 + cancel_on close 口径 D9）`

### Task 3: scan_at / scan_live 改调 detect_signal

**Spec:** §4.1 · **依赖:** Task 2 · **验收:** A2 触发脚本前后信号一致（识别等价 gate）

**Files:**
- Modify: `strategies/neckline_method.py` scan_live（:200-299 → 调 `detect_signal`，删内联识别段）
- Modify: `strategies/neckline_method.py` scan_at（:131-134 detect 调用 → 调 `detect_signal`；scan_at 仍保留 simulate_exit 模拟出场）
- Modify: `strategies/neckline/backtest.py` scan_symbol（:356 detect 调用 → 调 `detect_signal`，统一识别源；scripts 路径也单源）

- [ ] **Step 1:** scan_live 改调 `detect_signal(sym_df, id_cfg, exec_cfg, date)`，返回即 Signal list（删内联 cancel_on/突破过滤，已并入 detect_signal）
- [ ] **Step 2:** scan_at 的 detect 调用改 `detect_signal`（scan_at 拿 Signal 后仍喂 simulate_exit）
- [ ] **Step 3:** scan_symbol 的 detect 调用改 `detect_signal`
- [ ] **Step 4:** **A2 等价性 gate**——记录改前 `trigger_eod_once.py` 信号输出（标的/数量），改后再跑，对照（注：D9 cancel_on close 口径可能挡更多，信号数≤改前，预期内）
- [ ] **Step 5:** 跑 `test_neckline_recognition` + `test_engine_eod_injection`
- [ ] **Step 6:** commit `refactor(strategies): scan_at/scan_live/scan_symbol 共用 detect_signal（识别单源 U2）`

---

## 阶段 2：执行统一（U3 · 最高风险，golden 严守）

### Task 4: 抽 decide_exit + ExitDecision 契约（strategies/neckline/execution.py）

**Spec:** §2 目标 2 + §4.2 + D5/D6 · **依赖:** Task 1 · **验收:** decide_exit 各 (phase,bar,cfg)→reason 单测过

**Files:**
- Create: `strategies/neckline/execution.py`（`decide_exit` + `NecklineExitDecision` + 迁入 `ExitAction/ExitReason`）
- Test: `tests/test_decide_exit.py`（新建）

**Interfaces:**
```python
class NecklineExitDecision(frozen): action/reason/portion/new_stop
def decide_exit(state: dict, bar: dict, cfg) -> NecklineExitDecision
  # state: phase(pending/holding)+entry/stop/tp1/tp2/cancel_on/neckline/atr/holding_days
  # 优先级：pending→CANCEL_ON | holding→STOP_LOSS(trailing)>TP2>TP1>TIMEOUT
  # trailing: 调 compute_stop_price（Task 5 迁入同包）
```

- [ ] **Step 1: 写失败测试**（对照 `simulate_exit:125-199` 行为）
  - `test_decide_exit_cancel_pending`：pending 期 high≥cancel_on → CANCEL（注：cancel_on 入参用 close 预判已是 detect_signal 挡，此处 pending 留 high 兜底盘中）
  - `test_decide_exit_stop_loss_trailing`：holding 期 low≤stop（trailing 收紧后）→ CLOSE/STOP_LOSS
  - `test_decide_exit_tp2`：high≥tp2 → CLOSE/TAKE_PROFIT/portion=1.0
  - `test_decide_exit_tp1`：high≥tp1（tp2 未到）→ CLOSE/TAKE_PROFIT/portion=tp1_portion
  - `test_decide_exit_timeout`：is_last + 浮盈<threshold → CLOSE/TIMEOUT
  - `test_decide_exit_hold`：均未触发 → HOLD
- [ ] **Step 2:** 建 execution.py，从 `trading/compute/exit.py` 迁 `ExitAction/ExitReason`（check_exit 留着 Task 7 删）
- [ ] **Step 3:** 实现 `decide_exit`（逻辑零改动于 simulate_exit:125-199，只搬成纯函数）
- [ ] **Step 4:** 跑测试 pass
- [ ] **Step 5:** commit `feat(strategies): decide_exit 执行单源纯函数（照 check_exit 范式 D5/D6）`

### Task 5: simulate_exit 改调 decide_exit + trailing 内联改调 compute_stop_price

**Spec:** §2 目标 3 + §4.3 · **依赖:** Task 4 · **验收:** golden 冠军 outer calmar 不退化（执行等价 gate）

**Files:**
- Modify: `strategies/neckline/backtest.py` simulate_exit（:125-199 持有期循环 → 调 `decide_exit`；:160-173 内联 trailing → 调 `compute_stop_price`）
- Move: `compute_stop_price` 从 `trading/compute/stop.py:92` 迁 `strategies/neckline/execution.py`
- Modify: `trading/compute/stop.py`（删 compute_stop_price 定义，改 `from strategies.neckline.execution import compute_stop_price` re-export 垫片，保 engine.py:60 import）

- [ ] **Step 1:** compute_stop_price 迁 execution.py（与 decide_exit 同包，D6）
- [ ] **Step 2:** `trading/compute/stop.py` 留 re-export 垫片（engine.py:60 不破）
- [ ] **Step 3:** simulate_exit 持有期循环（:156-199）改调 `decide_exit(state, bar, cfg)`，按 action/reason/portion 推进 lot1/lot2 状态机
- [ ] **Step 4:** simulate_exit trailing 内联（:160-173）删除，decide_exit 内部调 compute_stop_price
- [ ] **Step 5:** **golden 等价性 gate**——`regression_neckline_golden.py` 跑改前（记录冠军 outer calmar 基线）→ 改后再跑，**冠军零退化**（R2）；非冠军若变（R1/R3 cancel_on 影响）记录幅度
- [ ] **Step 6:** 跑 `test_stop_loss`（compute_stop_price 仍可经垫片 import）+ `test_neckline_core`
- [ ] **Step 7:** commit `refactor(strategies): simulate_exit 调 decide_exit + trailing 收敛 compute_stop_price（执行单源 U3）`

---

## 阶段 3：迁移 + 废弃（U4）

### Task 6: 删 check_exit + ExitDecision 契约归位

**Spec:** §2 目标 6 + D8 · **依赖:** Task 4 · **验收:** check_exit 零残留，全量测试零退化

**Files:**
- Delete: `trading/compute/exit.py` 的 `check_exit` 函数（保留文件，因 ExitAction/ExitReason 已 Task4 迁走，exit.py 仅剩 check_exit → 整文件可删或留空）
- Modify: 测试文件删 caisen 专属 check_exit 测试
- Modify: `trading/compute/__init__.py`（删 check_exit re-export）
- Modify: docstring 残留（`backtest/__init__.py:31`、`backtest/replay.py:9-11` 提 check_exit 的过期注释）

- [ ] **Step 1:** grep `check_exit` 全项目确认只剩定义 + 注释 + 测试（零活跃调用）
- [ ] **Step 2:** 删 check_exit 函数 + 其单测 + `__init__` re-export
- [ ] **Step 3:** 清理 docstring 过期提及（replay.py:9-11、backtest/__init__.py:31、README.md:181）
- [ ] **Step 4:** 跑全量回归
- [ ] **Step 5:** commit `refactor(compute): 废弃 caisen 遗产 check_exit（D8，零调用方）`

---

## 阶段 4：gate 下沉 + adapter 瘦身 + schema 收尾（U5）

### Task 7: 完整性 gate 下沉 data/integrity

**Spec:** §2 目标 5 + §4.4 + D2 · **依赖:** 无 · **验收:** 策略层无数据质量代码，回测/实盘共用 filter

**Files:**
- Modify: `data/integrity.py`（新增 `filter_universe_by_continuity(universe, df_map, window, susp, trade_days) -> clean_universe`）
- Modify: `trading/engine.py` _eod（调 detect_signal 前先 filter universe）
- Modify: `backtest/replay.py`（replay 前调同一 filter，数据校验也单源）
- Modify: `strategies/neckline_method.py` scan_live（删 :200-216 内联 gate）

- [ ] **Step 1:** 写 `test_filter_universe_by_continuity`（漏采 symbol 过滤、干净 symbol 保留）
- [ ] **Step 2:** data/integrity 实现 filter 函数
- [ ] **Step 3:** engine._eod 调用（universe 过滤后再扫信号）
- [ ] **Step 4:** replay 调用（回测数据完整性同口径）
- [ ] **Step 5:** scan_live 删内联 gate（:200-216）+ `_ensure_integrity_cache`（:54-83）
- [ ] **Step 6:** 跑 `test_integrity*` + `test_engine_eod_injection` + A2 触发脚本
- [ ] **Step 7:** commit `refactor(data): 完整性 gate 上提 filter_universe_by_continuity（D2，策略层无数据代码）`

### Task 8: adapter 瘦身 + trailing 进 schema + 修 backtest import os

**Spec:** §2 目标 7/8 + §4.5 + D3 · **依赖:** Task 2/3/5/7 · **验收:** adapter 仅剩 Protocol 编排，全量回归

**Files:**
- Create: `strategies/neckline/strategy.py`（薄 NecklineStrategy 类：@register_strategy("neckline")，precompute/scan_at/scan_live/config_schema 全部委托 detect_signal/simulate_exit）
- Delete: `strategies/neckline_method.py`（adapter，4 类职责已拆解）
- Modify: `strategies/neckline/schema.py`（NecklineConfig 加 `trailing_grace/step/floor` 三字段，D3）
- Modify: `strategies/neckline/backtest.py:17-25`（补 `import os`，修 main():435 NameError）
- Modify: `backtest/worker.py:114`（改 import 新薄类路径）

- [ ] **Step 1:** schema.py NecklineConfig 加 trailing 3 旋钮（默认对齐 EXEC_DEFAULTS：grace=0/step=0/floor=0.5）+ ParamLab 反射测试
- [ ] **Step 2:** 建 strategies/neckline/strategy.py 薄类（scan_live 调 detect_signal、scan_at 调 detect_signal+simulate_exit、precompute 调 compute_atr）
- [ ] **Step 3:** registry 注册改新路径（`strategies/__init__.py` import 改 `.neckline.strategy`）
- [ ] **Step 4:** 删 `strategies/neckline_method.py`；worker.py:114 改 import
- [ ] **Step 5:** `strategies/signal.py`/`neckline_schema.py` 垫片删除（Task 1 过渡垫片）
- [ ] **Step 6:** 补 `backtest.py` import os（修 main 潜伏 bug）
- [ ] **Step 7:** 跑全量回归 + A2 触发脚本（最终验证 _eod → 计划 → 推送全链路）
- [ ] **Step 8:** commit `refactor(strategies): adapter 瘦身为薄 Protocol 类 + trailing 进 schema + 修 backtest import os（U5）`

---

## 阶段 5：实盘执行统一（U6 · 最高风险 · dry_run + 模拟盘双验）

> ⚠️ 盘中关键路径，漏止损/误止损=真金损失。should_trigger_stop fallback（D12）必须保留；dry_run + QMT 模拟盘双重验证方可标完成。

### Task 9: stop_loss_monitor 切 decide_exit + tp 价格同源 + pending cancel_on

**Spec:** §2 目标 3/6 + §4.6 + D10/D11/D12 · **依赖:** Task 4/5（decide_exit 就绪） · **验收:** dry_run mock 链路通 + 模拟盘 cancel_on/tp1 成交观察

**Files:**
- Modify: `trading/engine.py` `stop_loss_monitor`（:544-649，:628 `should_trigger_stop` → `decide_exit` + fallback）
- Modify: `trading/engine.py` `_stoploss`（:1354，构造 state from position_book+plan + bar from quotes）
- Modify: `trading/engine.py` `_place_take_profit`（加 tp1 分级挂单，价格同源 build_orders cfg · 修 P0-3）
- Test: `tests/trading/test_stop_loss_monitor_decide_exit.py`（新建）

**Interfaces:**
- stop_loss_monitor 内：state={phase:holding, entry, stop, tp1, tp2, cancel_on, neckline, atr, holding_days}（from position_book.avg_price + plan orders）+ bar={high, low, close}（from get_quotes last_price + xtdata 当日累积 high/low）→ `decide_exit(state, bar, cfg)` → 按 action 发单
- _place_take_profit：挂 tp1（tp1_portion 比例）+ tp2（剩余）两张限价卖单，价格=颈线+tp1_h_mult×H / 颈线+tp_h_mult×H（同 build_orders cfg，D10 价格同源）
- pending cancel_on：挂单等待期 high≥cancel_on → 撤买单（对齐 simulate_exit:130，D11）

- [ ] **Step 1: 写失败测试**
  - `test_monitor_stop_loss_via_decide_exit`：mock bar low≤stop → decide_exit 返 CLOSE/STOP_LOSS → 发卖出单
  - `test_monitor_timeout_via_decide_exit`：holding_days>max_holding + 浮盈<threshold → CLOSE/TIMEOUT → 发卖
  - `test_monitor_hold_when_no_trigger`：未触发 → HOLD → 跳过不发单
  - `test_monitor_decide_exit_fallback`（D12）：decide_exit 抛异常 → 降级 should_trigger_stop（不裸奔）
  - `test_place_take_profit_tp1_tp2_two_orders`：挂两张限价单（tp1 portion + tp2 剩余），价格同源 cfg
  - `test_pending_cancel_on_during_wait`：挂单等待期 high≥cancel_on → 撤买单
- [ ] **Step 2: 实现 bar 构造**（盘中 `get_quotes` last_price + xtdata 当日累积 high/low → bar dict；R7 防 bar 不准）
- [ ] **Step 3: stop_loss_monitor 改调 decide_exit**（构造 state+bar → decide_exit → 按 action 发单）+ try-except fallback should_trigger_stop（D12）
- [ ] **Step 4: _place_take_profit 加 tp1 分级**（价格同源 build_orders cfg，挂 tp1+tp2 两张限价卖单，修 P0-3）
- [ ] **Step 5: pending cancel_on 撤单**（挂单等待期监控 high≥cancel_on → 撤买单）
- [ ] **Step 6: dry_run 验证**（mock quotes 跑完整 monitor 周期：STOP_LOSS/TIMEOUT/HOLD/fallback 四路径 + tp1 分级挂单）
- [ ] **Step 7: QMT 模拟盘验证**（标的跑 cancel_on 撤单 + tp1 分级成交，观察对齐回测行为）
- [ ] **Step 8: commit** `feat(trading): 实盘 stop_loss_monitor 切 decide_exit + tp1 分级 + pending cancel_on（U6 实盘执行统一）`

**⚠️ 风控红线（R6/R7）：** 盘中关键路径；should_trigger_stop fallback 必留；bar 用 xtdata 当日累积 high/low（非单 tick 避免误判）；dry_run + 模拟盘双验。

---

## 收尾验收（全量 gate）

- [ ] **识别单源：** grep 确认 scan_at/scan_live/scan_symbol 均调 `detect_signal`，无内联 cancel_on/突破过滤
- [ ] **执行单源（回测+实盘）：** simulate_exit **与 stop_loss_monitor** 均调 `decide_exit`；trailing 内联删除，调 `compute_stop_price`
- [ ] **cancel_on 同口径：** decide_exit 内统一（close，D9）；pending 期实盘撤单补齐（D11）
- [ ] **compute_stop_price 归位：** 在 `strategies/neckline/execution.py`，`trading/compute/stop.py` 仅垫片
- [ ] **gate 下沉：** 策略层零数据质量代码
- [ ] **check_exit 删除：** grep 零活跃
- [ ] **adapter 删除：** `strategies/neckline_method.py` 不存在
- [ ] **tp1 分级（P0-3 修复）：** `_place_take_profit` 挂 tp1+tp2 两张限价单，价格同源 cfg
- [ ] **should_trigger_stop fallback（D12）：** decide_exit 异常时降级，盘中不裸奔
- [ ] **全量回归：** `.venv310/Scripts/python.exe -m pytest tests/ -q` → ≥978 passed，零新增失败
- [ ] **冠军 golden：** `regression_neckline_golden.py` outer calmar 不退化
- [ ] **A2 e2e：** `trigger_eod_once.py` 跑通，钉钉收到明日计划
- [ ] **实盘模拟盘（U6）：** QMT 模拟盘 cancel_on 撤单 + tp1 分级成交观察通过

## 回滚点

每 Task 独立 commit，任一阶段 gate 不过即 `git revert` 该 commit 回滚。
- **U3（Task 5）**：golden 冠军退化即 revert（预期冠军零退化，退化=decide_exit 实现 bug）
- **U6（Task 9）**：最高风险——模拟盘发现漏止损/误止损/重复挂单立即 revert；should_trigger_stop fallback（D12）保底不裸奔
