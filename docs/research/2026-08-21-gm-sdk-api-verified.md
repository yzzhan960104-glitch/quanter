# gm SDK 源码级 API 核对文档（权威名映射）

- 日期：2026-08-21
- 核对对象：`gm 3.0.186`，安装于 `E:/quanter/.venv_emquant/Lib/site-packages/gm/`（Task 1 建立的专用环境，禁止 pip install）
- 地位：**本文档是后续所有任务 gm API 调用的唯一权威**。计划/设计片段与本文档冲突时，以本文档为准（计划明文规则）。
- 方法：只读源码（`.py`/`.pyi`/`.pb2`）；C 扩展（`.pyd`/`.dll`）采用二进制字符串扫描取旁证；辅以东财掘金官方帮助文档（emquant.18.cn，仅作文档级证据并明确标注）。
- 行号基准：gm 3.0.186 源码当前安装副本，全部为实测行号，非推测。
- 安全（C8）：本文档不含任何真实 token；东财官方文档示例代码中含一处示例 token，本文档不转录、仅引用其语义。
- 与 Task 1 结论的一致性：已复核 `MODE_SIMULATION` 不存在（`gm/enum.py:130-132` 仅 `MODE_UNKNOWN=0 / MODE_LIVE=1 / MODE_BACKTEST=2`）；订单枚举为 `gm/enum.py` 模块级离散常量、非 Enum 类。以下不重复论证，直接作为既定事实。

---

## 一、TL;DR 权威名映射总表

| 用途 | 权威名（gm.api 暴露） | 源码位置 | 关键签名/返回要点 |
|---|---|---|---|
| 运行策略 | `run` | gm/api/basic.py:399 | `run(strategy_id='', filename='', mode=MODE_UNKNOWN, token='', ...)`；filename 转模块名 import |
| 实时/回测模式 | `MODE_LIVE`(=1) / `MODE_BACKTEST`(=2) | gm/enum.py:130-132 | **无仿真常量**；终端"仿真交易"亦走 MODE_LIVE |
| 定时任务 | `schedule` | gm/api/basic.py:388 | `schedule(schedule_func, date_rule, time_rule)`；回调只收 context |
| 行情订阅 | `subscribe` / `unsubscribe` | gm/api/basic.py:129 / 217 | tick 最新价字段为 `price` |
| tick 快照查询 | `current` / `current_price` / `last_tick` | gm/api/basic.py:312 / 353 / 264 | `current_price` 返回 `price` 键（由 `last_price` 改名） |
| 限价/市价下单 | `order_volume` | gm/api/trade.py:115 | 返回 `List[Dict]`（DictLikeOrder），非单 dict |
| 撤单 | `order_cancel` / `order_cancel_all` | gm/api/trade.py:398 / 361 | `order_cancel` 收 dict 或 list[dict]，键 `cl_ord_id`+`account_id` |
| 查委托 | `get_orders`（日内全部）/ `get_unfinished_orders`（未结） | gm/api/trade.py:336 / 311 | **均无参数**，内部遍历 context.accounts |
| 查成交回报 | `get_execution_reports` | gm/api/trade.py:464 | 无参数 |
| 查资金 | `get_cash` | gm/api/query.py:1146 | `get_cash(account_id=None)` → 单个 dict（nav/available/market_value…） |
| 查持仓 | `get_position` | gm/api/query.py:1163 | `get_position(account_id=None)` → list[dict] |
| 历史行情 | `history` / `history_n` | gm/api/query.py:562 / 614 | 见 §Q6；33000 条上限不在 Python 层 |
| 跌停价/昨收 | `get_history_symbol`（首选）或 `get_history_instruments` | gm/api/ds_instrument.py:118 / query.py:244 | 字段 `upper_limit` / `lower_limit` / `pre_close`（double） |
| 交易日历 | `get_trading_dates` / `get_previous_trading_date` / `get_next_trading_date` / `get_previous_n_trading_dates` / `get_next_n_trading_dates` / `get_trading_dates_by_year` | query.py:438/465/488；ds_instrument.py:228/249/146 | 返回 `yyyy-mm-dd` 字符串列表 |
| 账户对象 | `context.accounts` / `context.account(id_or_name)` | gm/model/storage.py:213 / 223 | `Dict[account_id, Account]`；Account.match 按 name **或** id |
| account 解析 | `get_account_id(name_or_id)` | gm/api/trade.py:454 | 名字/ID 均可；匹配不上原样透传给后端 |
| 预设账号 | `set_account_id` | gm/api/basic.py:858 | C 层预设，Python 侧无人调用 |
| token | `set_token` / `run(token=...)` | gm/api/basic.py:98 / 598 | `context.token = 'bearer ' + token` |
| 终端地址 | `set_serv_addr` / `run(serv_addr=...)` / 命令行 `--serv_addr` | basic.py:764 / 595 / api/__init__.py:433 | 默认 `127.0.0.1:7001`（gmsdk.dll 字符串） |
| 策略日志 | `log` | gm/api/basic.py:739 | 仅实时模式，终端界面查看 |
| 停止策略 | `stop` | gm/api/basic.py:753 | `sys.exit(2)` |

回调注册约定（无装饰器、无 run 参数）：**策略文件模块级函数名约定**，`run()` 里 `getattr(fmodule, 'on_tick', None)` 等逐一抓取（basic.py:610-638）。首参恒为 `context`。

---

## 二、必答十题（逐题源码证据）

### Q1 仿真语义真身（最高优先）

**结论：SDK Python 层无法区分仿真账户与实盘账户。gm 3.0.186 没有 `get_account_infos`/`get_accounts` 这类 Python 可调的账户列表 API；唯一账户枚举路径在 Context 内部 `_set_accounts()`，且只消费 `account_id`/`account_name` 两个 string 字段，丢弃了其余全部信息（含可能携带类型标记的 `info.properties` map）。C1 守卫只能靠「账户 ID（或账户名）白名单 + `context.accounts` 实测比对」。**

直接证据链：

1. **账户列举的唯一入口**（且不是公开 API，是 Context 内部方法）：
   ```python
   # gm/model/storage.py:242-250
   def _set_accounts(self):
       status, result = py_gmi_get_accounts()
       if c_status_fail(status, "py_gmi_get_accounts") or not result:
           return
       accounts = Accounts()
       accounts.ParseFromString(result)
       for account in accounts.data:
           self._get_account_info(account.account_id, account.account_name)
   ```
   `py_gmi_get_accounts` 来自 C 扩展（gm/model/storage.py:20 导入），返回 `core.api.Accounts` protobuf。**注意：拉取失败时静默 return（空账户表），策略无感知**——C1 守卫必须把「context.accounts 为空」视为故障而非通过。

2. **`core.api.Account` 消息体的全部字段**（gm/pb/account_pb2.py:3173 起，Account 消息注册于 `account_pb2.py` 内 `DESCRIPTOR`；字段名由 descriptor 反射实测）：
   `account_id, account_name, title, intro, comment, exchanges[], sec_types[], info(core.api.AccountInfo), created_at, updated_at`。
   其中 `AccountInfo = { is_active(bool), appid(string), properties(map<string,string>) }`。
   **没有任何「账户类型/仿真标记」显式字段。** `properties` map 理论上可能由服务端塞入类型标记，但 SDK 不读它，Python 侧拿到的 `Account` 对象（gm/model/account.py:11-21）只有 `id/name/cash/inside_positions/status` 五个属性——properties 已被丢弃。

3. **`Account.match` 是名字或 ID 双匹配**（gm/model/account.py:23-24）：
   ```python
   def match(self, name):
       return self.name == name or self.id == name
   ```
   下单侧 `get_account_id(name_or_id)`（gm/api/trade.py:454-461）据此解析；匹配不上**不报错，原样透传**（注释原文：「都没有匹配上, 等着后端去拒绝」）。

4. **proto 层确实存在仿真/实盘类型枚举，但 Python SDK 全程未消费**（最强旁证）：
   - `core.api.AccountChannel.Type` 枚举：`Type_Unknown=0, Type_Simulate=1, Type_Live=2`（gm/pb/account_pb2.py:1298-1316，`Type_Simulate` 在 :1309，`Type_Live` 在 :1314）。
   - `core.api.AccountChannel` 消息字段：`channel_id, channel_type, title, intro, logo, mode, exchanges, sec_types, conn_addr, conn_addr_fixed, conn_conf, conn_type, conn_properties, conn_ssl, tags`（gm/pb/account_pb2.py:3537-3643）。
   - 全包检索 `Type_Simulate|AccountChannel|simulate|仿真`：**除 pb2 生成代码外零命中**（实测 grep）。`AccountChannel` 消息在 Python 侧无任何构造/解析代码。
   - 含义：仿真/实盘的区分标记活在**终端 tradegw 服务层**（账户通道 Channel），SDK 的 Python 面没有暴露查询入口。

5. **终端侧证据（二进制字符串 + 官方文档）**：
   - gmsdk.dll 内嵌 HTTP 路由字符串（实测扫描）：`/v3/strategies/{strategy_id}/accounts`、`/v3/account-trade/cash/{account_id}`、`/v3/account-trade/orders/{account_id}`、`/v3/account-trade/positions/{account_id}`、`/v3/account-trade/execrpts/{account_id}`、`tradegw.api.GetAccountChannelsReq`、`/v5/discovery/all-account-services`——**策略↔账户绑定关系由终端 tradegw 按 strategy_id 维护**，`run()` 只调 `py_gmi_set_strategy_id(strategy_id)`（basic.py:601）。
   - 东财官方文档（emquant.18.cn/help/doc/python/，「选择回测模式/实时模式运行示例」节）原文：「**在终端仿真交易和实盘交易的启动策略按钮默认是实时模式**，运行回测默认是回测模式」——终端 UI 的"仿真交易"与"实盘交易"两个入口**在 SDK 层同属 MODE_LIVE**，区别只在终端把策略挂到哪个资金账户/通道上。

**C1 守卫的落地建议（据上证据）**：`run()` 启动后（`_set_accounts` 已在 gmi_init 后执行，basic.py:673-676）读 `context.accounts`，校验「账户白名单中的 account_id（或 account_name）确实在场且唯一」；无法从 SDK 层验证"仿真"属性本身，只能验证"绑定的是我们指定的那个账户"。若需更强的类型证据，唯一途径是终端 UI 人工核对账户属于仿真柜台（见 Q8 的 tradegw 机制），SDK 自动化层面到此为止。

### Q2 `schedule` 完整签名与 date_rule/time_rule 格式

签名原文（gm/api/basic.py:388-397）：
```python
def schedule(schedule_func, date_rule, time_rule):
    # type: (Any, Text, Text) -> None
    """
    定时任务. 这里的schedule_func 要求只能有一个context参数的函数
    """
    schemdule_info = SCHEDULE_INFO.format(
        date_rule=date_rule, time_rule=time_rule)
    context.inside_schedules[schemdule_info] = schedule_func
    status = py_gmi_schedule(date_rule, time_rule)
    check_gm_status(status)
```
- 注册键格式：`'date_rule={date_rule},time_rule={time_rule}'`（gm/constant.py:49 `SCHEDULE_INFO`）。**同一 date_rule+time_rule 组合后注册的函数会覆盖先注册的**（dict 赋值语义）；不同 func 但同刻注册 = 前者被覆盖。
- 触发路径：C 层发 `schedule` 回调（constant.py:22）→ `schedule_callback`（gm/callback.py:410-419）以拼串为键查 `context.inside_schedules` 再调用，**回调只收一个 context 参数**。
- **Python 层对 date_rule/time_rule 零校验、零格式解析**——直接透传 `py_gmi_schedule`。gmsdk.dll 二进制内仅有拼串模板 `date_rule=%s,time_rule=%s`，无格式常量。
- 格式与合法时刻的**文档级证据**（东财官方帮助文档 emquant.18.cn/help/doc/python/ 定时任务示例，注释原文）：
  > `# date_rule执行频率，目前暂时支持1d、1w、1m，其中1w、1m仅用于回测，实时模式1d以上的频率，需要在algo判断日期`
  > `# time_rule执行时间， 注意多个定时任务设置同一个时间点，前面的定时任务会被后面的覆盖`
  > `schedule(schedule_func=algo, date_rule='1d', time_rule='14:50:00')`
  即：date_rule ∈ {`1d`(每交易日), `1w`(周), `1m`(月)}；**实时模式实际只有 1d 可靠，1w/1m 仅回测**，周/月粒度须在回调里自行判断日期；time_rule 格式 `HH:MM:SS`（示例含秒）。
- **盘前 09:15:00 可否**：源码与官方文档均未记载时刻限制（time_rule 是纯时钟时刻，无时段校验代码）。09:15:00 属交易日内时刻，机制上应可触发；集合竞价时段本身存在（`get_trading_times` 返回 `time_callauction` 如 `{'start': '09:15', 'end': '09:25'}`，gm/api/query.py:858-880，docstring 原文）。**裁定：可用，但列入 live 验证清单（§五）——五阶段里 09:15 的盘前阶段依赖它。**

### Q3 事件回调注册机制与 tick 报价字段

**注册机制：全局函数名约定，run() 里 getattr 抓取**（不是装饰器、不是 run 参数）。gm/api/basic.py:608-638 原文（节选）：
```python
    # 调用户文件的init
    context.inside_file_module = fmodule
    context.on_tick_fun = getattr(fmodule, 'on_tick', None)
    ...
    context.on_bar_fun = getattr(fmodule, 'on_bar', None)
    ...
    context.init_fun = getattr(fmodule, 'init', None)
    context.on_execution_report_fun = getattr(fmodule, 'on_execution_report', None)
    context.on_order_status_fun = getattr(fmodule, 'on_order_status', None)
```
完整可注册名单（basic.py:610-638）：`init`、`on_tick`、`on_depth`、`on_bar`、`on_l2transaction`、`on_l2order`、`on_l2order_queue`、`on_order_status`、`on_order_status_mm`、`on_execution_report`、`on_algo_order_status`、`on_backtest_finished`、`on_parameter`、`on_error`、`on_shutdown`、`on_trade_data_connected`、`on_market_data_connected`、`on_account_status`、`on_market_data_disconnected`、`on_trade_data_disconnected`、`on_customized_message`。
另外：gm.api 的全部符号会被**注入策略模块命名空间**（basic.py:589-592），`from gm.api import *` 非必需。C 层统一入口 `py_gmi_set_data_callback(callback_controller)`（basic.py:643），分发器 `callback_controller(msg_type, data)` 在 gm/callback.py:783-882。

**签名约定（首参恒 context）**：
- `init(context)` — callback.py:121-135（`context.init_fun(context)`）
- `on_tick(context, tick)` — callback.py:221-222（单条 tick）
- `on_bar(context, bars)` — callback.py:301 `context.on_bar_fun(context, [bar])`：**bars 是 list**（wait_group 时是多标的 list；超时触发 callback.py:616 同样传 list）
- `on_order_status(context, order)` / `on_execution_report(context, rpt)` — callback.py:477-479 / 439-440
- `on_error(context, code, info)` — callback.py:569，data 为 `"code|info"` 串拆分；未定义时用 `default_err_callback` 仅打 warning（callback.py:541-552，其中 1200/1201 是行情重连提示）

**tick 报价字段名（权威）**：实时模式下 on_tick 收到的 `TickLikeDict2`（dict 子类）由 callback.py:184-201 构造，键集原文：
```python
    ticknew = {
        "quotes": quotes,              # 十档 list[dict]: bid_p/bid_v/ask_p/ask_v/bid_q/ask_q
        "symbol": tick.symbol,
        "created_at": protobuf_timestamp2bj_datetime(tick.created_at),  # 北京时间 datetime
        "price": round_float(tick.price),   # ★最新价是 price，不是 last_price
        "open": round_float(tick.open),
        "high": round_float(tick.high),
        "low": round_float(tick.low),
        "cum_volume": tick.cum_volume,
        "cum_amount": tick.cum_amount,
        "cum_position": tick.cum_position,
        "last_amount": tick.last_amount,
        "last_volume": tick.last_volume,
        "trade_type": tick.trade_type,
        "flag": tick.flag,
        "receive_local_time": time.time(),
        "iopv": tick.iopv,
    }
```
类型旁证（gm/csdk/c_sdk.pyi:14-31，`TickLikeDict2`）：`price/open/high/low: float`，`cum_volume/cum_amount: float`，`cum_position/last_volume/trade_type/flag/nanos: int`，`created_at: datetime.datetime`。注意：quotes 不足十档时**补零到十条**（callback.py:172-182），消费侧不能以 `bid_p != 0` 之外的档位存在性判断深度。`current_price()` 返回结构里则把服务端 `last_price` 改名为 `price`（basic.py:370-377）。

### Q4 下单族

**`order_volume` 签名原文**（gm/api/trade.py:115-127）：
```python
def order_volume(
    symbol,
    volume,
    side,
    order_type,
    position_effect,
    price=0,
    trigger_type=0,
    stop_price=0,
    order_duration=OrderDuration_Unknown,
    order_qualifier=OrderQualifier_Unknown,
    account="",
):
    # type: (Text, float, int, int, int, float, int, int, Text) ->List[Dict[Text, Any]]
    """
    按指定量委托
    """
```
- **限价买最小参数集**：`order_volume(symbol='SHSE.600000', volume=200, side=OrderSide_Buy, order_type=OrderType_Limit, position_effect=PositionEffect_Open, price=10.55, account=<account_id>)`。限价单必须显式给 `price`（默认 0 会被柜台拒：`OrderRejectReason_IllegalPrice=8`，enum.py:47）。
- **限价卖**：`side=OrderSide_Sell, order_type=OrderType_Limit, position_effect=PositionEffect_Close, price=X`（A 股现货无开平仓语义，但字段必填，官方示例用 Open 买/Close 卖，见东财文档示例原文 `position_effect=PositionEffect_Open`）。
- `account=""` 时：`get_account_id("")` 匹配不到任何账户（`Account.match('')` 为 False），**account_id 以空串透传给后端**（trade.py:454-461 注释「都没有匹配上, 等着后端去拒绝」）——单账户场景由终端侧解析。**建议 pilot 显式传 account_id**（C1 可审计）。
- **返回值形态**：`List[Dict]`（`_inner_place_order`，trade.py:87-112），每项为 `DictLikeOrder`（dict 子类，属性/下标双访问，gm/model/__init__.py:843）。同步返回的 order 里 `cl_ord_id` 有值（trade.py:90 注释原文「下单返回的order 的 client_order_id 将会有值」——注释里的 client_order_id 是陈旧写法，真实字段名是 `cl_ord_id`，见 pb）。**调用侧必须取 `[0]`**。
- Order dict 字段全集（`core.api.Order`，account_pb2 descriptor 实测）：`strategy_id, account_id, account_name, channel_id, cl_ord_id, order_id, ex_ord_id, algo_order_id, symbol, side, position_effect, position_side, order_type, order_business, order_duration, order_qualifier, order_src, position_src, status, ord_rej_reason, ord_rej_reason_detail, price, stop_price, order_style, volume, value, percent, target_volume, target_value, target_percent, filled_volume, filled_vwap, filled_amount, filled_commission, properties, created_at, updated_at`（created_at/updated_at 为 datetime）。
- 同族：`order_value`(trade.py:152)、`order_percent`(185)、`order_target_volume`(218)、`order_target_value`(249)、`order_target_percent`(280)、`order_batch`(420)。

**`order_cancel`**（gm/api/trade.py:398-417）：
```python
def order_cancel(wait_cancel_orders):
    # type: (Union[Dict[Text,Any], List[Dict[Text, Any]]]) -> None
    """
    撤销委托. 传入单个字典. 或者list字典. 每个字典包含key: cl_ord_id, account_id
    """
```
**入参不是 cl_ord_id 字符串**，是含 `cl_ord_id`+`account_id` 两键的 dict（或 list[dict]）——最省事的取法：拿 `get_unfinished_orders()` 返回项直接回传（其含这两个键）。空列表会抛 `GmError(-1, "撤单信息不能为空", ...)`（trade.py:407-408，`send_custom_error` 见 _errors.py:33-34）。另有 `order_cancel_all()`（trade.py:361，**无返回值、不 check 状态**）与 `order_close_all()`（trade.py:374，平所有可平持仓，慎用）。

**委托/成交查询真名（均无参数，内部遍历 context.accounts）**：
- `get_orders()` — 日内全部委托（gm/api/trade.py:336-358，docstring「查询日内全部委托」）
- `get_unfinished_orders()` — 所有未结委托（trade.py:311-333）
- `get_execution_reports()` — 执行回报（trade.py:464-487）
三者都在函数体里 `for account in context.accounts.values()` 逐账户查询合并，**不接受 account_id 过滤参数**（与掘金4 计划通识名 `get_orders(account_id)` 不同，见差异表）。返回项额外注入 `origin_module`/`origin_product` 两键（trade.py:329-330/354-355）。

**订单状态词汇表**（gm/enum.py:23-37 原文值）：

| 常量 | 值 | 语义 |
|---|---|---|
| OrderStatus_Unknown | 0 | 未知 |
| OrderStatus_New | 1 | 已报 |
| OrderStatus_PartiallyFilled | 2 | 部成 |
| OrderStatus_Filled | 3 | 已成 |
| OrderStatus_DoneForDay | 4 | （当日终结） |
| OrderStatus_Canceled | 5 | 已撤 |
| OrderStatus_PendingCancel | 6 | 待撤 |
| OrderStatus_Stopped | 7 | （停止） |
| OrderStatus_Rejected | 8 | 已拒绝 |
| OrderStatus_Suspended | 9 | 挂起 |
| OrderStatus_PendingNew | 10 | 待报 |
| OrderStatus_Calculated | 11 | — |
| OrderStatus_Expired | 12 | 已过期 |
| OrderStatus_AcceptedForBidding | 13 | — |
| OrderStatus_PendingReplace | 14 | — |

成交回报侧 `ExecType`（enum.py:4-21）：`ExecType_Trade=15`（成交）、`ExecType_New=1`（已报）、`ExecType_Canceled=5`、`ExecType_Rejected=8`、`ExecType_CancelRejected=19` 等。拒绝原因 `OrderRejectReason_*`（enum.py:39-55，如 `NoEnoughCash=2`、`NotInTradingSession=13`、`SymbolSusppended=16`）与撤单拒绝 `CancelOrderRejectReason_*`（enum.py:57-60）。ExecRpt dict 字段：`strategy_id, account_id, account_name, channel_id, cl_ord_id, order_id, exec_id, symbol, order_business, position_effect, side, ord_rej_reason, ord_rej_reason_detail, exec_type, price, volume, amount, commission, cost, properties, created_at, sno`。

### Q5 持仓/资金查询真名与返回字段

**真名是 `get_cash` / `get_position`（不是掘金4 风格的 `get_account_cash`/`get_account_positions`）**：
```python
# gm/api/query.py:1146-1147
def get_cash(account_id=None):
    """查询指定账户资金"""
# gm/api/query.py:1163-1164
def get_position(account_id=None):
    """查询指定账户所有持仓"""
```
- `get_cash(account_id)` → **单个 dict**（空结果返回 `{}`，query.py:1154-1160）。
- `get_position(account_id)` → **list[dict]**；每项注入 `credit_position_sellable_volume`（默认 0，query.py:1179-1184）。

**Cash dict 字段全集（`core.api.Cash`，account_pb2 descriptor 实测）——CAP 扣减三要素加粗**：
`account_id, account_name, channel_id, currency(int),` **`nav`（总权益，double）**`, pnl, fpnl, fpnl_diluted, frozen, order_frozen,` **`available`（可用资金）**`, balance,` **`market_value`（持仓市值）**`, cum_inout, cum_trade, cum_pnl, cum_commission, last_trade, last_pnl, last_commission, last_inout, change_reason, change_event_id, market_value_long, market_value_short, used_bail, enable_bail, created_at, updated_at`。
注意 `available` 是「可用」，`frozen+order_frozen` 是冻结；nav = 市值+余额口径由柜台定义，CAP 扣减建议用 `nav` 与 `available` 双读。

**Position dict 字段全集（`core.api.Position`）**：
`account_id, account_name, channel_id, symbol, side, volume, volume_today, vwap, vwap_diluted, vwap_open, amount, price, fpnl, fpnl_diluted, fpnl_open, cost, order_frozen, order_frozen_today, available, available_today, available_now, market_value, last_price, last_volume, last_inout, change_reason, change_event_id, has_dividend, covered_flag, properties, created_at, updated_at`。
A 股多头 `side=PositionSide_Long(1)`（enum.py:110-111）；持仓键为 `(symbol, side, covered_flag)` 三元组（storage.py:329-336）。

**另一条等价路径（缓存的账户对象）**：`context.accounts` → `Dict[account_id, Account]`（storage.py:213-221，property，首次访问触发 `_set_accounts`）；`context.account(account_id='')`（storage.py:223-230，**单账户且不传参时自动返回唯一账户**）；`Account.cash`（dict）/`Account.positions(symbol='', side=None)` / `Account.position(symbol, side)`（gm/model/account.py:26-38）。交易事件推送会自动刷新这两处缓存（cash_callback/position_callback，callback.py:491-524；断线重连也会全量重拉，callback.py:655-661）。**警示（同 Q1）**：`_get_account_info` 里资金/持仓请求失败是 `return` 静默跳过（storage.py:292-293/307-308），缓存可能残缺，关键时点应以 `get_cash`/`get_position` 直查为准。

### Q6 `history` 完整签名、返回列名与 33000 上限

**签名原文（gm/api/query.py:562-565）——注意 fields 与 adjust 之间还有 skip_suspended/fill_missing 两个参数**：
```python
def history(symbol, frequency, start_time, end_time, fields=None,
            skip_suspended=True, fill_missing=None, adjust=None,
            adjust_end_time='', df=False):
    # type: (str|List, str, str|Datetime|Date, str|Datetime|Date, str, bool, str, int, str, bool) -> List[Dict]|pd.DataFrame
    """
    查询历史行情
    """
```
- `symbol` 支持 str 或 list（逗号串）；`frequency`：`'1d'`/`'60s'`/`'tick'` 等（分钟以秒表示，`adjust_frequency` 把 `1m`→`60s`，gm/utils.py:275-281；history 内只 strip 不换算，**建议直接写 `60s` 风格**）；tick 走 `GetHistoryTicksReq`（query.py:577-593），bar 走 `GetHistoryBarsReq`（query.py:594-611）。
- `adjust`：ADJUST_NONE=0/ADJUST_PREV=1/ADJUST_POST=2（enum.py:134-136）；`adjust_end_time` 是**定点复权基准时刻**（官方文档示例：`adjust=ADJUST_PREV, adjust_end_time='2020-12-31'`，emquant.18.cn 提取数据研究示例）。
- `fields`：逗号串或 list，过滤返回列；不传=全字段。
- `df=False` 返回 `List[Dict]`；`df=True` 返回 `pd.DataFrame`。

**返回列名清单（bar）——三源一致的权威证据**：
1. `subscribe` 的 bar 全字段白名单（gm/api/basic.py:147，SDK 自己的列名校验表）：
   `["symbol","frequency","open","high","low","close","volume","amount","position","bob","eob","pre_close"]`
2. `data.api.Bar` protobuf 字段（data_pb2 descriptor 实测，序即上）：`symbol, frequency, open, high, low, close, volume(int64), amount(double), position(int64), pre_close(double), bob(Timestamp), eob(Timestamp)`
3. `BarLikeDict2` 类型存根（gm/csdk/c_sdk.pyi:34-47）：同名列 + `receive_local_time`（仅实时回调注入，history 不含）。
   ⇒ **history(df=True) 的列 = 上表 12 列**（`bob`=bar 开始时刻、`eob`=bar 结束时刻，均为北京时间 datetime；`volume` pb 为 int64、pyi 标注 float——SDK 源码未注明计量单位，A 股日线惯用口径为「股」，对拍任务（Task 10）需实测确认）。fields 指定时仅含指定列。tick 行情列为 Q3 的 ticknew 键集（少 `receive_local_time`）。

**33000 条上限的实现位置：不在 Python 层，也不在本地 C 二进制**。
- Python 源码与 c_sdk.pyd/gmsdk.dll/gmpytool.pyd 二进制扫描均无 `33000` 常量（实测）。
- 唯一书证：gm-3.0.186.dist-info/METADATA 变更记录 v3.0.162：「日内回测行情提取逻辑调整, 匹配单次33000条记录限制」——说明该限制是**服务端/数据服务的一次拉取上限**，SDK 只是适配。⇒ pilot 的 history 调用必须自行分页（按时间段切片，每片预估 ≤33000 行；60s 频率单标的约 65 个交易日一片、1d 频率约 130 年不受限）。

### Q7 跌停价来源（证券基本信息接口）

**结论：字段名 `upper_limit` / `lower_limit`（double），首选 `get_history_symbol`。**

```python
# gm/api/ds_instrument.py:118-122
def get_history_symbol(symbol, start_date="", end_date="", df=False):
    # type: (str, str, str, bool) -> List[Dict]|pd.DataFrame
    """
    查询指定标的多日交易信息
    """
```
- **单数 `symbol`**（一次一只，与掘金4 通识的复数不同）；返回 `instrument_service.Symbol` 列表，每项 `item.update(item.pop('info'))` 展开后字段（descriptor 实测）：`trade_date(datetime), is_adjusted, is_suspended, position, settle_price, pre_settle,` **`pre_close`**`, turn_rate, adj_factor, margin_ratio, conversion_price, exercise_price, multiplier` **`, upper_limit, lower_limit`** `, is_st` + info 内的 SymbolInfo 基本档（symbol/sec_name/exchange/price_tick/board/listed_date/delisted_date…）。
- ⇒ **跌停判定 = `lower_limit`（当日）；涨停 = `upper_limit`；昨收 = `pre_close`**；`is_suspended`/`is_st` 同源可拿。
- 替代路径：`get_history_instruments(symbols, fields=None, start_date=None, end_date=None, df=False)`（gm/api/query.py:244）批量查多标的，返回 `data.api.Instrument` 展开字典，同样含 `upper_limit/lower_limit/pre_close`（descriptor 实测：`upper_limit: double, lower_limit: double, pre_close: double`），并有列名换算 `exercise_price→strike_price`、`created_at→trade_date`（query.py:266-279）。**注意其注释「不要传 fields, 否则可能没有 info 字段」（query.py:254）——fields 过滤在 info 展开之后，但若服务端按 fields 裁剪会丢 info 内字段，取涨跌停时不要传 fields。**
- `get_instruments`（query.py:191，最新交易日的 Instrument）亦含同名字段（含 `is_suspended` 入参过滤）。
- tick/bar 推送与 `current`/`last_tick` **没有** upper/lower_limit 字段（Q3/Q6 字段集为证）——跌停价只能查证券信息接口，不能从行情流拿。

### Q8 token / 终端连接机制（只写机制；真实 token 由 Task 10 处理，本文档不出现任何 token 值）

**`set_token`**（gm/api/basic.py:98-104 原文）：
```python
def set_token(token):
    # type: (Text) -> None
    """
    设置用户的token, 用于身份认证
    """
    py_gmi_set_token(token)
    context.token = str('bearer {}'.format(token))
```
- token 亦可经 `run(token=...)` 传入（basic.py:598 `set_token(token)`；run 里另有 `apitoken` 走 `py_gmi_set_apitoken`，basic.py:599）。
- token 语义（东财官方文档原文）：「token绑定计算机的ID, 可在系统设置-密钥管理中生成」「set_token 设置用户token，如果token不正确, 函数调用会抛出异常」。
- 相关辅助：`get_encrypted_token()`（basic.py:107）/`get_orgcode()`（basic.py:117）/`set_mfp(mfp)`（basic.py:772，合规设备指纹）。**本机不搜不读任何 token 文件（C8，Task 10 职责）。**

**serv_addr 约定**：
- `set_serv_addr(addr)`（basic.py:764-769，docstring「设置终端服务地址」）；或 `run(serv_addr=...)`（basic.py:595-596）；或进程命令行 `--serv_addr=...`（gm/api/__init__.py:416-435，**import gm.api 时即 getopt 解析并立即 set_serv_addr**，:432-435）。
- 默认地址：Python 层无默认值常量；gmsdk.dll 二进制字符串实测含 **`127.0.0.1:7001`**——终端在本机 7001 端口提供 HTTP 服务（dll 内含 `/v3/...` REST 路由与 discovery 服务发现：`/v5/discovery/site-services-names`、`discovery.v5.api.GetAllSitesReq` 等）。⇒ **SDK 连的是本机终端，终端再连柜台/数据服务**；终端未启动 = 本机 7001 无服务。

**连接失败的报错形态（三层）**：
1. **同步 API 调用失败 → 抛 `GmError` 异常**（gm/api/_errors.py:24-31 原文）：
   ```python
   def check_gm_status(status):
       if status != 0:
           message = gmi_strerror(status).decode("utf8")
           ext_message = gmi_get_ext_errormsg().decode("utf8")
           if ext_message:
               message += "; " + ext_message
           function = sys._getframe().f_back.f_code.co_name
           raise GmError(status, message, function)
   ```
   `GmError.__str__` 输出 JSON：`{"status":..,"message":..,"function":..}`（_errors.py:16-21）。错误码中文文案可经 `get_strerror(error_code)`（basic.py:379-385）或 `gmi_strerror` 获取；错误码表文档链接见 callback.py:557（myquant.cn/docs/cpp/170）。
2. **运行期异步错误 → `on_error(context, code, info)` 回调**，payload 为 `"code|info"` 字符串按首个 `|` 拆分（callback.py:555-572；拆分失败按 code=1011 处理）。未定义 on_error 时默认仅 gmsdklogger.warning（callback.py:541-552），其中 **1200/1201 为行情重连提示**，不致命。
3. **连接/断连事件回调**：`on_trade_data_connected` / `on_trade_data_disconnected` / `on_market_data_connected` / `on_market_data_disconnected`（basic.py:628-636 注册；callback.py:655-749 分发）。交易服务重连时会自动全量重拉账户资金持仓（callback.py:657-661 注释原文：「当SDK连接远程的终端时如果跟终端意外断连…需要在每次重连交易服务时获取一遍完整的交易数据」）。
- 进程退出行为：`gm.api` import 时注册 `atexit` → `os._exit(0)`（api/__init__.py:402-405）——**策略进程退出不走优雅清理**，巡检/守护设计须据此考虑。

### Q9 策略运行形态（终端如何执行 main.py）

- **文件名约定：`main.py`**。东财官方文档示例统一 `run(strategy_id=..., filename='main.py', ...)`，注释原文：「filename文件名, 请与本文件名保持一致」。
- **`run()` 的 filename 参数语义 = 策略文件的模块导入路径**（gm/api/basic.py:574-587 原文）：
  ```python
    # 处理用户传入 __file__这个特殊变量的情况
    syspathes = set(s.replace('\\', '/') for s in sys.path)
    commonpaths = [os.path.commonprefix([p, filename]) for p in syspathes]
    commonpaths.sort(key=lambda s: len(s), reverse=True)
    maxcommonpath = commonpaths[0]
    filename = filename.replace(maxcommonpath, '')  # type: str
    if filename.startswith('/'):
        filename = filename[1:]
    if filename.endswith(".py"):
        filename = filename[:-3]
    filename = filename.replace("/", ".")
    filename = filename.replace('\\', ".")
    fmodule = import_module(filename)
  ```
  即：剥离 sys.path 最长公共前缀 → 去扩展名 → 路径分隔符转 `.` → `import_module`。**惯例是传 `__file__`**（注释「处理用户传入 __file__这个特殊变量的情况」），策略目录必须在 sys.path 上（终端以策略目录为 CWD/首路径拉起 python）。
- **终端注入命令行参数**：`gm.api` 在 import 时即解析 `sys.argv` 的长选项（gm/api/__init__.py:416-430）：`--strategy_id --filename --mode --token --apitoken --backtest_* --serv_addr --port`；`run()` 内部再用 OptionParser 二次解析同名参数（basic.py:420-516）——**命令行值优先于 run() 形参默认值**（`options.strategy_id` 覆盖）。⇒ 终端启动策略 = 以策略目录为工作目录拉起 python main.py，并注入 strategy_id/token/mode 等。
- **mode 终裁在 run()**：`mode not in (MODE_UNKNOWN, MODE_LIVE, MODE_BACKTEST)` 抛 ValueError（basic.py:520-521）；MODE_BACKTEST 才下发回测配置（basic.py:654-667）；MODE_LIVE 走 `gmi_init()` → `context._set_accounts()` → 主循环 `while running: gmi_poll()`（basic.py:673-686）——**实时模式是 SDK 内进程轮询，阻塞在 run() 直到 stop()**。
- 动态参数（终端 UI 交互）：`add_parameter`/`set_parameter`（basic.py:703-736），`on_parameter` 回调；`log(level, msg, source)`（basic.py:739）「只支持实时模式，在仿真交易和实盘交易界面查看，重启终端log日志会被清除」（东财文档原文）。

### Q10 交易日历函数真名与签名

```python
# gm/api/query.py:438-439
def get_trading_dates(exchange, start_date, end_date):
    # type: (str, str|Datetime|Date, str|Datetime|Date) -> List[str]
    """查询交易日列表 ... 返回 yyyy-mm-dd 格式的列表"""
# gm/api/query.py:465-466
def get_previous_trading_date(exchange, date):      # -> str('yyyy-mm-dd') | None
# gm/api/query.py:488-489
def get_next_trading_date(exchange, date):          # -> str | None
# gm/api/ds_instrument.py:228-229
def get_previous_n_trading_dates(exchange, date, n=1):   # -> List[str]（含 date 当日往前 n+1 个？见下）
# gm/api/ds_instrument.py:249-250
def get_next_n_trading_dates(exchange, date, n=1):       # -> List[str]
# gm/api/ds_instrument.py:146-147
def get_trading_dates_by_year(exchange, start_year, end_year):  # -> pd.DataFrame（年度日历）
```
- `exchange` 仅认 `{'SHSE','SZSE','CFFEX','SHFE','DCE','CZCE','INE','GFEX'}` 大写（`to_exchange`，gm/utils.py:213-220；非法返回 None）。
- `get_previous/next_trading_date` 查无结果返回 `None`（query.py:483-484/504-505）。
- `get_previous_n_trading_dates` 返回**列表**（含边界行为未在源码注明——是否含 date 自身取决于服务端，列入 live 验证清单）；日期入参支持 `yyyy-mm-dd`/`yyyymmdd`/date/datetime（`param_convert_date` 系，gm/api/_utils.py）。
- 时段查询（与日历互补）：`get_trading_times(variety_names)`（query.py:858，返回交易+集合竞价时段）、`get_trading_session(symbols)`（ds_instrument.py:174，指定标的可交易时段）。

---

## 三、与计划通识名差异表（冲突以本文档为准）

| # | 计划/通识名（或直觉写法） | 权威真名/事实 | 证据 |
|---|---|---|---|
| D1 | `MODE_SIMULATION` 仿真模式常量 | **不存在**。终端"仿真交易"= MODE_LIVE + 仿真柜台账户；SDK 无仿真语义 | enum.py:130-132；emquant 文档「仿真交易和实盘交易的启动策略按钮默认是实时模式」 |
| D2 | `get_account_cash(account_id)` | `get_cash(account_id=None)`，返回单 dict | query.py:1146 |
| D3 | `get_account_positions(account_id)` | `get_position(account_id=None)`，返回 **list[dict]** | query.py:1163 |
| D4 | `get_orders(account_id)` / `get_execution_reports(...)` 带账户参数 | `get_orders()` / `get_unfinished_orders()` / `get_execution_reports()` **均无参**，内部遍历全部绑定账户 | trade.py:336/311/464 |
| D5 | `on_tick(context, tick)` 里最新价 `tick.last_price` | **`tick.price`**（high/low/open 同名直取） | callback.py:184-201；c_sdk.pyi:18 |
| D6 | `order_volume(...)` 返回单 dict / 返回 cl_ord_id 字符串 | 返回 **`List[Dict]`**，取 `[0]['cl_ord_id']` | trade.py:87-112 |
| D7 | `order_cancel(cl_ord_id)` 传字符串 | 传 **dict/list[dict]**，键 `cl_ord_id`+`account_id` | trade.py:398-403 |
| D8 | `history(symbol, frequency, start_time, end_time, fields, adjust, adjust_end_time, df)` | 中间还有 `skip_suspended=True, fill_missing=None`；关键字传参可安全跳过 | query.py:562-564 |
| D9 | `get_history_symbols(...)` 复数 | `get_history_symbol(symbol, ...)` **单数、单标的** | ds_instrument.py:118 |
| D10 | 跌停价字段 `limit_down`/`dl` | **`lower_limit` / `upper_limit`**（double） | Symbol/Instrument descriptor 实测 |
| D11 | `schedule(context, func, ...)` 或 run 参数注册回调 | `schedule(schedule_func, date_rule, time_rule)` 注册时绑定；回调**只收 context**；注册机制=策略模块全局函数名 getattr | basic.py:388/610-638 |
| D12 | `on_bar(context, bar)` 单条 | `on_bar(context, bars)` **list** | callback.py:301 |
| D13 | `context.account.cash` 直取即可 | 可用，但缓存可能静默残缺（失败静默 return）；关键时点直查 `get_cash` | storage.py:213-230/292-293 |
| D14 | 账户类型字段（仿真/实盘标记）可查 | Python 层**查不到**（Account 仅 id/name 进 SDK；Type_Simulate 枚举未被消费）；只能账户 ID/名称白名单 | Q1 全节 |
| D15 | `run()` 里传 filename 绝对路径 | filename=模块路径（惯例传 `__file__`），需在 sys.path 上 | basic.py:574-587 |
| D16 | 枚举用 `OrderSide.Buy` 类属性 | 模块级常量 `OrderSide_Buy`（int 1） | enum.py:62-64 |
| D17 | history 有本地 33000 截断可配置 | 33000 是**服务端单次上限**（METADATA v3.0.162），本地二进制无此常量；须自行分页 | Q6 |
| D18 | `get_cash()` 不传 account 自动选唯一账户 | `get_cash(account_id=None)` 的 account_id 直通 pb（None→默认账户由服务端定）；**自动选唯一账户的是 `context.account()`**（不传参且单账户时） | query.py:1146-1152；storage.py:223-230 |
| D19 | token 每次调用显式传 | 进程级一次 `set_token`（或 run/命令行注入）；命令行优先级最高 | basic.py:98/598；api/__init__.py:416-435 |
| D20 | `order_target_percent` 等 target 族带 side 参数 | target 族参数是 `position_side`（非 side），且无 position_effect | trade.py:218-308 |

---

## 四、签名原文附录（可截断，未改写）

```python
# gm/api/basic.py:129-130
def subscribe(symbols, frequency=None, count=0, wait_group=False, wait_group_timeout='10s', unsubscribe_previous=False, fields=None, format="df"):
    # type: (GmSymbols, Text, int, bool, Text, bool, str, str) -> None

# gm/api/basic.py:217
def unsubscribe(symbols, frequency='1d'):

# gm/api/basic.py:264-265 / 312-313 / 353-354
def last_tick(symbols, fields="", include_call_auction = False):
def current(symbols, fields='', include_call_auction=False):
def current_price(symbols):

# gm/api/basic.py:388-391
def schedule(schedule_func, date_rule, time_rule):
    # type: (Any, Text, Text) -> None

# gm/api/basic.py:399-414
def run(strategy_id='', filename='', mode=MODE_UNKNOWN, token='',
        backtest_start_time='',
        backtest_end_time='',
        backtest_initial_cash=1000000,
        backtest_transaction_ratio=1,
        backtest_commission_ratio=0,
        backtest_commission_unit=0,
        backtest_slippage_ratio=0,
        backtest_marginfloat_ratio1=0.2,
        backtest_marginfloat_ratio2=0.4,
        backtest_adjust=ADJUST_NONE,
        backtest_check_cache=1,
        serv_addr='',
        backtest_match_mode=0,
        backtest_intraday=0,
        ):

# gm/api/basic.py:739-740 / 753 / 764-765 / 858-859 / 869
def log(level, msg, source):
def stop():
def set_serv_addr(addr):
def set_account_id(account_id):
    """预设账号"""
def set_option(max_wait_time=3600000, backtest_thread_num=1, ctp_md_info={}, bus={}):

# gm/api/trade.py:115-127（order_volume，见 §Q4 全文）
# gm/api/trade.py:311-313 / 336-338 / 361-362 / 374-375 / 398-399 / 454-455 / 464-465
def get_unfinished_orders():
def get_orders():
def order_cancel_all():
def order_close_all():
def order_cancel(wait_cancel_orders):
def get_account_id(name_or_id):
def get_execution_reports():

# gm/api/query.py:438-439 / 465-466 / 488-489
def get_trading_dates(exchange, start_date, end_date):
def get_previous_trading_date(exchange, date):
def get_next_trading_date(exchange, date):

# gm/api/query.py:562-565 / 614-616
def history(symbol, frequency, start_time, end_time, fields=None,
            skip_suspended=True, fill_missing=None, adjust=None,
            adjust_end_time='', df=False):
def history_n(symbol, frequency, count, end_time=None, fields=None,
              skip_suspended=True, fill_missing=None, adjust=None,
              adjust_end_time='', df=False):

# gm/api/query.py:191-192 / 244 / 289-290 / 1146-1147 / 1163-1164
def get_instruments(symbols=None, exchanges=None, sec_types=None, names=None,
                    skip_suspended=True, skip_st=True, fields=None, df=False):
def get_history_instruments(symbols, fields=None, start_date=None, end_date=None, df=False):
def get_instrumentinfos(symbols=None, exchanges=None, sec_types=None,
                        names=None, fields=None, df=False):
def get_cash(account_id=None):
def get_position(account_id=None):

# gm/api/ds_instrument.py:118-119 / 146-147 / 174-175 / 228-229 / 249-250
def get_history_symbol(symbol, start_date="", end_date="", df=False):
def get_trading_dates_by_year(exchange, start_year, end_year):
def get_trading_session(symbols, df=False):
def get_previous_n_trading_dates(exchange, date, n=1):
def get_next_n_trading_dates(exchange, date, n=1):

# gm/enum.py:130-136（核心常量）
MODE_UNKNOWN = 0
MODE_LIVE = 1
MODE_BACKTEST = 2
ADJUST_NONE = 0
ADJUST_PREV = 1
ADJUST_POST = 2

# gm/enum.py:62-68 / 113-115
OrderSide_Buy = 1  # 买入
OrderSide_Sell = 2  # 卖出
OrderType_Limit = 1  # 限价委托
OrderType_Market = 2  # 市价委托
PositionEffect_Open = 1  # 开仓
PositionEffect_Close = 2  # 平仓, 具体语义取决于对应的交易所
```

---

## 五、残余不确定性清单（需 live/终端实测收口，非源码可裁）

1. **Q2 盘前时刻**：`time_rule='09:15:00'` 是否如约在交易日内触发（源码无校验、文档无禁令；五阶段盘前阶段依赖）。
2. **Q10 边界语义**：`get_previous_n_trading_dates(exchange, date, n)` 返回是否含 `date` 当日本身（源码未注明，服务端定义）。
3. **Q6 列序与 volume 单位**：`history(df=True)` 的列顺序由 C 工具 `gmpytool.to_bars` 构造（闭源），列名三源一致但顺序以实测为准；`volume` 单位（股/手）源码未标注，由 Task 10 对拍裁定。
4. **Q1 仿真旁证的最后一环**：终端「仿真交易」入口创建的资金账户，其 `account_name`/`account_id` 命名规律（若含 `sim` 字样可作为弱白名单校验）——需终端 UI 实测一次后回填本文档。
5. **33000 分页实测**：超限时的具体报错形态（GmError 的 status/message 值）待首夜拉数时记录。

（完）
