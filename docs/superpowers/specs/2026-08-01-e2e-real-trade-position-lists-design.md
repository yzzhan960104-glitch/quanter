# E2E 报表真实交易/持仓列表设计

- **日期**：2026-08-01
- **分支**：master（E2E 套件增强，不改生产代码）
- **状态**：待审（design review gate）
- **关联**：[[2026-08-01-e2e-long-cycle-design]]（原 spec §8.2/§13 验收）、`tests/e2e_long_cycle/`

## 1. 背景与痛点

原 23 日 E2E full run 通过，但报表（`logs/e2e_long_cycle/e2e_long_cycle_report.md`）中：

- `fill=0`、`position=0` 贯穿 23 天，order 终态只有 SUBMITTED/REJECTED，无 FILLED/PARTIAL_FILLED；
- §2 只有计数，没有交易/持仓明细行；
- §4 钉钉推送 0 条（DingTalkLog.enabled=True 也不记录）。

根因：`ProbabilisticBroker.simulate_submit` 返回 FILLED/PARTIAL_FILLED 后，**没有任何成交回报回调**把结果推进生产账本（真实 QMT 靠 `engine._handle_order_update` 成交回报写 fill/position/order 状态）。因此流程"看似全绿、实则空转"。

## 2. 目标

1. **成交回报注入**：模拟 QMT kind=order/kind=trade 回报，通过 `engine._handle_order_update` 真身写 fill、position、trade_event(FILLED)，并推进 order 状态。
2. **真实持仓留存**：TP1/TP2 止盈限价单按 stk_mins 真实价格触发（当日累积 high ≥ tp 价才成交，非挂即成交），STOP/超期平仓市价单立即成交；持仓跨日留存可见。
3. **明细采集**：TableSnapshotCollector 每日采集 order/fill/trade_event/position/account_daily 明细行（不只计数）。
4. **报表渲染**：ReportBuilder 渲染「交易列表」（每笔成交）与「持仓列表」（每只持仓），含全周期成交流水小节。
5. **推送可审计**：DingTalkLog 真推时记录每条推送（时间/类型/成功失败），§4 不再恒为 0。

## 3. 非目标

- 不改生产代码（engine/state_store/place_take_profit 等全部真身，改动只在 `tests/e2e_long_cycle/`）。
- 不做主推延迟注入（原 spec §7 TODO 项，本次仍不实现）。
- 不重写 4 类校验（保留现有 checks；新增 pytest 断言盯 fill/position/order 状态）。

## 4. 架构与数据流

### 4.1 成交回报注入（ProbabilisticBroker）

`simulate_submit` 增加职责：

- **记录回报**：FILLED/PARTIAL_FILLED/REJECTED 全部入 `self._pending_reports`（含 order_id/symbol/qty/side/price/state/traded_time）。
- **方向反查**：`_submit_mock` 把 `gw._orders[oid] = {"order_type": 23|24}`（BUY=23/SELL=24），与生产 broker/qmt.py 契约一致；`eng._gw = gw` 由 orchestrator 挂接。
- **持仓镜像**：`self._positions` 改为真实增减——BUY 加、SELL 减（clamp 到 0），供 `stop_loss_monitor` 经 `gw._fetch_broker_positions` 读取。
- **TP 限价单**：side=sell 且 price 命中 plan 的 tp1/tp2（±1e-6）→ 返 `SUBMITTED` 并记 `self._resting`（挂单等价格），不立即成交。
- **市价卖单**：STOP/超期平仓（side=sell 非 TP 价）→ 返 `FILLED`（成交价=当前时点价），并 clamp 到当前镜像持仓。
- **买入概率**：仅 OPEN BUY 走 70/15/5 概率；卖单按上述确定性分支。

新增 `async inject_fills(eng)`：

- 排空 `_pending_reports`（深度上限 50，防环）。
- FILLED/PARTIAL_FILLED：先 `eng._handle_order_update({kind:"order", order_id, state, traded_volume, traded_price})` 推进 DB order 状态；再 `eng._handle_order_update({kind:"trade", order_id, stock_code, traded_volume, traded_price, traded_time})` 走生产落账（insert_fill → apply_fill_to_position → trade_event FILLED → BUY 时 `_place_take_profit` 挂 TP）。
- REJECTED：只发 kind=order REJECTED 状态更新。
- TP/STOP 行 broker_oid 回填（生产 async_response 链路在 E2E 不模拟）：按内部 order_id `{date}_{symbol}_{purpose}_1` 直写 `state_store.update_order_state(..., broker_oid=oid, filled_qty, filled_price)`，让 order 表终态与 fill 一致。

新增 `async scan_resting_and_inject(eng, t_date, up_to)`：

- 对每笔 resting TP 限价单，`min_bar_feeder.feed(symbol, t_date, up_to).high >= price` 即排队 FILLED 回报（成交价=限价）并移除；随后注入。
- 只在盘中时点调用（9:30-15:00），pre_open/post_close 不扫（盘前无 bar、盘后不再成交）。

### 4.2 编排（orchestrator）

- pre_open：`broker.attach(...) as gw: eng._gw = gw; run_pre_open_phase(...); inject_fills(eng)`。
- stoploss：monitor 后 `scan_resting_and_inject(eng, t_plus_1, now_time)`（仍在行情/gw patch 内）。
- post_close：`run_post_close_phase(...)` 后 `inject_fills(eng)`（超期平仓卖单落账）。

### 4.3 隔离（conftest.isolated_state）

- 补 patch `presentation.server.services.trading_service.record_live_trade`（防 E2E 写真实 logs/live_trades.csv），与既有 query_trades 隔离同一范式。

### 4.4 明细采集（TableSnapshotCollector）

`snapshot(t_date)` 新增：

- `fills`：fill 表当日行（order_id/traded_time/symbol/direction/qty/price/applied_at）。
- `orders`：order 表当日行（order_id/symbol/side/purpose/qty/price/state/filled_qty/filled_price）。
- `trade_events`：trade_event 当日事件流。
- `positions`：当前持仓行（qty>0），附 holding_days（entry_date 起算）。
- `account_daily_rows`：当日账户快照。

原计数键保留（trade_event/order_count/fill/position/account_daily/trade_event_by_action/order_by_state/plan_*）。

### 4.5 报表（ReportBuilder）

§2 增加：

- 全周期成交流水表（跨日聚合 fills：日期/traded_time/symbol/direction/qty/price）。
- 期末持仓列表（最后一个非空快照）。
- 每日小节内：trade_event 明细、order 明细、fill 交易列表、持仓列表、account_daily 行。

§4 用 DingTalkLog.records 渲染（每条：时间/类型/成功失败）。

## 5. 测试策略（TDD）

1. `test_orchestrator_smoke`（改）：断言 day2 snapshot `fill>0`、`positions` 非空、`order_by_state` 含 FILLED；md 含「交易列表」「持仓列表」与 symbol 行。先跑失败（当前 fill=0）再实现。
2. `test_probabilistic_broker.py`（增）：`inject_fills` 经真身写 fill/position 单测。
3. `test_table_snapshot.py`（增）：预置 fill/order/position 行，断言明细列表。
4. `test_report_builder.py`（增）：预置快照明细，断言 md 含交易/持仓表格行。
5. `test_dingtalk_log.py`（增）：enabled=True 时记录条数 > 0。
6. full run（改断言）：聚合 fill>0、positions 出现过、order_by_state 含 FILLED/PARTIAL；报告含全周期成交流水表。

## 6. 验收标准

1. smoke 断言全绿（fill/position/order FILLED 真实落表）。
2. 23 日 full run PASSED，报告 §2 出现真实交易列表（每笔成交行）与持仓列表（含 entry_date/holding_days）。
3. order 终态分布出现 FILLED/PARTIAL_FILLED，不再是纯 SUBMITTED/REJECTED。
4. §4 推送记录非空（若真推链路有实际通知）。
5. 组件单测全绿；默认回归不退化。

## 7. 风险与缓解

- **TP 价格匹配误判**（STOP 卖单价恰好等于 tp 价）→ 匹配加 plan 价格 ±1e-6 与目的推断；误判仅影响 E2E 行为，不触生产。
- **负持仓/双卖** → 镜像 clamp 到 0，SELL qty 超持仓时按持仓量平；DB position 不会负。
- **注入循环**（BUY fill → TP 挂单 → ...）→ 深度上限 50。
- **full run 时长** → 注入为内存操作，预计增量 < 1min。
