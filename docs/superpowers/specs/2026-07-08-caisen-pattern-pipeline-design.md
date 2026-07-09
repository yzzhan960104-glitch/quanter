# 蔡森多空转折形态学流水线 · 设计规格 (Spec)

> 日期：2026-07-08
> 状态：待审查 → 通过后转 writing-plans
> 范围决策：**删除归因回测 / 因子 / 策略三大模块，全栈专精蔡森形态学这一种策略。**

---

## 0. 背景与目标

将现有"多策略量化平台"收敛为"蔡森多空转折（纯多头）形态学专用系统"。采用 **T 日收盘离线分析 → 人工审核 → T+1 及后续自动化执行** 的半自动架构，针对 A 股 / ETF，在 4h / 日线 / 周线级别识别头肩底与 W 底。

核心工程约束（CLAUDE.md）：
- **无未来函数**：形态识别必须因果化，禁止 `scipy.signal.argrelextrema` 等全局后向方法。
- **极简显式**：拒绝黑盒，向量化优先，数学逻辑平铺直叙。
- **风控极度拷问**：流动性枯竭、断线/部分成交、逼空/保证金不足三连拷问必须显式处置。
- **全中文**：沟通、文档、代码注释 100% 中文。

### 0.5 与蔡森原著《多空轉折一手抓》方法学的对齐

> 详见 `docs/caisen-methodology-summary.md`。本 spec 的形态识别、止盈、量价、假突破逻辑均以蔡森原著方法学为准绳（CLAUDE.md 事实审查，杜绝幻觉）。原著核心要点：

- **满足计算（目标价测量）**【蔡森灵魂】：止盈不是随意等距，而是「颈线满足计算」——`目标 = 颈线 + (颈线 − 谷底)`，含 1 倍/2 倍多级满足点（对应"幅宽与张力"）。
- **打底 ABC 波**【实战篇二】：底部构筑过程（A 跌 / B 弹 / C 末跌），C 波末跌才是真多方转折点。识别"过程"而非仅"静态 W 底"，避免把下跌中继误判为 W 底。
- **破头锅**【技术篇二】：突破前头部压力区（"锅"=套牢区）的多头买进形态，与 W 底并列的核心买讯。
- **多方转折点**【实战篇四·书名核心】：底部反转确认点，打底完成 + 颈线突破 + 量能共振。
- **形态失败（形态完成）**【实战篇十】：假突破视为反向信号——spec 假突破防御的蔡森原典依据。
- **量价观念**【推荐序点题"精準量價觀念"】：量价为硬过滤基础（突破放量、右底缩量）。

> 标注规则：基于原著目录/前言/插图直接读到的为**设计方向**；精确公式/规则在实现阶段读对应章节正文确认（见总结文档第 9-10 节"待精读清单"）。

---

## 1. 架构总览

### 1.1 核心数据流

```
DataLakeReader (jqdata 4h / akshare 日线 / 日线聚合周线)
  └─ 流动性过滤 (近30日均成交额 ≥ 1亿)
  └─ RiskManager
       ├─ 宏观仓位系数 (core/macro_regime.CreditRegime: +1→1.0 / 0→0.6 / -1→0.0)
       └─ 微观波动率过滤 (HV 分位 > 0.95 剔除无序震荡)
  └─ PatternScreener (因果 ZigZag → W底/头肩底, 跨度>10bar, 量价配合)
  └─ TradePlanGenerator (回踩入场/结构止损/等距止盈/盈亏比≥3/时间止损)
       └─ 落 plans JSON → 前端 CaisenScreenView 人工审核 (交互式 K 线标注)
            └─ 激活 → Celery beat 盘中监控
                 └─ ExecutionEngine (回踩触发→EMT挂单 / 止盈止损/时间止损监控)
```

### 1.2 实盘上线前置 Gate

`caisen/backtest_replay.py` 历史回放验证器：用历史日线滚动跑完整流水线（识别→计划→模拟成交→模拟离场），输出 **胜率 / 平均盈亏比 / 最大回撤 / 命中分布**。**未通过验证器门槛指标的参数组合，禁止激活实盘执行。**

---

## 2. 模块删除 / 迁移 / 保留清单

### 2.1 完全删除（蔡森不需要，无保留代码依赖）

| 路径 | 说明 |
|---|---|
| `backtest/` | 通用回测引擎（engine / cost_model / metrics / stress_test）整体删除 |
| `strategies/` | 多策略体系（base / loader + HMM / MA交叉 / 技术宏观融合）整体删除 |
| `factors/` 因子体系 | technical / hmm_macro / fusion(TargetWeightSignal) / mytt / analyzer / micro_momentum / alternative / base(FactorLoader) / exploratory_momentum / fundamental / macro / `__init__` 全删 |
| `server/api/v1/backtest.py` `strategies.py` `factors.py` `explorer.py` | 对应路由删除 |
| `server/services/backtest_service.py` `strategy_service.py` `factor_service.py` | 对应 service 删除 |
| `server/schemas/backtest.py` `strategy.py` `factor.py` | 对应 schema 删除 |
| `viz/report.py` | 回测报告（依赖 backtest/factors），删除 |
| 根 `config.py` | `BACKTEST_CONFIG` / `FACTOR_CONFIG` 配置块删除 |
| `server/core/config.py` | `BACKTEST_DEFAULTS` / `PORTFOLIO_DEFAULTS` / `API_CONFIG` 回测相关删除 |
| 测试 | `test_backtest` / `test_factors` / `test_strategy` / `test_mytt` / `test_engine_minute` / `test_engine_events` / `test_factor_analyzer` / `test_exploratory_momentum` / `test_micro_momentum` / `test_final_fixes` / `test_hmm_macro` / 根 `test_hmm_macro.py` 删除；其余因子相关测试（`test_macro_api` / `test_akshare_north_dragon` / `test_sync_fundamentals` 等）**执行时逐文件核实是否纯因子依赖**，是则删 |

### 2.2 迁移（被保留代码依赖，改路径不删逻辑）

| 原路径 | 新路径 | 说明 |
|---|---|---|
| `factors/macro_regime.py` (CreditRegime) | `core/macro_regime.py` | 纯函数，仅依赖 `data.lake_reader` macro 湖 + pandas，与 FactorLoader 零耦合 |

迁移后改 import 的保留文件：
- `trading/execution_gateway.py`（`MacroAwareGateway`）
- `server/api/v1/macro.py`（宏观驾驶舱端点）
- `scripts/sync_macro_credit.py`（宏观数据同步）
- `tests/test_macro_regime.py` / `tests/test_sync_macro_credit.py`

### 2.3 改造（保留但需清理耦合）

| 路径 | 改造内容 |
|---|---|
| `server/main.py` | 删 `StrategyLoader` / `FactorLoader` scan；删 `backtest/strategies/factors/explorer` 路由挂载；保留 `macro/trading/data/review/logs/portfolio` 路由；新增 `caisen` 路由 |
| `server/celery_app.py` | 删 `run_factor_grid` 及其线程池降级；新增蔡森 beat 三任务 |
| `server/services/portfolio_service.py` | **精确切分**：保留持仓/资产展示能力，删除 HMM 组合回测调仓部分（执行时逐函数核定） |
| `server/api/v1/portfolio.py` | 视 portfolio_service 切分结果同步收敛 |
| `web/src/router/index.ts` | 删 StrategyArchitect / backtest / factors 路由；新增 `/caisen` |

### 2.4 保留（蔡森核心依赖，不动）

- `trading/`：`execution_gateway`(BaseExecutionGateway/MacroAwareGateway/reconcile) / `emt_gateway` / `qmt_gateway` / `mock_broker` / `risk_shield` / `order_state` / `qmt_market_data`
- `data/`：`lake_reader` / `fetcher` / `lake_fetcher` / `cleaner` / `resilience` / `clients/*`
- `core/notifier.py`（+ 迁入 `core/macro_regime.py`）
- `server/api/v1/`：`macro` / `trading` / `data` / `review` / `logs` / `portfolio`(持仓展示)
- `server/services/trading_service.py`（`submit_order` + `check_order` 10关 + `record_live_trade`）
- 前端：Cockpit 交易 UI / 宏观驾舱 / AI 复盘 / 数据湖资管 / `--qt-*` design token

### 2.5 新增（蔡森专用）

```
caisen/
├── __init__.py
├── config.py              # StrategyConfig (Pydantic) —— 全参数真相源
├── risk.py                # RiskManager（宏观仓位系数 + 微观波动率过滤 + 仓位分配）
├── patterns/
│   ├── __init__.py
│   ├── zigzag_causal.py   # 因果化 ZigZag：zigzag 包末尾 pivot 滞后确认（未来函数隔离）
│   ├── neckline.py        # 颈线：trendln 线性回归 + numpy polyfit 双校验
│   ├── w_bottom.py        # W 底识别（纯数学比较 + 量价配合）
│   ├── head_shoulder.py   # 头肩底识别
│   └── screener.py        # PatternScreener：编排上述，输出候选 DataFrame
├── plan.py                # TradePlanGenerator：候选 → 结构化交易计划
├── execution.py           # ExecutionEngine：盘中监控 + 条件单状态机
├── storage.py             # 计划 JSON 持久化（plans/ + 激活/成交/平仓状态）
├── backtest_replay.py     # 蔡森专用历史回放验证器（上线 gate）
├── viz_static.py          # mplfinance 静态标注图（钉钉/邮件推送）
└── viz_interactive.py     # lightweight-charts 交互数据装配

server/api/v1/caisen.py        # 路由: scan/plans/activate/chart/positions/replay
server/services/caisen_service.py  # 编排: 扫描/计划CRUD/激活/执行调度
server/schemas/caisen.py       # Pydantic 契约

web/src/views/CaisenScreenView.vue  # 审核视图（路由 /caisen）
web/src/api/caisen.ts             # 前端 API 封装

tests/caisen/                  # 全套单测（含未来函数回归测试）
```

---

## 3. StrategyConfig 参数化模型

Pydantic 模型，`json_schema_extra` 带 `ui.control/group/step`，前端表单直消费。**严禁逻辑代码硬编码任何阈值。**

```python
class StrategyConfig(BaseModel):
    # —— 时间跨度类 ——
    min_pattern_bars: int = Field(11, ge=11, ...)        # 形态最小跨度(>10 硬约束)
    max_pattern_bars: int = Field(60, ge=20, le=120, ...) # 形态最大跨度
    symmetry_tolerance: float = Field(0.3, ...)           # 左右结构时间对称容忍度

    # —— 空间高度类 ——
    zigzag_threshold_atr: float = Field(1.0, ge=0.5, ...) # ZigZag 波段阈值(倍 ATR)
    min_pattern_depth: float = Field(0.03, ...)           # 形态最浅幅度
    max_pattern_depth: float = Field(0.30, ...)           # 形态最深幅度(防失效长趋势)
    w_price_tolerance: float = Field(0.02, ...)           # W 底两底价格高度容忍度

    # —— 量价配合类 ——
    right_vol_shrink: float = Field(0.8, ...)             # 右底缩量比例上限
    breakout_vol_multiplier: float = Field(1.5, ...)      # 突破颈线成交量放大倍数

    # —— 交易执行类 ——
    pullback_window_bars: int = Field(3, ...)             # 突破后有效回踩 K 线数
    pullback_max_pct: float = Field(0.02, ...)            # 回踩至不高于突破点 2%
    stop_loss_atr_buffer: float = Field(0.3, ...)         # 止损点 ATR 缓冲垫
    min_rr_ratio: float = Field(3.0, ...)                 # 盈亏比下限(25%胜率期望为正)

    # —— 时间止损/超时离场 ——
    max_holding_bars: int = Field(15, ...)                # 最大持仓周期
    timeout_exit_threshold: float = Field(0.01, ...)      # 超时离场浮盈阈值(1%)
    trailing_activation_bars: int = Field(5, ...)         # 移动止盈激活持仓天数
    trailing_to_breakeven: bool = Field(True, ...)        # 激活后止损上移至盈亏平衡

    # —— 风控类 ——
    liquidity_min_amount: float = Field(1e8, ...)         # 近30日均成交额下限(1亿)
    hv_window: int = Field(20, ...)                       # 历史波动率窗口
    hv_max_quantile: float = Field(0.95, ...)             # HV 异常分位上限
    max_position_pct: float = Field(0.05, ...)            # 单标的占总资金上限 5%
    macro_regime_veto: bool = Field(True, ...)            # 宏观收缩期是否一票否决新开仓
    confirm_bars: int = Field(3, ...)                     # ZigZag 末尾 pivot 滞后确认窗口

    # —— 蔡森方法学专用 ——
    neckline_projection_multiple: float = Field(1.0, ge=0.5, le=3.0, ...)  # 颈线满足计算倍数(1倍/2倍满足点)
    abc_wave_detect: bool = Field(True, ...)              # 启用打底 ABC 波过程识别(防下跌中继误判)
    pattern_tension_ratio: float = Field(0.4, ...)        # 幅宽张力:形态高度/宽度比例下限(爆发力约束)
    enable_pot_breakout: bool = Field(True, ...)          # 启用"破头锅"突破前头部形态(与W底并列买讯)
```

---

## 4. RiskManager（事前风控）

与既有 `risk_shield.check_order`（事中拦废单）、`MacroAwareGateway`（事中 regime 否决）互补，本模块是**事前**：决定能否开、开多大、标的是否被过滤。

```python
class RiskManager:
    def macro_position_coef(self, date) -> float:
        """宏观仓位系数 0~1。复用 core.macro_regime.CreditRegime.compute(date):
        regime=+1→1.0, 0→0.6, -1→0.0(macro_regime_veto=True 时直接 0)。"""

    def micro_filter(self, price_df, symbol) -> tuple[bool, str]:
        """微观波动率过滤。pandas-ta ATR/HV 计算;近 hv_window 的 HV 分位
        > hv_max_quantile → 剔除(无序震荡)。返回(是否通过, 原因)。"""

    def liquidity_filter(self, price_df) -> bool:
        """近30日平均成交额 ≥ liquidity_min_amount。volume 不 ffill(复用 DataLakeReader 语义)。"""

    def position_size(self, aum, entry, stop, coef) -> float:
        """固定风险分配: 份额 = (aum * max_position_pct * coef) / (entry - stop),
        再被 max_position_pct 硬钳,A股向下取整到100整手(复用既有契约)。"""
```

**拷问三连处置**：①逼空——`macro_regime_veto` 收缩期直接 0 仓位；②保证金不足——`position_size` 被 `max_position_pct` 硬钳；③波动率极端——`micro_filter` 剔除 HV 异常标的。

---

## 5. PatternScreener（核心 · 未来函数隔离 · 假突破防御）

### 5.1 未来函数隔离契约（对 zigzag 包的审计修正）

`zigzag.peak_valley_pivots(close, up_thresh, down_thresh)` 是**全局后向算法**：T 是否为极值取决于 T 之后是否出现足够幅度反向运动。直接用于"T 日识别 → T+1 买入"是**未来函数**——回测虚高、实盘失效。

**隔离层 `zigzag_causal.py`**：
- 历史**已完成** pivot（T 日之前已被后续反转确认）→ 无未来函数，用 zigzag 包提取主干波段。
- 末尾**未确认** pivot（最近一个极值，其后尚无足够反向运动）→ **滞后确认**：该 pivot 之后须存在 ≥ `confirm_bars`(默认3) 根 K 线且未创新极值，才认定为有效 pivot；否则视为"未成形"，不作形态右底/右肩。
- zigzag 包仅用于"中段去噪"，末尾确认走自写因果逻辑，**末尾未来函数被切断**。
- 回退路径：zigzag 包安装失败时，自写因果 ZigZag（pandas 滚动极值 + ATR 阈值），保底无黑盒。

```python
def causal_pivots(close, atr, cfg) -> pd.Series:
    """返回每点 pivot 标记(1=峰,-1=谷,0=非),严格因果(无未来函数)。
    1. zigzag 包提取全部 pivot(含末尾未确认)。
    2. 最后一个 pivot 若距序列末尾 < confirm_bars → 标 0(未成形,丢弃)。
    3. 每个 pivot 在其发生时刻 t 仅依赖 t 之前数据 + t 之后 confirm_bars 内确认
       (确认窗口是"已发生"的,T 日收盘看 T-1 及之前,合法)。"""
```

### 5.2 W 底数学化识别（跨度 > 10 bar）

给定因果 pivot 序列，W 底 = 4 个有序 pivot `[P1(谷), P2(峰/颈线), P3(谷/右底), P4(峰/突破确认)]`：

1. **跨度**：`P4.index - P1.index ∈ (min_pattern_bars, max_pattern_bars]`，硬约束 `>10`。
2. **结构**：P1、P3 为谷；P2 为峰且在 P1、P3 之间；`|P3.price - P1.price|/P1.price ≤ w_price_tolerance`（两底等高）。
3. **幅度**：`(P2.price - min(P1,P3).price)/min(P1,P3).price ∈ (min_pattern_depth, max_pattern_depth]`。
4. **对称性**：`|（P3-P2 区间）-（P2-P1 区间)|/形态跨度 ≤ symmetry_tolerance`。
5. **量价**：`volume[P3] ≤ volume[P1]*right_vol_shrink`（右底缩量）；`volume[P4] ≥ mean(volume[P2..P3])*breakout_vol_multiplier`（突破放量）。
6. **颈线**：P2 与前峰连线线性回归（trendln + numpy polyfit 双校验），P4 收盘 > 颈线价 → 突破成立；**颈线斜率 ≥ 0**（水平或上倾才健康，下倾直接否决）。
7. **打底 ABC 波**【蔡森实战篇二】：`abc_wave_detect=True` 时，要求 P1→P3 的下跌段可分解为 A 跌 / B 弹 / C 末跌三波，且 P3（右底/C 波末跌）为整个下跌段的最低或接近最低——防止把"下跌中继的反弹"误判为 W 底右底。
8. **幅宽与张力**【蔡森实战篇六】：`形态高度 / 形态宽度 ≥ pattern_tension_ratio`（高度=颈线−谷底，宽度=P4−P1 跨度归一化）；张力不足的扁平形态爆发力差，剔除。双底两底价格关系：右底 ≥ 左底×(1−w_price_tolerance)（右底可略高，"抬高底"更强）。

### 5.3 头肩底数学化识别

6 个 pivot `[P1(峰), P2(谷/左肩), P3(峰/头颈), P4(谷/头), P5(峰/右肩颈), P6(谷/右肩)]`：P4 为头底（最低），P2、P6 为左右肩底（等高且高于头底），颈线 = P3-P5 连线。

### 5.4 假突破防御

- **回踩确认**：突破后**不立即追入**。挂单条件 = "回踩至不高于突破点 `pullback_max_pct`(2%)" + "回踩发生在 `pullback_window_bars`(3天) 内"。超窗未回踩 → 计划失效（防假突破后一路上行踏空，宁可错过不追高）。
- **量能验证**：突破 K 线成交量须 ≥ `breakout_vol_multiplier`，否则无量假突破，计划丢弃。
- **颈线斜率**：下倾颈线直接否决。
- **形态失败（形态完成）反向信号**【蔡森实战篇十】：假突破（突破颈线后回落跌破右底/颈线）不只是一个"丢弃"动作——蔡森将其视为"形态完成"的反向信号。本项目纯多头不做空，故实现为：假突破的标的进入**冷却黑名单**（如 20 个交易日不再纳入候选），避免反复在同一个失效形态上消耗。

---

## 6. TradePlanGenerator

```python
@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    symbol: str
    pattern_type: str            # w_bottom / head_shoulder
    formed_at: pd.Timestamp
    breakout_price: float
    entry_upper: float           # 回踩买入区间上沿 = breakout
    entry_lower: float           # = breakout * (1 - pullback_max_pct)
    stop_loss: float             # 结构性: min(P3右底, 突破K线低点) - stop_loss_atr_buffer*ATR
    take_profit: float           # 颈线满足计算【蔡森】: 颈线 + (颈线-谷底) * neckline_projection_multiple
    take_profit_2x: float        # 二级满足点(2倍投影),分批止盈用
    rr_ratio: float              # 校验 ≥ min_rr_ratio 才输出
    valid_until: pd.Timestamp    # = formed_at + pullback_window_bars 交易日
    max_holding_until: pd.Timestamp
    timeout_exit_threshold: float
    shares: int
    metadata: dict               # 量价/形态参数快照,供复盘
```

- **止盈 = 颈线满足计算**【蔡森技术篇一】：`take_profit = 颈线价 + (颈线价 − 谷底价) × neckline_projection_multiple`（默认 1 倍满足，可调 2 倍）。这是蔡森区别于"随意等距"的数学内核——目标价由形态自身的颈线高度科学测量。支持多级满足点（幅宽张力）：1 倍满足平半仓、2 倍满足平剩余（移动止盈保护剩余利润）。
- **盈亏比校验**：`rr = (take_profit − entry_upper)/(entry_upper − stop_loss) ≥ min_rr_ratio(3.0)`，否则丢弃。颈线满足计算天然提供科学目标，`min_rr_ratio=3` 进一步确保 25% 胜率下数学期望为正。
- **输出排序**：按近 30 日成交额降序，落 `plans/<date>.json` + 候选 DataFrame。

---

## 7. ExecutionEngine（盘中监控 + 条件单状态机）

A 股无原生 OCO 条件单，自建状态机：

```
PENDING_APPROVAL → APPROVED → ARMED(待回踩) → FILLED(已持仓) → CLOSED
                      ↓ 超valid_until         ↓ 止损/止盈/时间止损
                   EXPIRED                   CLOSED
```

- **ARMED→FILLED**：Celery beat 每 60s 取行情（`qmt_market_data.get_quote`），若 `low ≤ entry_upper` 且 `high ≥ entry_lower` → 限价单挂 `entry_upper`，走 `trading_service.submit_order` 过 10 关风控 + EMT 网关。
- **FILLED→CLOSED**：持仓后 beat 持续监控，三离场并联：
  - 止损：`low ≤ stop_loss` → 市价卖
  - 止盈：`high ≥ take_profit` → 市价卖
  - 时间止损：`持仓 bar ≥ max_holding_bars` 且 `浮盈 < timeout_exit_threshold` → 市价平
  - 移动止盈：`持仓 bar ≥ trailing_activation_bars` 且 `trailing_to_breakeven` → 止损上移至盈亏平衡点
- **断线/部分成交**：复用 `OrderState` 状态机 + `reconcile()` 对账；断线 `_lock_down` 触发 `check_order` 关1拒单，beat 遇 `is_locked` 跳过本轮不补发。

---

## 8. backtest_replay 蔡森专用验证器（上线 gate）

```python
def replay(price_data, cfg, start, end) -> ReplayReport:
    """历史日线滚动回放:每个交易日 T 用 T 及之前数据跑 PatternScreener,
    生成计划后在 T+1 模拟回踩成交(若触及区间),模拟止盈止损/时间止损离场,
    严格无前视(T 日决策只用 T 及之前数据)。
    输出: 胜率 / 平均盈亏比 / 最大回撤 / 命中数 / 形态分布 / 月度收益。"""
```

- **gate 阈值**（写入 StrategyConfig 或独立 `ReplayGate`）：胜率、平均盈亏比、最大回撤需同时达标，参数组合方可激活实盘。
- 复用 `PatternScreener` / `TradePlanGenerator` / 状态机离场逻辑，与实盘共用同一套因果化代码，**保证回放与实盘语义一致**（杜绝"回测算一套、实盘跑另一套"的双源真理）。

---

## 9. server API + Celery beat

### 9.1 新增路由（`server/api/v1/caisen.py`）
- `POST /api/v1/caisen/scan` → 投 Celery `caisen.scan_universe` 异步全市场扫描
- `GET /api/v1/caisen/plans?status=pending` → 列候选计划
- `PATCH /api/v1/caisen/plans/{id}` → 人工审核（approve/reject/微调 entry/stop）
- `GET /api/v1/caisen/plans/{id}/chart` → lightweight-charts 数据（K线 + 颈线/形态点/止盈止损标注）
- `POST /api/v1/caisen/plans/{id}/activate` → 进 ARMED 态
- `GET /api/v1/caisen/positions` → 当前形态学持仓（富化 `record_position_attribution`）
- `POST /api/v1/caisen/replay` → 触发历史回放验证器

### 9.2 Celery beat（`server/celery_app.py` 追加，原项目无 beat）
- `caisen.scan_universe`：T 日 15:30 全市场扫描
- `caisen.monitor_pullback`：交易时段每 60s 监控 ARMED 计划回踩
- `caisen.monitor_holding`：交易时段每 60s 监控 FILLED 持仓离场
- Redis 不可用时降级线程池（复用既有 `explorer` 降级模式）

---

## 10. 前端

`CaisenScreenView.vue`（路由 `/caisen`，复用 `--qt-*` token）：
- 左：候选计划列表（按成交额降序，徽章 `pattern_type` / `rr_ratio`）
- 右：lightweight-charts 交互 K 线（标注 W 底四点、颈线、回踩区间、止损止盈线）
- 底：参数表单 + approve/reject/微调按钮
- 独立 tab：历史回放验证器结果展示（胜率/盈亏比/回撤图表）

前端清理：删 `StrategyArchitectView` 及 backtest/factors API 与组件；保留 Cockpit / 宏观驾舱 / AI 复盘 / 数据湖资管。

---

## 11. 可视化层

- `viz_static.py`：mplfinance `mpf.plot(..., alines=[颈线, W底连线])` 静态 PNG，接 `core.notifier` 推钉钉/邮件（T 日晚报）。
- `viz_interactive.py`：装配 lightweight-charts JSON（K线 + markers 形态点 + priceLines 止盈止损），供前端与本地 HTML 消费。
- 与既有 `viz/interactive.py`（plotly 净值/回撤）职责正交。

---

## 12. 错误处理与测试

### 12.1 错误处理（边界审查）
- 行情缺失/停牌 → `get_quote` 返 None，beat 跳过本轮（不补发废单）
- 部分成交 → `OrderState` 状态机 + `reconcile()` 对账，drifted>阈值 触发 notifier
- NaN/Inf → `micro_filter` 与 ATR 计算显式 `dropna` + 除零保护
- Celery/Redis 不可用 → 降级线程池，scan 走同步

### 12.2 测试（pytest，与既有风格一致）
- `tests/caisen/test_zigzag_causal.py`：**未来函数回归测试**——构造已知序列断言末尾未确认 pivot 被丢弃；前视偏差反例断言识别结果不随未来数据变化
- `test_w_bottom.py` / `test_head_shoulder.py`：合成标准形态 + 假突破反例 + 跨度不足反例
- `test_plan_generator.py`：盈亏比边界（=3.0 通过, 2.99 丢弃）、等距测量
- `test_execution_engine.py`：状态机迁移、时间止损、移动止盈、断线跳过
- `test_risk_manager.py`：宏观系数三态、HV 过滤、仓位 5% 钳制、整手取整
- `test_backtest_replay.py`：回放无前视、gate 阈值判定
- `test_caisen_api.py`：API 契约 + Celery 降级
- 迁移回归：`test_macro_regime.py` / `test_sync_macro_credit.py` 改 import 路径后全绿

---

## 13. 依赖新增

`requirements.txt` 追加（Python 3.10 venv 兼容性需验证，锁版本）：
```
zigzag>=0.2.0
pandas-ta>=0.3.14b
trendln>=4.0.0
mplfinance>=0.12.10
lightweight-charts-python>=2.0
```
回退路径：zigzag/trendln 安装失败时，`zigzag_causal.py` 自写因果 ZigZag、`neckline.py` 纯 numpy polyfit，保底无黑盒。可删除 `hmmlearn`/`scikit-learn`（HMM 随 factors 删除）。

---

## 14. 风控拷问小结（Grill Me）

1. **流动性与极端行情**：回踩挂限价（非市价追涨）防滑点失控；`max_position_pct=5%` + 固定风险分配限单笔敞口；逼空行情 `pullback_window_bars` 超窗即失效，不追高踏空。
2. **接口与状态机边界**：复用 `OrderState` + `reconcile` + `check_order` 关1断线拒单；beat 遇 `is_locked` 跳过不补发；部分成交走状态机合法迁移。
3. **策略风险敞口**：纯多头（BUY only），无做空逼空风险；`macro_regime_veto` 收缩期 0 仓位；时间止损防资金被横盘标的占用；上线前必须过 `backtest_replay` gate。

---

## 15. 执行阶段需精确核定项（follow-up）

1. `server/services/portfolio_service.py` 逐函数切分：保留持仓/资产展示，删除 HMM 组合回测调仓。
2. 因子相关测试逐文件核实（`test_macro_api` / `test_akshare_north_dragon` / `test_sync_fundamentals` / `test_akshare_client` 等）：纯因子依赖则删，仅借数据源客户端则改路径保留。
3. `server/celery_app.py` 改造时保留 Celery 实例与 Redis 配置，仅替换任务内容。
4. `web/src/router/index.ts` 与各视图的 backtest/factors 引用逐个清理。
5. 删除前确认 `docs/` 下旧 spec/plan 不受代码删除影响（文档保留作历史）。
