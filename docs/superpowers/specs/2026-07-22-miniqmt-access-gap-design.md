# miniQMT 接入补全 设计文档

> **创建日期**：2026-07-22
> **状态**：设计 → 实施计划（writing-plans）
> **事实来源**：`dict.thinktrader.net/nativeApi/xttrader.html`（交易 API 权威）+ `xtdata.html`（行情 API 权威）+ `迅投QMT极速策略交易系统说明文档.pdf`（472 页 GUI 手册，无 API 章节，仅账号状态枚举互证）
> **前置调研**：交易模块 review + 行情模块 review（对照官方文档全集 vs 现有 `trading/qmt_gateway.py` + `trading/qmt_market_data.py`）

## 1. 目标

对照迅投官方 xttrader/xtdata API 全集，补全现有 miniQMT 接入（`QmtExecutionGateway` + `qmt_market_data`）的 8 项缺失能力，覆盖**账号安全盲区、二期熔断 equity 源、订单状态可靠性、行情批量优化**四个 live 前必修维度。维持颈线法现状语境（固定止损价 + 柜台限价止盈 + 5min 轮询），行情不接 subscribe 推送（YAGNI）。

## 2. 背景

现有 miniQMT 接入核心链路完整且工程质量高（connect/submit_order/cancel_order/持仓/6 个回调/on_order_stock_async_response seq↔real 映射/自动重连/超时兜底/GC 全到位），但对照官方文档全集有 3 类缺失：

- **账号层状态盲区**：只处理 `on_disconnected`（连接级 socket 断），未处理 `on_account_status`（账号级：系统停用/登录失败/穿透副链断开）。
- **查询能力不足**：只 `query_stock_positions`，缺 `query_stock_asset`（二期熔断 equity 源卡点）/`query_stock_orders`/`query_stock_trades`（主推失败兜底）。
- **行情单只拉取**：`get_quote` 单只，止损监控 N 只持仓 N 次调用。

## 3. 范围（8 项 · 3 组）

| 组 | # | 能力 | 文件 |
|---|---|---|---|
| P0 | ① | `on_account_status` 回调（账号停用感知） | qmt_gateway.py |
| P0 | ② | `query_stock_asset` → `query_asset()`（解锁二期熔断 equity 源） | qmt_gateway.py |
| P0 | ③ | `get_quotes` 批量快照（止损监控优化） | qmt_market_data.py |
| P1 | ④ | `query_stock_orders` + `query_stock_trades`（主动查询） | qmt_gateway.py |
| P1 | ⑤ | subscribe 失败惰性查询兜底 | qmt_gateway.py |
| Minor | ⑥ | `_assert_status_contract` 校验补全 11 态 | qmt_gateway.py |
| Minor | ⑦ | `cancel_order` rc==0 message 非终态语义 | qmt_gateway.py |
| Minor | ⑧ | `_fetch_broker_positions` 扩展成本价/昨夜股字段 | qmt_gateway.py |

## 4. 设计

### 4.1 架构与边界

- **纯增量**：只在 `qmt_gateway.py` / `qmt_market_data.py` 加方法/回调，不动 connect/submit_order/cancel_order/持仓主链路与 seq↔real 映射。
- **复用三红线**（现有工程已立）：
  1. 同步 C++ 调用（query_stock_asset/query_stock_orders/query_stock_trades）经 `loop.run_in_executor` 投线程池 + `asyncio.wait_for` 超时兜底。
  2. 回调（`on_account_status`）在 xtquant C++ 线程，只做"解析 + `call_soon_threadsafe` 投递主线程"，零跨线程副作用（与 `on_disconnected`/`on_stock_order` 同铁律）。
  3. 状态字面量 + 连接时 `_assert_status_contract` 一次性校验防版本漂移。
- **不重构现有**：一期 `trading_service.get_asset` 的 QMT 内联分支（现内联调 `query_stock_asset`）保持不动；新增的 `gw.query_asset()` 供二期熔断消费，未来可统一双网关口径（follow-up，非本 spec scope）。

### 4.2 P0 三项

#### ① `on_account_status` 回调（账号停用感知）

```python
def on_account_status(self, status: Any) -> None:
    # C++ 线程：解析 status.status → call_soon_threadsafe → 主线程 _on_account_status_change
```

**8 态锁策略**（决策点 1，已确认）：

| 枚举 | 值 | 含义 | 处理 |
|---|---|---|---|
| `ACCOUNT_STATUS_INVALID` | -1 | 无效 | 🔴 `_lock_down=True` + 钉钉告警 |
| `ACCOUNT_STATUS_OK` | 0 | 正常 | 🟢 清 `_lock_down` |
| `ACCOUNT_STATUS_WAITING_LOGIN` | 1 | 连接中 | 🟡 log |
| `ACCOUNT_STATUSING` | 2 | 登录中 | 🟡 log |
| `ACCOUNT_STATUS_FAIL` | 3 | 登录失败 | 🔴 锁 + 告警 |
| `ACCOUNT_STATUS_INITING` | 4 | 初始化中 | 🟡 log |
| `ACCOUNT_STATUS_CORRECTING` | 5 | 数据刷新校正中 | 🟡 log（校正完有新推送） |
| `ACCOUNT_STATUS_CLOSED` | 6 | 收盘后 | 🟢 不锁（正常） |
| `ACCOUNT_STATUS_ASSIS_FAIL` | 7 | 穿透副链接断开 | 🔴 锁 + 告警 |
| `ACCOUNT_STATUS_DISABLEBYSYS` | 8 | 系统停用（密码错误超限） | 🔴 锁 + 告警 |
| `ACCOUNT_STATUS_DISABLEBYUSER` | 9 | 用户停用 | 🔴 锁 + 告警 |

- 主线程 `_on_account_status_change(status_int)`：🔴 态置 `_lock_down=True` + `fire_and_forget(notify_risk_event(..., "ERROR"))`（复用 `_on_disconnect_fatal` 告警通道）；🟢 态清锁；🟡 态 `logger.info`。
- 与 `on_disconnected` 的关系：disconnected 是连接级（socket 断），account_status 是账号级（账号被停用但 socket 可能还在）。两者独立，账号被系统停用时 `on_disconnected` 不一定触发，必须靠 `on_account_status`。

#### ② `query_asset()`（解锁二期熔断 equity 源）

```python
async def query_asset(self) -> dict[str, Any]:
    """投线程池调 query_stock_asset(acc)，返标准化资产 dict。"""
    # 返回：{"account_id": str, "cash": float, "total_asset": float, "market_value": float}
```

- **返回结构**（决策点 2，合理性已核查）：4 字段，与一期 `trading_service.get_asset` 的 QMT 分支 + EMT `_fetch_asset` + 前端 `Asset` 类型**完全对齐**（不含 `frozen_cash`——前端不用、二期熔断只需 `total_asset`，YAGNI）。
- 异常/None 返 `{}`（与一期 get_asset 缺失语义一致）。
- **双消费者**：
  - 一期 `get_asset`：QMT 内联分支保持（增量不重构），未来可改调 `gw.query_asset()` 统一。
  - 二期 `circuit_breaker.check_daily_loss_limit(start_equity, curr_equity)`：`total_asset` 即 equity，**直接解锁二期 live 必修 gap①**（post_close 熔断连线）。

#### ③ `get_quotes` 批量快照（行情优化）

```python
# qmt_market_data.py
async def get_quotes(symbols: list[str]) -> dict[str, Optional[Mapping[str, Any]]]:
    """批量取多标的 tick 快照（get_full_tick 原生支持 list）。缺失标的值 None。"""
```

- `get_full_tick(symbols)` 原生支持 list，一次调用返多只；现有 `get_quote(symbol)` 保持（单只便利方法，内部可改为 `get_quotes([symbol])[symbol]`）。
- `engine.stop_loss_monitor` 改用 `get_quotes(持仓列表)`：N 只持仓 N 次 `get_quote` → 1 次 `get_quotes`（线程池调用 N→1）。
- 缺失标的（`get_full_tick` 不含）值 `None`，调用方按 None 降级（跳过该标的止损检查，已有逻辑）。

### 4.3 P1 两项

#### ④ `query_orders` + `query_trades`（主动查询）

```python
async def query_orders(self, cancelable_only: bool = False) -> list[dict[str, Any]]:
    """投线程池调 query_stock_orders(acc, cancelable_only)，返标准化 XtOrder dict 列表。"""
async def query_trades(self) -> list[dict[str, Any]]:
    """投线程池调 query_stock_trades(acc)，返标准化 XtTrade dict 列表。"""
```

- 标准化字段（XtOrder）：`order_id/stock_code/order_type/order_volume/price/traded_volume/traded_price/order_status/state(_map_qmt_status)/status_msg/order_remark`。
- 标准化字段（XtTrade）：`order_id/stock_code/traded_volume/traded_price/traded_amount/traded_time`。
- None（查询失败/空）返 `[]`。
- 用途：subscribe 失败兜底（⑤）+ 二期盘后对账强化（Task7 reconcile，不止持仓对账，还能对委托/成交流水）。

#### ⑤ subscribe 失败惰性查询兜底

- connect 时 `sub_rc != 0` 标记 `self._main_push_available = False`（不再只 `warning` 继续，明确主推不可用）。
- **惰性同步**（决策点 3，已确认）：不引入后台定时轮询（避免新调度复杂度），而是在 `pre_open` / `stop_loss_monitor` 等触发点**前**，若 `_main_push_available == False`，调 `query_orders` 主动同步 `self._orders`（补全订单状态后再生效触发逻辑）。
- `_main_push_available` 初始 True；connect 成功 + subscribe 成功保持 True；subscribe 失败置 False；重连成功后重新 subscribe（若成功）恢复 True。

### 4.4 Minor 三项

#### ⑥ `_assert_status_contract` 校验补全 11 态

现有 `expected` 只校验 7 个状态字面量，补全到 11 个：+`PARTSUCC_CANCEL=52`/`REPORTED_CANCEL=51`/`WAIT_REPORTING=49`/`UNKNOWN=255`（`_map_qmt_status` 全在用，校验应全覆盖防版本漂移）。

#### ⑦ `cancel_order` rc==0 message 非终态语义

现状 rc==0 返 `CANCELLED` + message"撤单指令已发出，等待回报确认"。微调 message 明示"**最终态以 `on_stock_order` 推送 CANCELLED 为准，当前仅表示指令发出**"——现状已近似，补非终态语义（OrderState 无"撤单中"中间态，保持返 CANCELLED 但 message 严谨）。

#### ⑧ `_fetch_broker_positions` 扩展字段

现有返回 `{stock_code: volume}`（只 volume）。扩展为 `{stock_code: {volume, avg_price, open_price, yesterday_volume}}`：
- `avg_price`/`open_price`：成本价（浮盈对账用）
- `yesterday_volume`：昨夜股（T+1 判断强化）
- **向后兼容**：保持 volume 为主可用量（`can_use_volume==0` 过滤不变），新增字段供对账层按需读取；现有消费者（二期 stop_loss_monitor 取 qty）改为读 `volume` 子键。

## 5. 测试策略

- 每个 API 一个 TDD test（先红后绿）。
- **回调测试**（① on_account_status）：`FakeCallback` 触发 `on_account_status(status_obj)`，断言 `call_soon_threadsafe` 投递 + 主线程 `_on_account_status_change` 对 8 态的锁/告警/log 行为（构造 11 态各一例）。
- **查询测试**（②③④）：`FakeTrader` 返 fixture `XtAsset`/`XtOrder[]`/`XtTrade[]`/`get_full_tick dict`，断言标准化返回结构 + None/异常降级。
- **subscribe 兜底测试**（⑤）：connect 时 mock `subscribe` 返 -1，断言 `_main_push_available=False` + 触发点前调 `query_orders` 同步 `_orders`。
- **Minor 测试**（⑥⑦⑧）：⑥ 补全校验后断言 11 态全过 + 故意改一个值断言 fail-fast；⑦ message 文案断言；⑧ 扩展字段断言 + 向后兼容（volume 仍可读）。
- **回归红线**：现有 `tests/trading/` 49 绿不破 + `test_qmt_gateway`（若有）全绿。

## 6. 与二期/live 的关联

| 二期 gap | 本 spec 对应 | 状态 |
|---|---|---|
| ① post_close 熔断连线（需 equity 源） | **② `query_asset`** | 实现后解锁（`total_asset` = equity） |
| ② 策略层数据源注入 | 已由实验系统解决（memory: quanter-experiment-system，88 passed 合 master） | 不在本 spec |
| ③ EMT 行情源 | EMT 已废弃（miniQMT 模拟盘路径），`qmt_market_data.get_quote` 已可用 | 不阻塞 |
| 隐含第 4 项：账号停用感知 | **① `on_account_status`** | live 前应补 |

## 7. 不做（YAGNI / 单一真相源边界）

对照 xttrader + xtdata 全集，以下**不接入**：

- **行情订阅推送**：`subscribe_quote`/`subscribe_whole_quote`/`subscribe_tick_data`——颈线法 5min 轮询够（固定止损价 + 柜台限价止盈），高频场景才需要。
- **历史 K 线**：`get_market_data`/`get_market_data_ex`/`download_history_data`——`data_lake`（Tushare 5 年全市场落湖）是单一真相源。
- **交易日历**：`get_trading_dates`——`trading/calendar.py`（Task1 Tushare trade_cal）是真相源。
- **标的详情/名称**：`get_instrument_detail`——`name_resolver`（Tushare）是真相源。
- **板块/财务**：`get_sector_list`/`get_stock_list_in_sector`/`get_financial_data`——Tushare 已落湖。
- **同步下单/异步撤单/按合同号撤**：`order_stock`/`cancel_order_stock_async`/`cancel_order_stock_sysid`——现有 async 下单 + 同步撤单够用。
- **资金划拨/外部成交导入/通用导出**：`fund_transfer`/`sync_transaction_from_external`/`export_data`/`query_data`——普通股票账户不需要。
- **信用/约券/期货/期权/新股**：`query_credit_*`/`smt_*`/期货统计/ETF 申赎/新股额度——普通股票账户不需要。
- **`run_forever`/`set_relaxed_response_order_enabled`**：asyncio loop 替代前者；回调里不调同步查询，后者不需要。

## 8. 文件结构

**修改**：
- `trading/qmt_gateway.py`：+ `on_account_status`/`_on_account_status_change`（①）、`query_asset`（②）、`query_orders`/`query_trades`（④）、`_main_push_available` + connect 兜底（⑤）、`_assert_status_contract` 补全（⑥）、`cancel_order` message（⑦）、`_fetch_broker_positions` 扩展（⑧）。
- `trading/qmt_market_data.py`：+ `get_quotes`（③），`get_quote` 改委托。
- `trading/engine.py`：`stop_loss_monitor` 改用 `get_quotes` 批量（③ 消费侧）。

**测试**：
- `tests/trading/test_qmt_gateway.py`（新增）：①-②④-⑧ 全覆盖。
- `tests/trading/test_qmt_market_data.py`（新增）：③ 批量 + 现有 get_quote 回归。
- `tests/trading/test_engine.py`：③ stop_loss_monitor 批量取价回归。

## 9. 风险与边界

- **`on_account_status` 推送频率**：账号状态变动是低频事件（登录/停用/收盘），非行情高频，主线程负担可忽略。
- **惰性同步的时序**（⑤）：触发点前 query_orders 同步有延迟（一次主动查询），但比"主推缺失 + 不查询"（订单状态盲区）可靠得多。颈线法触发点低频（pre_open 1 次/日 + stop_loss 每 5min），查询开销可接受。
- **`get_full_tick` 批量缺失标的**（③）：返 dict 不含的标的，`get_quotes` 标 None，调用方按 None 降级（已有逻辑），不影响其他标的。
- **`_fetch_broker_positions` 向后兼容**（⑧）：返回结构从 `{sym: float}` 变 `{sym: {volume, ...}}` 是**破坏性变更**，需同步改所有消费者（二期 stop_loss_monitor 取 qty 的位置）。spec 明确这点，plan 会单列一个 task 处理消费者迁移。
