# Task 9 Report: stop_loss_monitor 切 decide_exit + tp 价格同源 + pending cancel_on（U6 · 最高风险）

**分支:** fix/strategy-unify-backtest-live · **基线:** 951f6986（Task 8，1001 passed）
**结果:** 1007 passed（+6 新测试，零回归）· **风控红线全守住**

---

## 1. 完成项

### 1.1 stop_loss_monitor 切 decide_exit（resolution 2 · 主路径）
`trading/engine.py:604` `stop_loss_monitor` 新增 `monitor_ctx` 参数（主路径）：
- 每持仓标的构造 state（phase=holding, stop/tp1/tp2/neckline/atr from plan, holding_days from position_book.entry_date, is_last=holding_days≥max_holding, lot1/lot2_open=True）+ cfg（stop_atr_mult/trailing_grace/step/floor/tp1_portion/max_holding from _trade_cfg）+ bar（high/low/close from get_quotes tick 当日累积）→ decide_exit(state, bar, cfg) → 按 action 分发：
  - CLOSE/STOP_LOSS | CLOSE/TIMEOUT | CLOSE/TAKE_PROFIT（portion≥1 全平）→ 发卖出单
  - CLOSE/TAKE_PROFIT portion<1（tp1）→ 按 portion 发部分卖单（对齐回测语义）
  - HOLD → 跳过不发单
- strangler 等价：decide_exit STOP_LOSS 分支 low≤compute_stop_price 等价 should_trigger_stop（price≤stop）。

### 1.2 D12 fallback（resolution 2 · 风控红线 · 盘中不裸奔）
try decide_exit except → 降级 should_trigger_stop(price, sp)。sp 来源优先 monitor_ctx.state.stop > stop_prices[sym]。fallback_used 返回字段 + logger.exception 告警。

### 1.3 R7 bar 防御（resolution 3/5 · 防漏判误判）
bar.high/low 优先 xtdata 当日累积（get_quotes tick.high/low，get_full_tick 开盘至当前累积），非单 tick last_price。close=last_price。缺失/NaN → 回退 last_price + 告警。

### 1.4 pending cancel_on（resolution 4 · D11 · 新增）
- 落盘：PlannedOrder.cancel_on（plan.py）+ build_orders 用 stop_cfg["cancel_thresh_mult"] 算 cancel_on=颈线+cancel_thresh_mult×H（对齐 simulate_exit:128-129）+ _eod order_dicts 落盘 + _trade_cfg 加 cancel_thresh_mult（env 默认 0.75 对齐 NecklineConfig EXEC）。
- 盘中撤单：stop_loss_monitor 新增 pending_ctx + gw.query_orders(cancelable_only=True) → 匹配 order_type==STOCK_BUY + day_high≥cancel_on → gw.cancel_order。三重过滤防误撤。

### 1.5 _stoploss 构造三 map 注入（resolution 3/6/7）
从同一张 confirmed 计划 orders + position_book.entry_dates + _trade_cfg 派生 stop_prices（D12 fallback）+ monitor_ctx（主路径）+ pending_ctx（D11）。缺 neckline/atr/tp2 老 order → 只塞 stop_prices 走 fallback。

### 1.6 _place_take_profit 价格同源验证（resolution 1 · 验证不重写）
**结论：已同源，不重写。** _place_take_profit 两腿 Task 1/8 已实现，价格来自 plan tp1/tp2；build_orders 用 stop_cfg tp1_h_mult/tp_h_mult 算；simulate_exit 用 exec/id_cfg 同 mult（默认 1.0/2.0）。test 验证 build_orders 算 tp1=12.0/tp2=14.0 == _place_take_profit 挂单价（无漂移）。

---

## 2. TDD RED → GREEN

**测试文件:** tests/trading/test_stop_loss_monitor_decide_exit.py（6 测试）

| 测试 | 路径 | GREEN |
|---|---|---|
| test_monitor_stop_loss_via_decide_exit | low≤stop → CLOSE/STOP_LOSS → 发卖 | ✅ |
| test_monitor_timeout_via_decide_exit | holding_days≥max_holding → is_last → CLOSE/TIMEOUT → 发卖 | ✅ |
| test_monitor_hold_when_no_trigger | low>compute_stop_price 且 high<tp1 且非 is_last → HOLD | ✅ |
| test_monitor_decide_exit_fallback（D12） | decide_exit 抛异常 → 降级 should_trigger_stop | ✅ |
| test_place_take_profit_tp1_tp2_two_orders_and_same_source | tp1/tp2 同源 build_orders cfg | ✅ |
| test_pending_cancel_on_during_wait（D11） | pending 期 high≥cancel_on → 撤买单 | ✅ |

**关键测试修正（self-review 捕获）：** decide_exit 内部用 compute_stop_price 重算 trailing stop（不读 state.stop）。HOLD 测试原构造 stop=9.0/low=9.5 误判——实际 compute_stop_price(10,0.5,3,1.0,5,0.1,0.5)=9.5，low=9.5≤9.5 触发 STOP_LOSS。修正 low=9.6 才 HOLD。

---

## 3. dry_run 结果（mock 四路径 + tp1 + pending cancel_on）

- STOP_LOSS：low=9.3 ≤ stop=9.5 → decide_exit CLOSE/STOP_LOSS → 卖 100 股 ✅
- TIMEOUT：holding_days=15≥max_holding → is_last → CLOSE/TIMEOUT → 卖 200 股 ✅
- HOLD：low=9.6>stop=9.5, high=10.2<tp1=11.0, 非 is_last → HOLD → 不发单 ✅
- D12 fallback：decide_exit 抛 RuntimeError → should_trigger_stop(9.4,9.5)=True → 卖 100 股，fallback_used=1 ✅
- tp1 分级：_place_take_profit 挂 tp1=12.0(500股)+tp2=14.0(500股)，同源 build_orders ✅
- pending cancel_on：high=11.8≥cancel_on=11.5 → cancel_order，pending_cancelled=1，未发卖单 ✅

---

## 4. 模拟盘验证（弹性 · 留 controller/用户实盘前验证）

**留 controller/用户实盘前验证。** 本环境无 QMT 连接/无模拟盘账号/无 xtdata 通道，按 resolution 8 弹性原则不硬跑。dry_run 已证逻辑正确，D12 fallback 保底不裸奔。

实盘前验证清单：1) QMT 模拟盘 cancel_on 撤单 + tp1 分级成交观察；2) xtdata 当日累积 high/low 实盘有效性；3) gw.query_orders order_type==STOCK_BUY 匹配确认；4) ≥5 日影子模式后切 live。

---

## 5. 全量回归

1007 passed（基线 1001 + 6 新测试），零新增失败，零回归。

---

## 6. 风控核对（R6/R7 · 真金损失红线）

| 红线 | 守住 | 证据 |
|---|---|---|
| D12 fallback 不裸奔 | ✅ | try decide_exit except → should_trigger_stop 兜底；fallback_used + 告警 |
| R7 bar 防御 | ✅ | bar.high/low 优先 xtdata 当日累积，缺失回退 last_price + 告警 |
| 止损判定等价 | ✅ | decide_exit STOP_LOSS low≤compute_stop_price 等价 should_trigger_stop |
| qty 真实持仓 | ✅ | 卖出 qty 来自 gw._fetch_broker_positions volume（scope #3） |
| pending 不误撤 | ✅ | 三重过滤：cancelable_only + STOCK_BUY + day_high≥cancel_on |
| 人审闸 | ✅ | 仅 confirmed 计划构造三 map |

---

## 7. Self-review

- D12 fallback 守住：decide_exit 抛异常 → dec=None → 落 should_trigger_stop 分支。HOLD/CLOSE continue 跳过 fallback。✅
- R7 bar 防御：xtdata 当日累积优先，缺失回退 + 告警。✅
- 止损等价：decide_exit 用 compute_stop_price 动态 trailing，fallback 用静态 stop_price（plan 盘后演进）。主路径动态更精确，fallback 静态保守降级。✅
- is_last 不判浮盈：holding_days≥max_holding 即 TIMEOUT（resolution 6）。✅
- 价格同源：build_orders/simulate_exit/_place_take_profit 三处同 mult，test 验证无漂移。✅

---

## 8. Concerns（交 controller）

1. **state.entry=None**：decide_exit holding 分支不读 entry，monitor 只决策发不发单。若未来需浮盈判断需从 position_book.avg_price 注入。
2. **lot1/lot2_open 默认 True**：monitor 不维护 lot 翻转（_place_take_profit 限价单成交翻 lot）。若 tp1 限价单已成交但 monitor 仍 lot1_open=True，decide_exit 可能重复触发 tp1 portion<1 → 发部分卖单。**实盘前确认**：qty 来自 gw 真实持仓（tp1 成交已减），不会超卖；但部分卖单可能重复。缓解：实盘 tp1 走 _place_take_profit 限价单，monitor tp1 分支是兜底；若担心重复可在 monitor 据 position_book 判 lot1_open（follow-up）。
3. **order_type==STOCK_BUY 硬编码 23**：lazy import xtconstant 失败兜底 23（与 engine.py:1655 既有兜底同值）。
4. **模拟盘未跑**：环境无 QMT，留 controller/用户实盘前验证。

---

## 9. 回滚点

Task 9 独立 commit。模拟盘若发现漏止损/误止损/重复挂单立即 git revert。D12 fallback 保底不裸奔。

---

## Fix: I-1/I-2/I-3/I-4（2026-07-29 · reviewer 4 Important 修复）

### 修复范围
reviewer 审 Task 9 发现 4 个 Important（无 Critical，风控红线已守住），盘中关键路径谨慎修复。

### I-1（必修·盘中关键）：monitor TAKE_PROFIT skip（方案 A，交预挂限价单·D10）

**事实**：stop_loss_monitor 对 decide_exit 的 CLOSE/TAKE_PROFIT 分支发市价卖单，与 _place_take_profit 预挂的 tp1+tp2 限价单重复（tp1 限价单成交后 monitor state lot1_open 不翻转 → 下巡检 decide_exit 再返 TP1 → 再发市价部分卖单 = 滑点差异 + broker 拒单风险）。

**修法**（reviewer 推荐方案 A）：monitor 收到 `dec.reason is ExitReason.TAKE_PROFIT` 时 **不发单 continue 跳过**，TP 完全交 _place_take_profit 预挂限价单撮合（D10 物理边界）。加 info 日志「TP 跳过 —— TP 由 _place_take_profit 预挂限价单撮合，monitor 跳过不发市价单（D10）」。

**分发三路径（改后）**：
- ① STOP_LOSS/TIMEOUT（CLOSE/STOP_LOSS | CLOSE/TIMEOUT）→ monitor 发市价卖单（止损/超期是 monitor 职责）
- ② TAKE_PROFIT（CLOSE/TAKE_PROFIT，含 portion<1 tp1 与 portion=1.0 tp2）→ **skip**（交预挂，D10）
- ③ HOLD → 跳过

**不动**：decide_exit/_place_take_profit 逻辑不变（strangler：只改 monitor 行为）；D12 fallback 不动；STOP_LOSS/TIMEOUT 分发不变。

### I-2（必修）：test_monitor_take_profit_skipped_for_premarked_limit

新增测 7：构造 high≥tp1 的 bar（decide_exit 返 CLOSE/TAKE_PROFIT/portion=0.5），断言 monitor **不发卖单**（submitted 空、stop_triggered==0）。验证 I-1 的 skip 行为。

### I-3（应修）：cancel_thresh_mult 默认 0.75→1.0（对齐回测）

**事实**：Task 9（fa5f3d85）_trade_cfg 的 TRADE_CANCEL_THRESH_MULT env 默认 0.75（注释自认对齐 EXEC_DEFAULTS，但实际 EXEC_DEFAULTS backtest.py:49 = 1.0）。实盘 env 缺省 0.75 vs 回测 1.0 → cancel_on 撤单阈值分叉。

**修法**：_trade_cfg 默认 `"0.75"` → `"1.0"`，注释改为「对齐回测 EXEC_DEFAULTS backtest.py:49 + NecklineConfig schema.py:45，二者均 1.0；原 Task 9 误设 0.75」。

**NecklineConfig 核查结果**：`strategies/neckline/schema.py:45` 的 `cancel_thresh_mult: Optional[float] = Field(1.0, ...)` —— 默认 **1.0**，与 EXEC_DEFAULTS（backtest.py:49 = 1.0）**一致**。仅 `_trade_cfg` 单点漂移，改后三处（EXEC_DEFAULTS / NecklineConfig / _trade_cfg）全对齐 1.0。无 schema vs EXEC_DEFAULTS 漂移，不需 follow-up。

**test_pending_cancel_on_during_wait 影响**：该测自构造 `cancel_on=11.5`（颈线10+0.75×2）直接注入 pending_ctx，**不从 _trade_cfg 读**，故改默认不影响该测（已验证通过）。

### I-4（应修）：is_last（>=）vs _scan_expired_positions（>）双向注释

**事实**：_scan_expired_positions（engine.py:894）用 `holding_days > max_holding`，monitor is_last（:1700）用 `holding_days >= max_holding`，第 max_holding 日语义冲突。

**修法（保守，不改运算符）**：`>` 是兜底设计（monitor 第 max_holding 日市价优先平，_scan_expired 第 max_holding+1 日跌停价兜底处理 monitor 漏掉的标的）。改 `>=` 会让 post_close 与 monitor 同日触发（monitor 市价先平后 post_close 跌停价再平 = 卖空风险）。**改为加显式双向注释**：
- _scan_expired_positions docstring + `>` 处：澄清兜底语义，防同日双卖；
- monitor is_last `>=` 处：对齐回测 is_last 市价优先强平。

### 验证

**decide_exit/monitor 测试**：`tests/trading/test_stop_loss_monitor_decide_exit.py` 7 测全过（原 6 + I-2 新 1）。
- 测1 STOP_LOSS 发单 ✓ / 测2 TIMEOUT 发单 ✓ / 测3 HOLD 跳过 ✓ / 测7 TAKE_PROFIT 跳过 ✓（I-1 dry_run 四路径）
- 测4 D12 fallback ✓ / 测5 tp 同源 ✓ / 测6 pending cancel_on ✓

**dry_run 四路径**（I-1 改后）：
- STOP_LOSS（测1）：low≤stop → 发卖出单 ✓
- TIMEOUT（测2）：is_last → 发卖出单 ✓
- HOLD（测3）：未触发 → 跳过 ✓
- **TAKE_PROFIT（测7）：high≥tp1 → 跳过不发市价单，交预挂限价单 ✓**

**全量回归**：1008 passed / 0 failed / 10 deselected（基线 1007 + I-2 新测 1 = 1008，42.56s）。

### 风控红线复核
- I-1 后 monitor STOP_LOSS/TIMEOUT 仍正确发单（测1/2）✓
- D12 fallback 不动（测4）✓
- R7 bar 不动 / pending cancel_on 不动（测6）✓
- decide_exit/_place_take_profit 逻辑不动（strangler，只改 monitor 行为）✓

### Concerns
- **TP skip 无兜底风险**：I-1 方案 A 后，若 _place_take_profit 预挂限价单未成交（如 broker 拒单、断线），monitor 不再补市价单 → 止盈漏触发风险。但优先符合 D10 物理边界（止盈=限价单预挂撮合，非市价追平），且 _place_take_profit 失败已有告警人工补挂链路（engine.py:1838 注释），Phase2 议题含 _tp_placed 持久化。若研究员反馈需兜底，follow-up 可加 lot 翻转/幂等保留 monitor TP 兜底（但当前优先 reviewer 方案 A）。

### Commit
`fix(trading): plan Task9 fix I-1/I-2/I-3/I-4（monitor TP skip + cancel_mult 对齐 + 口径注释）`

SHA: 见 `git log -1`（本次提交）。
