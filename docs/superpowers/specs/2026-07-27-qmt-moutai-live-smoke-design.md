# QMT 茅台实盘冒烟测试设计（模拟盘）

> 日期：2026-07-27
> 状态：已批准（三轮确认）
> 关联：`trading/tools/qmt_live_smoke.py`（ETF 版既有冒烟脚本，步骤10 写死 ETF@5.0）
> 实现：`trading/tools/qmt_live_smoke_moutai.py`（新建）

## 1. 目标

趁开盘时段，用贵州茅台 `600519.SH` **真连 miniQMT 模拟盘柜台**，端到端验证
`QmtExecutionGateway`（`broker/qmt.py:225`）的挂单/下单/撤单/成交全链路 + 回调推送。

**核心标尺（用户原话）**：真实请求 + 交互把守 + **不是自嗨**。

## 2. 范围与约束

- **账户**：模拟盘（东北证券 NET 10110356 或 `.env` 配置），无真实资金风险
- **标的**：`600519.SH` 贵州茅台，100 股（A 股最小单位）
- **网关**：直连 `QmtExecutionGateway`，**不经 risk_shield / engine**（本次测网关本身，非策略引擎）
- **session_id**：默认 `123456`

## 3. 参数与价格策略

| 段 | 动作 | 挂单价 | 成交预期 | 价格来源 |
|---|---|---|---|---|
| 挂撤段 | 买单挂+撤 | **跌停价 `low_limit`** | 不成交（远离盘口） | `get_quote` |
| 成交段 | 买单即成 | **涨停价 `high_limit`** | 即成（撮合优先，实价≈现价非涨停价） | `get_quote` |
| 卖单挂撤（可选） | 卖单挂+撤 | **涨停价 `high_limit`** | 不成交（高价卖单） | `get_quote` |

**价格策略原理**：
- 跌停价是挂买单不成交的**最低合法价**（挂更低会被柜台拒单——超涨跌停区间）。
- 涨停价是挂买单的**最高合法价**，撮合优先级最高，几乎必然即成；A 股撮合按卖方最优价成交，故实价≈现价而非涨停价。
- 涨停价挂卖单同理不成交（除非涨停有人买）。

## 4. 动作序列

### 挂撤段（步骤 1-11，跌停价买单不成交）
```
 1. connect              → _connected=True, _lock_down=False, _main_push_available=True
 2. query_asset          → 确认模拟盘资金（打印 cash/total_asset）
 3. get_quote(600519.SH) → last_price / high_limit / low_limit
                           ★ 边界：last_price ≤ low_limit×1.001（已跌停）→ 跳过挂撤段
 4. query_orders/trades  → 测试前基线
 5. 【挂单】submit 买单 100 @low_limit → 期望 SUBMITTED + order_id      [源①]
 6. sleep(2) 等 on_order_stock_async_response + on_stock_order 首推      [源②]
 7. query_orders(cancelable_only=True) 找该 oid → 非终态, traded==0      [源③]
 8. 【撤单】cancel_order(order_id) → 期望成功                            [源①]
 9. sleep(2) 等 on_stock_order 推 CANCELLED                              [源②]
10. query_orders 找该 oid → 终态 CANCELLED, traded_volume==0             [源③]
11. 段内汇总
```

### 成交段（步骤 12-20，涨停价买单即成）
```
12. query_asset + _fetch_broker_positions → 成交前基线（cash、茅台 volume）
13. get_quote 取 high_limit
    ★ 边界：last_price ≥ high_limit×0.999（已涨停）→ 告警仍尝试
14. 【成交单】submit 买单 100 @high_limit → SUBMITTED                    [源①]
15. sleep(3) 等 on_stock_order(FILLED) + on_stock_trade                  [源②]
16. query_orders → state==FILLED, traded_volume==100                     [源③]
    ★ traded∈(0,100) → 部分成交，记录暴露 gap
17. query_trades → traded_price（实价≈现价）, traded_volume==100          [源③]
18. query_asset → cash ↓ ≈ 实价×100, market_value ↑                      [源③]
19. 原始 query_stock_positions（不过滤 can_use==0）→ 茅台 volume +100, can_use_volume==0（T+1 当日不可卖） [源③]
    注：_fetch_broker_positions(qmt.py:425) 会过滤 can_use==0（废弃/T+1冻结仓），当日买入不可见，故成交段用原始查询
20. 段内汇总（成交价/额/持仓变化/推送计数 trade≥1）
```

### 卖单挂撤段（步骤 21-25，有 T+1 解禁持仓才测）
```
21. 原始 query_stock_positions 查茅台 can_use_volume
22. can_use_volume ≥ 100 → 挂涨停价卖单 100 @high_limit → SUBMITTED     [源①]
    否则 → 打印"无可卖持仓（含本测试买入的100股T+1冻结）"跳过
23. sleep(2) + query_orders 确认在册可撤                                  [源②③]
24. cancel_order → CANCELLED                                              [源①]
25. query_orders 终态 CANCELLED, traded==0                                [源③]
```

## 5. 三源交叉验证（不自嗨核心）

每个真单动作后做三源对账，**任一源缺失/不一致 → ❌ 告警**：

| 动作 | 源①函数返回 | 源②柜台主推回调 | 源③主动查询复核 | 一致性校验 |
|---|---|---|---|---|
| 挂单 | `OrderResult.state` | `on_stock_order` 推送计数+1 | `query_orders` 查到该 oid | state 一致 |
| 撤单 | `cancel_order` 返回 | `on_stock_order` 推 CANCELLED | `query_orders` 终态 | ==CANCELLED |
| 成交 | — | `on_stock_trade` 推送 | `query_trades` + 持仓/资产前后差 | traded/持仓/资金三处对齐 |

**反自嗨铁律**：主推没来（subscribe 失败 / `_main_push_available=False`）也算真实结果，显式暴露并走 `query_orders` 兜底（顺带测 T5 惰性同步路径），**绝不靠函数返回值单方面宣布成功**。

## 6. 断言（Definition of Done）

- 步骤5 挂单 submit → `SUBMITTED`，order_id 非空
- 步骤6 收到 ≥1 条 `on_stock_order` 主推（源②）
- 步骤7 `query_orders` 查到该 oid，traded_volume==0（源③）
- 步骤10 撤单终态 `CANCELLED`，traded_volume==0
- 步骤16 成交 state==FILLED，traded_volume==100（部分成则记录暴露 gap）
- 步骤15 `on_stock_trade` 推送 ≥1（源②）
- 步骤17 `query_trades` 实价≈现价（非涨停价）
- 步骤19 持仓 +100，can_use_volume==0（T+1）

## 7. 边界告知（必须知情）

1. **市价单模拟盘不支持**：`submit_order` 源码注释明确「LATEST_PRICE 仅实盘生效，模拟环境不支持市价报单」。故成交段用涨停价限价单替代（撮合效果等同），**市价单链路本身仍是未验证 gap**，等真盘。
2. **T+1 留仓**：成交段买入 100 股茅台后，模拟盘账户多 100 股持仓，**当日不可卖**，次日才能平。这是测试真实代价。
3. **部分成交精度 gap**：memory 记录的「live 前必修 4 项」之一。高流动性茅台大概率一次性全成，若出现部分成交则如实记录暴露点（观察点，非阻塞）。

## 8. 失败处置（Grill Me 三连）

- **挂单即拒（REJECTED）**：打印 message，不继续后续动作，断开连接。查资金/价格合法性/账号限制。
- **撤单失败/超时**：**重试 1 次**（间隔 1s），仍失败 → 告警 + 保持连接人工介入（**绝不留可撤未撤废单**，养成纪律）。
- **主推缺失**（`on_stock_order` 计数未增）：警告 subscribe 可能失败，靠 `query_orders` 主动兜底验证（测 T5 路径）。
- **意外成交**（挂撤段 traded>0）：告警记录，**不撤**（已成单撤不了），如实汇报。

## 9. 交互把守

- 只读步骤（connect/query）：`input("回车继续")`
- 真单步骤（挂单/撤单/成交单/卖单）：`input("输入 YES 继续")` 严格把守，**绝不批量自动跑**

## 10. 运行

```
.venv310/Scripts/python.exe trading/tools/qmt_live_smoke_moutai.py
```

前置：miniQMT 客户端已启动并登录模拟盘 + `.env` 配 `QMT_USERDATA_PATH`/`QMT_ACCOUNT_ID`/`QMT_SESSION_ID`。
