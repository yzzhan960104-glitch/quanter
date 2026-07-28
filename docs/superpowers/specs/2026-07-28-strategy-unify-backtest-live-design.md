# 策略模块统一：回测/实盘 识别+执行 单源化（颈线法）

- **日期**：2026-07-28（v3 修订：纳入实盘执行范式统一 + 止盈触发物理边界）
- **分支**：master
- **状态**：待审（spec review gate · v3）
- **关联**：
  - `2026-07-22-layer2-decoupling-design.md`（§3.5 D2 compute 方案A——本 spec 收口其未完成的颈线法执行统一）
  - `2026-07-27-neckline-algorithm-fix-design.md`（R1/R2/R3 补丁式修复——本 spec 收敛成单函数）
  - `2026-07-28-live-readiness-design.md`（R-3 trailing 已接通 + P0-3 `_place_take_profit` tp1 分级缺口——本 spec 顺手修）
  - memory `data-lake-integrity-gap.md`（gate 下沉数据层）
- **范围**：颈线法识别+执行各收敛为单一纯函数，回测/实盘共用；消灭 trailing 数学双份 + cancel_on 双口径；**实盘 stop_loss_monitor 切 decide_exit** + tp 价格同源 + 补 pending cancel_on；下沉完整性 gate；废弃 caisen 遗产 check_exit；拆解 adapter。

---

## 1. 背景与现状诊断

### 1.1 宗旨（用户原话）

> 「主要的宗旨是可以把回测和实盘收成一套逻辑，不然维护两套总有无法对齐的地方。」

### 1.2 历史脉络

| 时间 | 事件 | 遗留 |
|---|---|---|
| 2026-07-22 layer2 | D2 确立 compute functional core，check_exit 单源 | 颈线法未接入，自带 simulate_exit |
| caisen 退役 | 颈线法成唯一策略 | check_exit 零调用方 |
| 2026-07-27 neckline-fix | R1/R2/R3 修 scan_live | 补丁式，未收敛单函数 |
| 2026-07-28 live-readiness | trailing 实盘侧接通（_evolve_trailing_stops） | simulate_exit 仍内联 trailing；_place_take_profit 单笔 tp2（P0-3 缺口） |

### 1.3 双轨/三轨精确诊断（master HEAD）

**识别层**：scan_at 调 detect（无 gate）vs scan_live 调 detect+cancel_on+突破过滤（`neckline_method.py:228-273`）。

**执行层**：

| # | 函数 | 问题 | 行号 |
|---|---|---|---|
| ① | `check_exit`（exit.py:79） | 零调用方，caisen v1 作废孤儿 | grep 无 `check_exit(` |
| ② | `simulate_exit`（backtest.py:96） | 回测真相；trailing 内联双份；cancel_on 用 high | trailing `:160-173`；cancel_on `:130` |
| ③ | `should_trigger_stop`（stop.py:141）+ stop_loss_monitor | 实盘止损；cancel_on 用 close | monitor `:628` 调 should_trigger_stop；scan_live `:257` close |
| ④ | `compute_stop_price`（stop.py:92） | trailing 纯函数，engine 已接，**simulate_exit 未调（内联）** | `_evolve_trailing_stops:822` 调；simulate_exit:160-173 内联 |
| ⑤ | `_place_take_profit`（engine.py） | 实盘止盈预挂限价，**单笔 tp2 全平，无 tp1 分级**（P0-3 缺口） | live-readiness spec 标 :1047 |

**两个"对不齐"铁证**：
- trailing 数学双份：simulate_exit:160-173 内联 vs compute_stop_price:111
- cancel_on 双口径：回测 simulate_exit:130 high vs 实盘 scan_live:257 close

**潜伏 bug**：backtest.py:435 main() 用 os.makedirs 但无 import os。

---

## 2. 目标与非目标

### 目标

1. **识别单源**：`detect_signal`（detect+R1 cancel_on+R2+突破过滤），scan_at/scan_live 共用（method_v0.py）
2. **执行单源（回测）**：`decide_exit`（分级止盈+cancel_on 撤单+trailing），simulate_exit 逐根调（strategies/neckline/execution.py）
3. **执行单源（实盘）**：`stop_loss_monitor` 切 decide_exit 的 STOP_LOSS/TIMEOUT 分支（替代 should_trigger_stop）；pending 期补 cancel_on 撤单
4. **trailing 数学收敛**：simulate_exit 内联 → 调 compute_stop_price；compute_stop_price 迁 strategies/neckline/
5. **cancel_on 口径统一**：decide_exit 内统一 close（D9）
6. **tp 价格同源**：_place_take_profit 的 tp1/tp2 从 decide_exit 同源 cfg 算；**顺手修 P0-3**（加 tp1 分级挂单，目前单笔 tp2 全平）
7. **gate 下沉**：data/integrity.filter_universe_by_continuity
8. **废弃 check_exit**：删函数；ExitDecision 契约迁策略层
9. **adapter 拆解删除**
10. **trailing 进 schema**：trailing_grace/step/floor 接进 NecklineConfig
11. **契约归位**：signal/schema 并入 strategies/neckline/

### 非目标（物理边界 · 显式 out of scope）

- **止盈「触发机制」完全统一**（D10 物理边界）：回测=算法判 high≥tp；实盘=柜台限价单撮合。两套物理机制，**不可同函数**。本 spec 统一止盈的「价格计算」（同 cfg）+「触发条件」（同 tp 价），**触发方式接受物理差异**（模拟 vs 撮合），非双轨缺陷。
- **scan_symbol 双轨合并**（follow-up，有测试守护）
- **live 上线**（本 spec 是 live 前置）
- **回测调优数据保鲜**：U3/U6 可能改非冠军回测口径（冠军 trailing 关=零影响），接受

---

## 3. 关键决策（刻碑 · v3）

| # | 决策 | 真实理由 |
|---|---|---|
| D1 | registry/base 保留 | 用户决策点1 |
| D2 | gate 下沉 data/integrity | 用户决策点2 |
| D3 | trailing 进 NecklineConfig | 用户决策点3 |
| D4 | 识别抽 detect_signal | 收敛 R1/R2 补丁 |
| D5 | 执行落 strategies/neckline/execution.py | 用户 Q2：执行属策略语义 |
| D6 | decide_exit + compute_stop_price 归 strategies/neckline/ | compute_stop_price 参数全颈线法概念；layer2 放 trading/compute 是中间态 |
| D7 | 冠军 trailing 关闭 = U3 零风险窗口 | grace=0/step=0 退化固定止损 |
| D8 | check_exit 废弃 | 零调用方 |
| D9 | cancel_on 统一 close 口径 | 实盘 T-1 无 high，close 因果可得 |
| **D10** | **止盈触发机制接受物理差异**（v3 新增） | 回测算法判 vs 实盘柜台撮合，物理不可同函数；统一价格+条件即可。强行统一（实盘废预挂改市价）劣化止盈质量（滑点+轮询延迟），不划算 |
| **D11** | **实盘 pending cancel_on 补齐**（v3 新增） | 挂单等待期 high≥cancel_on 撤单（对齐 simulate_exit:130），当前实盘缺这环 |
| **D12** | **stop_loss_monitor 保留 should_trigger_stop fallback**（v3 新增） | decide_exit 异常时降级，盘中关键路径不裸奔 |

---

## 4. 架构设计

### 4.1 识别单源：detect_signal（strategies/neckline/method_v0.py）
收拢 detect + R1 cancel_on（close，D9）+ R2 窗口已突破 + 当日突破过滤。scan_at/scan_live 共用。

### 4.2 执行单源：decide_exit（strategies/neckline/execution.py）
照 check_exit 范式（纯函数+frozen 值对象）。`decide_exit(state, bar, cfg) -> NecklineExitDecision`，优先级：pending→CANCEL_ON | holding→STOP_LOSS(trailing)>TP2>TP1>TIMEOUT。trailing 调 compute_stop_price。

### 4.3 trailing 数学收敛 + 迁移（D6）
compute_stop_price 迁 strategies/neckline/execution.py；trading/compute/stop.py 留 re-export 垫片；simulate_exit:160-173 内联 → 调 compute_stop_price。

### 4.4 gate 下沉（D2）
data/integrity.filter_universe_by_continuity；engine._eod 调 detect 前过滤；replay 同 filter。

### 4.5 adapter 拆解 + 目录
neckline_method.py 4 类职责各归其位（识别→method_v0、gate→data、编排→引擎/回测）。删 adapter，薄策略类移 strategies/neckline/strategy.py。

### 4.6 实盘执行统一（v3 新增）

**stop_loss_monitor 改造**（engine.py:544-649）：
- 现状：`:628` 调 `should_trigger_stop(price, sp)` 仅判跌破
- 改造：构造 state（从 position_book + plan）+ bar（从 quotes last_price + 当日 high/low）→ 调 `decide_exit`
- 按 NecklineExitDecision.action：CLOSE/STOP_LOSS → 发卖出单；CLOSE/TIMEOUT → 发卖出单（超时）；CANCEL → 撤 pending 挂单；HOLD → 跳过
- **fallback（D12）**：decide_exit 抛异常 → 降级 should_trigger_stop（不裸奔）

**_place_take_profit 价格同源 + 修 P0-3**（engine.py _place_take_profit）：
- tp1/tp2 价格从 build_orders 同源算（颈线+tp1_h_mult×H / 颈线+tp_h_mult×H，同 cfg）
- 加 tp1 分级挂单（tp1_portion 比例，目前单笔 tp2 全平）

**pending cancel_on 补齐**（D11）：
- 挂单等待期（pre_open 挂买单未成交期间），盘中监控 high≥cancel_on → 撤单（对齐 simulate_exit:130）

**bar 构造（实盘盘中）**：
- decide_exit 需 bar（high/low/close）；实盘盘中从 get_quotes 拿 last_price + 当日 high/low（xtdata 快照）凑 bar

---

## 5. 迁移策略（strangler · 逻辑零改动红线）

每步逻辑零改动（除 D9 cancel_on high→close）。**实盘侧改造（U6）是唯一引入新行为的部分**（切 decide_exit + 补 cancel_on + tp1 分级），需 dry_run + 模拟盘充分验证。每阶段过等价性 gate。

---

## 6. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | U3 改 simulate_exit 调 decide_exit，非冠军回测口径可能变 | golden 严守；冠军 trailing 关（R2）零影响 |
| R2 | trailing 收敛影响冠军 | grace=0/step=0 退化固定止损，零影响 |
| R3 | D9 cancel_on high→close 改回测口径 | close 更严（挡更多），偏保守；golden 验退化幅度 |
| R4 | decide_exit 状态机复杂 | 单测覆盖每个 (phase,bar,cfg)→reason |
| R5 | compute_stop_price 迁后 engine import 破 | stop.py 留垫片 |
| **R6** | **U6 实盘 monitor 切 decide_exit（盘中关键路径）** | **dry_run 先验 + 模拟盘跑 + should_trigger_stop fallback（D12）；漏止损/误止损=真金损失，最保守** |
| **R7** | **bar 构造（盘中 last_price→high/low/close）不准致 decide_exit 误判** | 当日 high/low 用 xtdata 快照（累积值）；close=last_price；单测覆盖 |

---

## 7. 测试策略

- **单测**：test_detect_signal / test_decide_exit（各分支）/ test_filter_universe_by_continuity / test_compute_stop_price_single_source / **test_stop_loss_monitor_uses_decide_exit（v3）**
- **等价性 gate**：A2 触发脚本信号一致；regression_neckline_golden 冠军不退化；全量 978 passed
- **实盘验证（v3 U6）**：dry_run 跑 _eod→计划→monitor（mock quotes）验证 decide_exit 链路；QMT 模拟盘跑 cancel_on 撤单 + tp1 分级

---

## 8. 验收标准

1. detect_signal 是识别唯一判定（scan_at/scan_live 共用）
2. decide_exit 是执行唯一判定（simulate_exit + **stop_loss_monitor** 都调）
3. simulate_exit 内联 trailing 删除，调 compute_stop_price
4. cancel_on 回测/实盘同一 close 口径（decide_exit 内）
5. compute_stop_price 在 strategies/neckline/，stop.py 垫片
6. gate 在 data/integrity
7. check_exit 删除，ExitDecision 契约在策略层
8. neckline_method.py 删除
9. NecklineConfig 含 trailing 3 旋钮
10. **_place_take_profit 挂 tp1+tp2 两张限价单（P0-3 修复），价格同源 decide_exit cfg**
11. **stop_loss_monitor 调 decide_exit（含 TIMEOUT/CANCEL），should_trigger_stop 仅 fallback**
12. 全量 978 passed + 冠军 golden 不退化 + 模拟盘 cancel_on/tp1 验证通过

---

## 9. 实现步骤（高层 · 详细 diff 见 plan）

| 阶段 | 内容 | gate | 风险 |
|---|---|---|---|
| **U1 契约归位** | signal/schema → neckline/ | e2e | 低 |
| **U2 识别统一** | detect_signal；scan_at/live/symbol 共用；cancel_on close | A2 信号一致 | 中 |
| **U3 执行统一（回测）** | decide_exit；simulate_exit 调它；trailing 收敛 | golden 不退化 | **高** |
| **U4 迁移+废弃** | compute_stop_price 迁；删 check_exit | 978 passed | 低 |
| **U5 gate+adapter+schema** | gate 下沉；删 adapter；trailing 进 schema；修 import os | 全量回归 | 中 |
| **U6 实盘执行统一（v3）** | stop_loss_monitor 切 decide_exit；_place_take_profit 价格同源+tp1 分级；pending cancel_on | dry_run + 模拟盘 | **最高** |

---

## 10. spec review 要点（v3 已对齐）

- Q1 check_exit 废弃 → D8 ✅
- Q2 执行归策略 → D5/D6 ✅
- Q3 回测变化容忍 → R1/R3 接受 ✅
- **Q4 实盘执行统一纳入** → D10（止盈物理边界）+ D11（pending cancel_on）+ D12（fallback）+ U6 ✅

spec 通过，plan 据此实现（含 U6 实盘执行统一 Task）。
