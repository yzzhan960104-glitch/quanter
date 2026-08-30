# NECK 策略算法详解（2026-08-30 定稿版）

> 本文档面向需要理解策略完整逻辑的读者（开发者/运维/审计），逐层拆解从行情到持仓的全链路。
> 版本锚：PILOT_BUILD_STAMP `2987dc20` · 参数指纹 `7fe3d5b3f4a04786` · C2 冻结（内核逐字节不动）。

---

## 一、策略一句话

在创业板/科创板 300 只股票的日线上，识别"底部颈线突破"形态——价格经过充分压制后突破颈线阻力——
在突破日收盘确认信号，次日以限价买单等回踩入场，止损设在颈线下方 1×ATR，止盈设在颈线上方 1H/2H，
配以 5 日 trailing 收紧和最长持有 15 日的出场纪律。

---

## 二、信号识别（内核，C2 冻结）

### 2.1 形态定义（method_v0.detect_neckline_method）

在 60 根日 K 线的滚动窗口内，同时满足四个几何条件：

| 条件 | 参数（B3 值） | 含义 |
|---|---|---|
| 颈线聚集带 | tops 距均值 ≤ 1×ATR | 至少 3 个局部高点聚集成一条水平阻力线 |
| 底部确认 | 底部距最低点 ≤ 1×ATR | 至少 2 个局部低点确认支撑 |
| 压制度 | ≥ 0.5（衰减加权） | 窗口内 ≥50% 的收盘价在颈线下方（真压制非假突破） |
| H/ATR | ≤ 4.0 | 形态深度（颈线到底部）不超过 4 倍 ATR（防追高） |

### 2.2 信号产出

检测命中时产出 `Signal` 数据类：

```python
Signal(
    symbol,               # 股票代码
    neckline,             # 颈线价位（聚集带均值）
    bottom,               # 底部价位
    atr,                  # 14 日 ATR（Wilder RMA）
    entry_price,          # 挂单价 = 突破日收盘价（前复权）
    formed_at,            # 形态形成日（突破日）
    rr,                   # 预期盈亏比 = (tp2 - entry) / (entry - stop)
    exec_params,          # 执行参数快照（从 TRADE_CFG 透传）
)
```

### 2.3 参数快照（§0 定稿，指纹 7fe3d5b3）

```python
ID_PARAMS = {
    'window': 60,              # 识别窗口（根）
    'min_tops': 3,             # 最少聚集顶数
    'tops_window': 7,          # 局部极值窗口
    'local_extrema_window': 5, # 底部极值窗口
    'min_suppression': 0.5,    # 最低压制度
    'max_h_atr': 4.0,          # 最大形态深度
    'stop_atr_mult': 1.0,      # 止损 ATR 倍数
    'tp_h_mult': 2.0,          # 止盈 H 倍数（tp2 = neckline + 2H）
    'tp1_h_mult': 1.0,         # 一段止盈 H 倍数（tp1 = neckline + 1H）
    'tp1_portion': 0.5,        # 一段止盈比例（50% 仓位）
    'decay_tau': 20.0,         # 压制度衰减时间常数
}
EXEC_PARAMS = {
    'cooldown': 1,             # 同标的信号冷却（交易日）
    'cancel_thresh_mult': 1.0, # 撤单阈值（颈线 + 1H）
    'max_wait': 5,             # 限价单最长等待（交易日）
    'max_holding': 15,         # 最长持有（交易日）
    'grace': 5,                # trailing 起动宽限（日）
    'step': 0.1,               # trailing 每日收紧步长（×ATR）
    'floor': 0.5,              # trailing 收紧下限（×stop_atr_mult）
}
TRADE_CFG = {
    'pos_cap': 0.075,          # 单票仓位上限（7.5%）
    'sizing_mode': 'fixed',    # 定尺模式
    'kelly_fraction': 0.25,    # （保留字段，fixed 模式不消费）
}
```

---

## 三、信号过滤（amihud keep-top-5，2026-08-29 新增）

### 3.1 因子定义

**amihud60** = 过去 60 个交易日的 `|日收益率| / 成交额` 的均值（Amihud 非流动性指标）。
值越大 = 股票越不流动（一点点资金就能推动价格）。

### 3.2 过滤规则

```
当日信号数 ≤ 5 → 全保留（无抢槽竞争，豁免留痕 AMIHUD_FILTER_EXEMPT）
当日信号数 > 5 → 按 amihud60 降序（最不流动=质量侧最高）留前 5 只，其余剔除
有效值（拉取成功且有 ≥48 个有效日）< 5 → 全保留（数据断供 fail-open，豁免）
```

被剔信号逐笔落 `SIGNAL_FILTERED_AMIHUD` audit 事件（带 amihud60 值与截面日）。

### 3.3 验证链（五遍筛选，两窗确认）

| 窗口 | 结果 |
|---|---|
| 回测 universe 子集 15,456 笔 | 外层 Δ+17.8pp / 全期 Δ+2.3pp / 五年逐年全正 |
| 史前窗 2010-2020 universe 子集 7,027 笔 | 全期 Δ+0.78pp / 后半 Δ+0.41pp（时间外确认） |
| 同族对照 | kt=4 差 0.09pp 触线 / kt=6 过但 2024 微负 / kt=8 灭 |

### 3.4 部署规格

```python
AMIHUD_FILTER = {
    'enabled': True,
    'window': 60,        # amihud 滚动窗
    'min_days': 48,      # 最少有效日
    'keep_top': 5,       # 保留名额
}
```

数据来源：EOD 时对每只信号股拉取 60 日 close+amount（掘金终端 history API），
至 t_minus_1（=突破日，与识别内核同视野），策略进程内现算，零外部文件依赖。

---

## 四、挂单与执行

### 4.1 挂单时序

```
09:31 早盘跑：扫 T-1 数据（昨日收盘突破）→ 信号 → 过滤 → 挂单（今日盘中等回踩）
15:36 尾盘跑：扫 T  数据（当日收盘突破）→ 信号 → 过滤 → 挂单（次日盘中等回踩）
```

### 4.2 闸序（check_caps，逐单执行）

| 闸 | 规则 | 参数 |
|---|---|---|
| ① fail-closed | equity/持仓/挂额任一查不到 → 当日不挂 | — |
| ② CAP 总仓位 | 本单金额 > equity×CAP − 持仓市值 − 已挂额 → 拒 | CAP.txt（缺省 1.0） |
| ③ 单日新挂 | 当日有效新挂 ≥ 5 → 拒 | `PILOT_MAX_NEW_ORDERS_PER_DAY = 5` |
| ④ 单票金额 | 本单金额 > 7.5%×equity → 拒 | `PILOT_MAX_POSITION_PCT = 0.075` |

"有效新挂"只数在途（非终态）或有成交的单——死单（REJECTED/零成交 CANCELLED）不占额。

### 4.3 挂单参数

- **价格**：突破日收盘价（前复权）；若超当日涨停价则钳到涨停价（R6-13f）
- **数量**：`floor(equity × 0.075 / entry / 100) × 100`（整手约束）
- **撤单价**：`颈线 + cancel_thresh_mult × H`（价格穿越此线自动撤单——已涨太远不等了）
- **类型**：限价买单

### 4.4 等待与成交

限价单在 `max_wait`（5 交易日）内等回踩成交：
- 成交 → 转持仓管理
- 超时未成交 → 撤单（dead order 回补机制允许重挂）
- 涨穿撤单价 → 撤单（已走太远，入场性价比不够）

---

## 五、持仓管理（出场）

### 5.1 止损（trailing，离散日频）

```python
base_stop = neckline - stop_atr_mult × atr        # grace 期内固定
# grace（5 日）后每日收紧：
eff_mult = stop_atr_mult - (holding_days - grace) × step
eff_mult = max(eff_mult, floor × stop_atr_mult)   # floor=0.5 → 最低 0.5×ATR
stop = neckline - eff_mult × atr
```

止损价只在盘后重算一次（次日固定价，盘中不移动——A 股 T+1 适配）。

### 5.2 止盈

| 目标 | 公式 | 动作 |
|---|---|---|
| TP1 | neckline + tp1_h_mult × H | 平 50% 仓位（tp1_portion=0.5） |
| TP2 | neckline + tp_h_mult × H | 平剩余 100% |

### 5.3 超时

持有超过 `max_holding`（15 交易日）→ 全平。

### 5.4 单仓一次性模型

同一标的不允许并发持仓或在途买单——已持仓（remaining_qty > 0）或已有在途买向单
（OPEN∪CHASE∪UNKNOWN 状态）的标的不接新信号（SIGNAL_SKIP_HELD 留痕）。

---

## 六、去重与冷却

| 规则 | 参数 | 语义 |
|---|---|---|
| 跨日冷却 | cooldown=1 | 最近 1 个交易日内已产出信号的标的丢弃新信号（防同形态连续触发） |
| 单仓防重 | — | 已持仓/在途买单不接新信号 |
| 死单回补 | — | REJECTED/零成交 CANCELLED 的死单自动重挂（不占新额度） |

---

## 七、完整信号管线图

```
EOD 定时触发（09:31 / 15:36）
  │
  ├─ ① 存量管理：撤昨日超时单 / 超期平仓 / reconcile
  │
  ├─ ② 扫描（scan_done 幂等防重扫）
  │    for sym in UNIVERSE(300):
  │      fetch_df_upto(sym, t_minus_1)     ← 掘金终端 history API
  │      detect_signal(sym, df, params)     ← C2 冻结内核
  │      → Signal or None
  │      冷却/持仓去重 → 候选信号列表
  │
  ├─ ③ amihud keep-top-5 过滤（≤5 豁免 / >5 留前 5 / 有效不足豁免）
  │
  ├─ ④ 人工风控（RISK_BLOCK.flag 在场 → 跳过挂单段）
  │
  └─ ⑤ 逐单挂限价买（check_caps 四闸 → place_limit_buy）
       → audit: SIGNAL / SIGNAL_FILTERED_AMIHUD / AMIHUD_FILTER_EXEMPT
                 / ORDER_PLACED / ORDER_BLOCKED
```

---

## 八、回测口径与实盘对齐

| 维度 | 回测 | 实盘 | 对齐锚 |
|---|---|---|---|
| 识别 | 同一内核（逐字节） | 同一内核（组装产物逐字块） | test_kernel_byte_equivalent |
| 数据 | 湖前复权（定点 adj） | 掘金终端定点前复权（adjust_end_time 钉死） | fetch_df_upto 逐参对拍 |
| 组合模拟 | PositionModel（整手/min5/冻结/4 或 5 并发） | 同参数（§0 TRADE_CFG + 硬闸） | portfolio_metrics 单源 |
| 排队 | random 21 种子中位（可部署标准） | 到达序（等价 random） | — |
| 摩擦 | 整手 100 股 + min5 佣金 + 5bps 滑点 | 仿真柜台同语义 | — |

---

## 九、性能指标（2026-08 回测，可部署口径）

| 指标 | 值 | 口径 |
|---|---|---|
| 全期年化（2021-2026） | +8.8% | random 21 种子中位 |
| 外层 2026 年化 | +45.5% | 过滤后（keep-top-5） |
| 逐年方向 | 五年全正 | 2022~2026 |
| 最大回撤 | ~-20% | 全期 |
| 信号量 | ~65 笔/年（过滤后） | universe 300 只 |
| 实际成交 | ~60 笔/年 | 4-5 并发约束下 |

---

## 十、验证纪律（改动红线）

1. **C2 冻结**：识别内核（method_v0.py + signal.py）逐字节不动——改了指纹就变
2. **参数指纹**：`PARAMS_FINGERPRINT = '7fe3d5b3f4a04786'`——改动须全量重验
3. **过滤语义**：keep-top-5 的任何变更（keep_top 数/窗口/方向）须重过四闸
4. **universe 约束**：任何过滤/分层改动须在实盘 universe（300 只）上重验，不可从混合池迁移
5. **两窗验证**：回测 universe 子集 + 史前窗（2010-2020）时间外确认
6. **实盘终审**：统计上的最终信心只能靠实盘双轨逐日积累
