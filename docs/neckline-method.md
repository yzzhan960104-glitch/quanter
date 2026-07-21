# 颈线法形态学策略 · 完整技术文档

> 维护范围：`scripts/neckline_method_v0.py` · `scripts/neckline_backtest.py` · `strategies/neckline_*` · `execution/backtest_replay.py` · `scripts/param_iter.py`
> 最后更新：2026-07-20

---

## 0. 一句话定位

颈线法是**拐点法范式的替代**：用「顶部高点聚集带」定位颈线阻力位 → 验证压制时长 → 等待带量突破 → **挂单等回踩进场**（不追涨）→ 分级止盈 + 颈线−ATR 止损。结构上盈亏比恒定 `rr = 2H/H = 2.0`，盈亏由形态结构决定、不依赖突破日涨幅。当前是仓库**唯一活跃策略**，基线凯利年化 ≈28.4%（全市场）/ 99.7%（创板科创 2025 至今口径，pos_cap=0.05）。

---

## 1. 总体架构

### 1.1 三层架构 + 单一真理源

```
┌─────────────────────────────────────────────────────────────────┐
│  核心算法层（single source of truth · 都在 scripts/）            │
│  ─────────────────────────────────────────────────────────────  │
│  neckline_method_v0.py                                          │
│    compute_atr / local_minima / local_maxima                    │
│    search_neckline      ← 颈线聚集带定位 + 压制验证              │
│    detect_neckline_method ← 识别层主流程（11 维参数）            │
│    DEFAULTS             ← 识别层参数字典                         │
│  neckline_backtest.py                                           │
│    simulate_exit        ← 执行层状态机（挂单回踩/撤单/分级止盈） │
│    dedup_signals        ← 信号冷却去重                           │
│    kelly_metrics        ← 凯利仓位 + 实盘年化（双封顶防爆）      │
│    scan_symbol          ← 单标的 滚动识别+去重+模拟 编排         │
│    EXEC_DEFAULTS        ← 执行层参数字典（含 trailing 3 维）     │
└─────────────────────────────────────────────────────────────────┘
              ↑ 直接 import                    ↑ 直接 import（适配器复用，零重写）
┌─────────────┴──────────────────┐   ┌─────────┴──────────────────┐
│  研究直调入口（scripts/）       │   │  策略适配层（strategies/）  │
│  ─────────────────────────────  │   │  ────────────────────────  │
│  param_iter.py   参数迭代引擎   │   │  base.py     Strategy 协议 │
│  neckline_backtest.main()       │   │  registry.py @register_strategy
│  kbkg_* / fullmarket（已清理）  │   │  neckline_method.py         │
│                                 │   │    NecklineMethodStrategy  │
│  特征：绕过 Strategy 接口，     │   │    .scan_at → 调 simulate_exit
│  直调 scan_symbol               │   │  neckline_schema.py         │
│                                 │   │    NecklineConfig (Pydantic)│
└─────────────────────────────────┘   └─────────────┬──────────────┘
                                                     │ 经 Strategy 接口
                                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  回测编排层（execution/）                                        │
│  ─────────────────────────────────────────────────────────────  │
│  backtest_replay.py  replay(price_data, strategy, start, end)   │
│    逐 symbol × T∈[start,end] 滚动 · df.loc[:T] 无前视            │
│    → 跨 symbol 聚合 → ReplayReport                               │
│  replay_worker.py    异步 worker（消费 task → 调 replay）        │
│  replay_tasks_db.py  任务持久化                                  │
│  replay_scheduler.py 并发调度                                    │
└─────────────────────────────────────────────────────────────────┘
                                                     ▲
┌─────────────────────────────────────────────────────────────────┐
│  服务编排（caisen/facade.py）                                    │
│    replay(req)        同步：_merge_cfg → backtest_replay.replay  │
│    replay_async(req)  异步：写 PENDING 行 → 返 task_id           │
│  前端 ParamLab（web/）：策略下拉 + 参数反射 + 抽屉式调参         │
└─────────────────────────────────────────────────────────────────┘
```

**关键设计**：`strategies/neckline_method.py` 是**薄适配器**，不重写算法——它的 `scan_at` 直接 `from neckline_backtest import simulate_exit`。两条入口（研究侧直调 / 编排侧经 Strategy 接口）**共享同一份 `simulate_exit`**，算法逻辑零分叉。

### 1.2 数据依赖（data_lake/）

| 数据集 | 用途 | 采集入口 |
|---|---|---|
| `a_shares_daily.parquet` | 个股 OHLCV（颈线法主输入，MultiIndex date×symbol） | `sync_all_tushare.py` / `sync_incremental.py` |
| `etf_daily.parquet` | ETF OHLCV（全市场外推实验用） | `sync_all_tushare.py` |
| `market_breadth.parquet` | 全市场宽度（站上 MA60 比例）· 四层动能②层 | `scripts/market_breadth.py` |
| `shibor.parquet` / `shibor_lpr.parquet` / `cn_m.parquet` | 利率三件套 · 四层动能②层流动性子模块 | `scripts/sync_rate_data.py` |

### 1.3 入口矩阵（谁在用哪条路）

| 场景 | 入口 | 走 Strategy 接口？ |
|---|---|---|
| 参数迭代（调参） | `scripts/param_iter.py` → `scan_symbol` | ❌ 直调 |
| 单标的诊断/复盘 | `scripts/neckline_backtest.main()` → `scan_symbol` | ❌ 直调 |
| 异步回测（前端 ParamLab） | `caisen/facade.replay_async` → `execution/backtest_replay.replay` → `scan_at` | ✅ 经接口 |
| 同步回测（API） | `caisen/facade.replay` → `execution/backtest_replay.replay` → `scan_at` | ✅ 经接口 |

> ⚠ 见 §6.2「双轨分叉风险」——研究侧与编排侧目前不走同一条路。

---

## 2. 算法细节

### 2.1 识别层 · `detect_neckline_method`（颈线法判定主流程）

输入：单标的 OHLCV `df` + 识别层参数 `cfg`（=DEFAULTS）+ 可选预算 ATR 序列。输出：候选 dict 或 None。

```
窗口截取 W = df.tail(window=60)                      ← 形态在近 60 日形成
   │
   ├─ ATR：窗口对齐（用 window 而非写死 14）· 预算复用避免每 T 重算
   │
1—┴─ 颈线搜索 search_neckline（两步，角色严格分离）
   │   ① 定位（颈线在哪）：窗口内【局部极大值=顶部高点】
   │      找「±ATR 带内含最多顶部高点的价位 c*」——"顶点连成颈线"
   │      · 时间衰减加权 exp(−Δt/τ)：近期顶部权重高（套牢盘还在），
   │        旧顶部淡出（套牢盘割肉=失效阻力）。τ=None 退化为等权。
   │      · 选位用加权 score，但要求【等权 touches ≥ min_touches】
   │        （防衰减后单个近期顶部独占颈线）
   │   ② 验证（确认有效）：压制时长 = P(close < c*) ≥ min_suppression
   │      · 旧版 bug：用压制时长最大化选位 → c 越高 close<c 越多 →
   │        选到窗口最高价，脱离真实阻力。压制只能验证、不能选位。
   │   → 返回 (c*, suppression) 或 (None, 0)
   │
2—┬─ 底部：min_price（窗口最低）+ local_minima 离散低点
   │   band = [min_price, min_price + ATR] 内的低点集合 ≥ min_bottoms（至少双底）
   │
3—┬─ 突破：close_T > c_star（收盘越过颈线）
   │        vol_T ≥ breakout_vol_mult × mean(vol[-5:])（带量）
   │
4—┴─ 交易要素 + 守卫
       entry = c_star（挂单价，不追涨）
       H     = c_star − min_price（形态高度）
       tp1   = c_star + 1·H        ← 第一波满足（50% 减仓位）
       tp2   = c_star + 2·H        ← 第二波满足（剩余 50%）
       rr    = 2H / H = 2.0（结构恒定）
       守卫1：H/ATR ≤ max_h_atr=4.0
              实证：浅形态胜率 51% vs 深形态 27%（深=暴跌反弹，规避）
       守卫2：rr ≥ min_rr（sanity，结构恒 2.0 故恒满足）
```

**返回的候选 dict**（供执行层消费）：`formed_at / neckline / suppression / bottom / n_bottoms / entry / stop / take_profit_1 / take_profit_2 / H / H_over_ATR / rr / atr`。

> 注：识别层算出的 `stop=min_price`（谷底）只用于「交易要素预告」，**执行层实际止损改为颈线−ATR**（见 §2.2）。

### 2.2 执行层 · `simulate_exit`（挂单回踩 + 分级止盈状态机）

输入：单标的全序列 `sym_df` + 信号下标 `signal_idx` + 颈线/谷底/ATR + 执行层参数 `exec` + 识别层参数 `id_cfg`。这是一个 ~130 行的完整状态机：

```
挂单价 buy_limit = c_star + buy_limit_atr_mult·ATR     ← 颈线上方挂买单等回踩
止损基准 base_stop = c_star − stop_atr_mult·ATR         ← 颈线下方 N×ATR
tp1 = c_star + tp1_h_mult·H    tp2 = c_star + tp_h_mult·H
撤单阈值 cancel_on = c_star + cancel_thresh_mult·H  (None=不撤单放飞所有信号)
   │
① 等回踩成交（signal_idx+1 .. +max_wait）逐日判：
   · 等待期 high ≥ cancel_on → 涨幅已兑现，回踩是退潮 → 撤单（skip_target_met）
   · low ≤ buy_limit          → 首个回踩日成交，break
   · 跑完 max_wait 未触       → skip_no_pullback（放弃）
   │
   成交价 entry = min(buy_limit, open[buy_idx])
     【v6 修复】限价买单：open>buy_limit（盘中回踩）→ 成交 buy_limit；
                          open≤buy_limit（跳空低开）→ 成交 open（更优）。
     旧版 entry=buy_limit 高估了跳空低开的买入价（早期悲观结论的元凶之一）。
   │
② 持有期逐根判 exit（buy_idx .. buy_idx+max_holding）：
   每日先算 trailing stop（海龟风格时间驱动）：
     holding_days ≤ grace      → stop = base_stop（宽限，给趋势确认空间）
     holding_days > grace      → eff_mult = stop_atr_mult − (holding_days−grace)·step
                                → 卡在 floor（grace=0/step=0 退化为固定止损）
   优先级判定（同日多重触发，从严到宽）：
     P1  low ≤ stop            → stop_loss（lot1/lot2 同价止损）
     P2  high ≥ tp2            → tp2（lot2 止盈，lot1 同日按 tp1 一并卖）
     P3  high ≥ tp1            → tp1（仅卖 lot1，lot2 续持）
     P4  is_last（超时）        → timeout（按收盘价平剩余）
   │
③ 汇总：avg_pnl = tp1_portion·lot1_pnl + (1−tp1_portion)·lot2_pnl
        exit_price = entry·(1+avg_pnl)（加权平均离场价）
   返回成交 dict（含 signal_date/buy_date/neckline/entry/risk_pct/
                  tp1/tp2/H_over_ATR/lot1_pnl/lot2_pnl/avg_pnl/exit_reason/
                  exit_date/exit_price/holding_bars）
```

`exit_reason` 枚举：`stop_loss / tp2 / timeout / skip_no_pullback / skip_target_met`（后两者不计入成交 hits）。

### 2.3 仓位与年化 · `kelly_metrics`（param_iter 目标函数）

```
凯利  f* = (b·p − q) / b        b=盈亏比=均盈/均亏, p=胜率, q=1−p
       f* ∈ [0, 0.5]            ← 约束
实盘仓位 pos = min(f*, pos_cap=0.05)        ← 第一道封顶：单笔仓位风控上限
资金曲线 curve = Π(1 + pos·r/100)
       按 year groupby.head(freq_cap=150)  ← 第二道封顶：每年最多复利 freq_cap 笔
       （模拟实盘最大持仓约束下"先到先得"；freq_cap≈ 同时持 6 只 × 持 10 天 × 年 250 日）
年化 ann = curve^(1/years) − 1
```

**为何双封顶（2026-07-20 连续两轮修复）**：旧版 `Π(1+f*·r/100)` 假设所有信号独立可同时下注，在近年高频短窗（创板科创 1250 笔/1.5 年、f*=0.2）下爆炸至 7257%~16495%；即便 pos=5%，高频（2905 笔）下仍爆炸至 784%。`pos_cap` 封单笔仓位、`freq_cap` 封年信号数，curve 才落到实盘可达区间（年化 60% = 每笔 5% 仓位平均贡献 +0.56%）。区分度保留：curve 由每笔 r 分布决定，非 f* 单值。

### 2.4 四层动能评分（第二方面 · 设计与数据就绪 · ⚠ 融合未接入核心算法）

> **当前状态（2026-07-20）**：四层动能是颈线法第二方面的**设计意图**，数据已部分落湖，
> 但经 2026-07-19 B 原型验证，②③④层作为"熊市过滤"**全部证伪**（见下）。核心算法链路
> （`detect_neckline_method`/`simulate_exit`/`scan_symbol`/`scan_at`）因此**主动不接入**
> 动能过滤——全仓 grep 证实 `strategies/` 与 `scripts/neckline*.py` 对 breadth/shibor/
> cn_m/lpr **零命中**，`market_breadth.parquet` 仅被生成脚本自身引用。即熊市软肋（§5.1）
> 是**完全裸跑**，且这是**证伪后的主动选择**，不是"还没来得及做"。

颈线法第一方面是 §2.1/2.2 的形态识别+执行；第二方面是**势能确认**（设计层面）——颈线突破在系统性动能萎缩时假突破概率高，**设计上**需四层共振过滤：

| 层 | 指标 | 数据 | 角色 | 数据状态 |
|---|---|---|---|---|
| ① 全球动能 | 风险偏好/外资流动 | 美债收益率 | 全球风向标 | ❌ 未采（探测 usb_yield 不可用） |
| ② A 股动能 | 流动性 + 宽度 | Shibor/LPR/M2 + market_breadth | 系统性动能温度计 | ✅ 已落湖 |
| ③ 板块动能 | 板块轮动 | sector_daily | 中观共振 | ✅ 已落湖 |
| ④ 微观动量 | 个股动量 | a_shares_daily | 标的级确认 | ✅ 已落湖（OHLCV 本就在主输入） |

**B 原型证伪（2026-07-19，②③④层全部负结果）**：
- **②宽度闸门对熊市反向**：宽度≥40% 过滤后 2018 −2.45→−2.94、2022 −2.21→−2.51（更差）——熊市反弹日（宽度≥40%）颈线信号是假突破陷阱，滤掉深度压缩日反留反弹顶。但宽度≥40% 对**非熊市大升有效**：2024 +1.95→+5.43%、2019 +1.72→+3.33%。**宽度是顺势指标，非避熊指标**。
- **②流动性**：能识别 2018 钱荒型（Shibor 高+M2 低），但 **2022 宽货币型失效**（Shibor 低+M2 高，流动性极宽松仍跌）。
- **③板块动量**：60 日行业均动量看似 28.4→61.6%，深挖证伪——孤立尖峰（邻居断崖）+逐年 5 正 4 负 +熊市更差，和②宽度同型失败。
- **④微观动量**：颈线信号事前特征（H/ATR/brk_vol/supp）各年无区分度（熊市牛市一样），区分熊市的只有事后 win%/stop%（不可事前用）。

**决定性结论**：2022 宽货币型（央行放水但风险偏好崩塌）是颈线法**事前不可过滤**的结构性盲区——顺势突破策略在风险偏好崩塌型熊市的内在局限，**非"层数不够"或"数据不全"**。这是核心链路主动不接入动能过滤的依据。

**保留的两条边际用途**（证伪后仍成立的顺势/降仓用法，**非熊市过滤**）：
- ②**宽度顺势加权**：宽度≥40% 时颈线信号加仓（非熊市年提升，2024 +5.43%）。
- ②**流动性降仓避 2018 钱荒型**：Shibor 历史高位+M2 下行时降仓（只抓 2018 类，漏 2022 类）。
- 2022 宽货币型接受软肋，靠**凯利仓位自适应**（整体近 N 笔胜率降时自动降仓，不依赖事前预测）。

详见 memory `neckline-momentum-scoring`（含五次订正链与"颈线×主力不共振"分界结论）。

---

## 3. 参数细节

### 3.1 识别层 11 维 · `DEFAULTS`（`neckline_method_v0.py:44`）

| # | 键 | 默认 | 候选档 | 物理意图 |
|---|---|---|---|---|
| ① | `window` | 60 | 40/60/80 | 颈线识别窗口（形态在近 N 日形成） |
| ② | `min_touches` | 2 | 2/3 | 颈线由 ≥N 个顶部高点聚集连成（定位用） |
| — | `min_suppression` | 0.6 | 0.5/0.6/0.7 | 压制时长下限：≥该比例 close 在颈线下方 |
| ③ | `local_extrema_window` | 3 | 3/5 | 局部极值左右窗（离散拐点提取） |
| — | `min_bottoms` | 2 | 2/3 | 至少双底（含窗口最低点） |
| — | `breakout_vol_mult` | 1.5 | 1.0/1.5/2.0 | 突破带量倍数（vs 近 5 日均量） |
| ⑥ | `min_rr` | 1.5 | 1.0/1.5/2.0 | 盈亏比下限（结构恒 2.0，作 sanity 守卫） |
| ⑦ | `max_h_atr` | 4.0 | 3.0/4.0/5.0 | 形态深度上限 H/ATR（**实证关键分水岭**：浅 51% vs 深 27%） |
| ⑧ | `stop_atr_mult` | 1.0 | 1.0/1.5 | 止损 ATR 倍数（止损=颈线−N×ATR） |
| ⑨ | `tp_h_mult` | 2.0 | 1.5/2.0/2.5 | 止盈 2 的 H 倍数（tp2=颈线+N·H） |
| ⑩ | `decay_tau` | None | None/30/60 | 颈线时间衰减（None=等权）。方案 A 实验净 −3.2 点，暂回 None |

### 3.2 执行层 7+3 维 · `EXEC_DEFAULTS`（`neckline_backtest.py:37`）

| # | 键 | 默认 | 候选档 | 物理意图 |
|---|---|---|---|---|
| ⑫ | `max_holding` | 15 | 10/15/20 | 成交后超时持仓日 |
| ⑬ | `max_wait` | 5 | 3/5/8 | 挂单等待回踩成交有效期（≈一周） |
| ⑭ | `cooldown` | 5 | 3/5/8 | 信号去重冷却（相邻信号合并为一次） |
| ⑮ | `buy_limit_atr_mult` | 1.0 | 0.5/1.0/1.5 | 挂单价 = 颈线 + N·ATR |
| ⑯ | `tp1_h_mult` | 1.0 | 0.5/1.0/1.5 | 止盈 1 = 颈线 + N·H（第一波减仓） |
| ⑰ | `tp1_portion` | 0.5 | 0.3/0.5/0.7 | 止盈 1 减仓比例（lot1 占比） |
| ⑱ | `cancel_thresh_mult` | 1.0 | None/1.0/2.0 | 撤单阈值 = 颈线+N·H（None=不撤单放飞） |
| — | `trailing_grace` | 0 | 5/10 | 宽限天数（前 b 天不收紧）· **待实施** |
| — | `trailing_step` | 0.0 | 0.05/0.1/0.15 | 收紧速度（ATR/日）· **待实施** |
| — | `trailing_floor` | 0.5 | — | 最低 ATR 倍数（收紧上限）· **待实施** |

> `trailing_*` 三维已写入 `EXEC_DEFAULTS` 且 `simulate_exit` 已实现分支逻辑（grace=0/step=0 退化为固定止损，兼容旧行为），但**参数迭代尚未纳入搜索**（PARAM_SPACE ⑱止），属「代码就绪、调参待跑」状态。

### 3.3 参数迭代 · `param_iter.py`（22 维概念空间 + 两阶段搜索）

- **universe（第 22 个概念参数，固定不调）**：创业板(300/301)+科创板(688/689)，2025-01-01 至今，近 30 日均成交额 ≥ 1 亿 → ≈1334 只可交易。
- **搜索空间**：§3.1/§3.2 的 21 维候选档（`PARAM_SPACE`，识别 11 + 执行 10 含 trailing 3），全笛卡尔积 ≈ 数万组，远超可跑量。
- **目标函数（v3 多目标 · 2026-07-21）**：`run_one` → `risk_metrics` 返 (ann, sharpe, max_dd) → `score_of` 约束式（`ann≥90%` 硬门槛，达标区最大化 `夏普/(1+回撤)`）。param_iter 按 score 排序/邻域贪心。可选 `--breadth-boost`（P1-c 宽度顺势加权：信号日 breadth≥0.4 → pnl×1.5）。
- **两阶段策略**：
  - 阶段 1 `random_params`：每维独立随机选一档，覆盖 18 维空间（`--n-random` 组）。
  - 阶段 2 `neighbor_params`：从当前最优组随机选 1~2 维移到相邻档（±1），2/3 概率改 1 维、1/3 改 2 维。
- **持久化续跑**：`logs/param_iter_state.json` 存 `{tried, best, best_ann, history}`，`params_key` 去重（None 可序列化），kill/重启自动接续。
- **目标**：`TARGET_ANN = 0.90`（用户 2026-07-21：≥90% 同时高夏普低回撤）。v2 旧基线 99.7%（ann 单目标，备份在 `logs/param_iter_state_v2_18d_backup.json`）。

---

## 4. 回测框架

### 4.1 `Strategy` 协议（`strategies/base.py`）

策略中性接口，回测引擎经此与具体策略通信。三个方法 + 一个契约：

| 方法 | 职责 |
|---|---|
| `precompute(symbol, full_df) → state` | 首个 T 前调一次，预算全序列指标（颈线法：全序列 ATR）。返回 strategy_state，scan_at 读它+写它（跨 T 状态如去重锚点） |
| `scan_at(symbol, df_T, T, state) → list[dict]` | T 日「识别+进场+出场」一站式闭环，返回 trade dict 列表（每个含 `TRADE_REQUIRED_KEYS`），未触发/未成交/被去重 → `[]` |
| `config_schema → type` | 策略参数 Pydantic 模型类（供 ParamLab 反射 + parse_review 字段护栏） |

**`TRADE_REQUIRED_KEYS`**（引擎统计层只读这些键，不感知策略种类）：
`symbol / signal_type / formed_at / entry_date / entry_price / exit_date / exit_price / exit_reason / rr / holding_bars`

**核心架构决策——出场逻辑归属策略侧**：颈线法 `simulate_exit` 是完整状态机（挂单回踩 + max_wait + cancel_on 撤单 + 分级止盈 + 超时）；若拆开迁就引擎的「T+1 回踩 + 单笔全平」模型必丢撤单/分级减仓语义 = 阉割颈线法。故引擎零感知策略内部，只接收标准 hit。

### 4.2 注册表机制（`strategies/registry.py`）

```
@register_strategy("neckline")     ← 装饰器自动入全局表 _STRATEGY_REGISTRY
class NecklineMethodStrategy: ...

build_strategy(name, cfg_override)  ← 按名字实例化
list_strategies()                   ← 供前端策略下拉
```

当前注册：`neckline`（颈线法，唯一活跃）+ `caisen`（蔡森形态，`CaisenPatternStrategy`，对照保留）。新增策略 = 实现接口 + 装饰注册 + 在 `strategies/__init__.py` import 触发。

### 4.3 无前视红线

- 引擎传给 `scan_at` 的 `df_T` **严格 = `df.loc[:T]`**；策略不得读 T 之后数据。
- `precompute` 预算的全序列指标，`scan_at` 内部必须用 `.iloc[:T_pos+1]` 截断后使用（颈线法 ATR、causal_pivots 均如此）。
- 进出场推进用的「未来 K 线」属回测允许（`simulate_exit` 从 T_pos 推进 max_holding 根）——这是「在 T 触发后模拟持有」，非前视。

### 4.4 异步回测链路

```
前端 ParamLab 提交
  → POST /replay/async（cfg_override + start/end + universe）
  → caisen/facade.replay_async(req)
       · _merge_cfg(cfg_override)         ← 增量覆盖默认（ValidationError→422）
       · replay_runs 写 PENDING 行 → 返 task_id（不阻塞）
  → ReplayScheduler 派发 → replay_worker 消费
       · backtest_replay.replay(price_data, strategy, start, end)
            逐 symbol×T 滚动 → ReplayReport(n_hits/win_rate/avg_rr/
                                 max_drawdown/pattern_dist/monthly_returns/
                                 avg_holding_bars/equity_curve/annualized_return)
       · abort_cb 中止 → ReplayAborted
  → 落 replay_tasks_db + replay_runs（前端历史列表）
```

**防御性**：参数/状态机异常（ValidationError/ValueError/KeyError）透传路由层转 422/404；算法/IO 异常降级返零统计报告（n_hits=0，杜绝 500 噪声）；price_data 装配为空同降级。

### 4.5 入口对比（研究直调 vs 编排）

| 维度 | `scan_symbol`（scripts 直调） | `replay` + `scan_at`（编排） |
|---|---|---|
| 去重 | `dedup_signals`（cooldown 交易日窗） | `_last_signal_pos`（per-symbol 跨 T 锚点） |
| 识别调用 | `detect_neckline_method` 直接 | `detect_neckline_method`（经适配器） |
| 出场 | `simulate_exit` | `simulate_exit`（同一份） |
| 仓位/年化 | `kelly_metrics`（param_iter 目标） | ReplayReport 的 equity_curve/annualized |
| 产出 | `logs/*.csv` + 控制台 | ReplayReport → DB → 前端 |

---

## 5. 已知边界与软肋

| # | 项 | 现状 | 风险 |
|---|---|---|---|
| 5.1 | **熊市软肋** | 2018(−2.45%)/2022(−2.21%) 亏；近年(2024-2026) +1.62% 稳健赚 | 四层动能已证伪（§2.4），熊市是结构性盲区、完全裸跑 |
| 5.2 | **双轨分叉** | `scan_symbol` 参数化 id_cfg（P1-b）+ `test_scan_symbol_matches_strategy` 守护一致 | 分叉风险已守护（去重/simulate 链路有测试覆盖） |
| 5.3 | ~~核心算法零单测~~ → **已补 24 用例** | 执行层 6 分支+trailing+kelly/risk 防爆+识别层 detect 成功/5 拒绝+scan 编排+双轨一致（P0 · test_neckline_core/recognition） | 回归网已建立 |
| 5.4 | **trailing 验证中** | 已纳入 PARAM_SPACE 搜索（P1-a，grace=0 基线对照）；v3 多目标 8h 搜索跑中 | top1 跨 2 月 −72% 回撤能否救回，待 v3 搜索结果 |
| 5.5 | **decay_tau 悬置** | 方案 A 实验净 −3.2 点，暂回 None | 颈线漂移问题真实但纯时间衰减非正解，量加权待探索 |

---

## 6. 项目级优化建议

> 以下按「风险×成本」分级。P0 是不做即裸奔的硬伤，P2 是工程整洁度。

### P0 · 给核心算法补回归单测（✅ 已完成 · 2026-07-20）

`simulate_exit` 是 ~130 行状态机，2026-07-20 刚连修两个 `kelly_metrics` 爆炸 bug，且 trailing 分支已埋但未验证。**无单测 = 任何后续改动裸奔**。已落 `tests/test_neckline_core.py`（10 用例全绿，1.32s），合成 OHLCV 构造确定性场景（基准 `c*=100/bottom=90/H=10/ATR=2` → `buy_limit=102/base_stop=98/tp1=110/tp2=120`）：

| 用例 | 构造 | 断言 |
|---|---|---|
| stop_loss | 挂单成交后 next bar low ≤ stop | `exit_reason=="stop_loss"`，lot_pnl=(stop−entry)/entry |
| tp1_then_tp2 | 触 tp1 后续触 tp2 | `exit_reason=="tp2"`，lot1=tp1 pnl、lot2=tp2 pnl、avg=加权 |
| timeout | max_holding 内无触发 | `exit_reason=="timeout"`，按收盘价 |
| skip_no_pullback | max_wait 内未回踩 | 返回 `skip_no_pullback`，entry=None |
| skip_target_met | 等待期 high≥cancel_on | 返回 `skip_target_met` |
| 跳空低开成交价 | open<buy_limit | `entry==open`（v6 修复点） |
| trailing 收紧 | grace 后持仓 | eff_mult 随 holding_days 递减、卡 floor |
| kelly 防爆 | 高频 + 高 f* 序列 | ann 落实盘区间（非 7257%） |

**识别层补充（`tests/test_neckline_recognition.py` · 12 用例全绿）**：基元（`local_minima`/`local_maxima`/`compute_atr`）+ `search_neckline`（聚集定位 + 压制验证）+ `detect_neckline_method` 成功路径 + **5 个拒绝边界**（未突破 / 未带量 / 深度过深 / 顶部不足 / 压制不足，每个守卫单独证伪）+ `scan_symbol` 编排（monkeypatch detect 隔离识别，专测去重+模拟+收集链路）。合成形态 `颈线=100/bottom=90/H=10/ATR≈3.6`，每根 K 线显式指定、守卫边界手算。至此 §6 P0 的「执行层 + 识别层」完整落地，颈线法 single source of truth 全链路有回归网兜底。

### P1 · 收敛双轨 + 消除绕过

1. **`diag_2026_cases.py:39` 自有 `replay` 函数** → 改调 `execution/backtest_replay.replay()`，收敛入口（前面已建议，待执行）。
2. **收敛双轨（✅ P1-b 部分完成 · 2026-07-21）**：采用「参数化 + 一致性测试」（比薄包装更轻量）——`scan_symbol` 加 `id_cfg` 参数消除硬编码 DEFAULTS 分叉，`test_scan_symbol_matches_strategy` 守护 `scan_symbol` 批量 == `scan_at` 逐 T 累积成交一致（signal_date/entry/exit_date/exit_reason 全对齐）。§5.2 分叉风险已守护。剩余边际项：`diag_2026_cases` 自有 replay 收敛、param_iter `run_one` 传 id_cfg（去全局 DEFAULTS）——非硬伤。
3. **trailing 纳入 param_iter 搜索（✅ 已完成 · 2026-07-21）**：`trailing_grace/step/floor` 已加入 `PARAM_SPACE`（21 维可调，候选 `grace∈{0,5,10}`/`step∈{0.05,0.1,0.15}`/`floor∈{0.0,0.5}`，`grace=0` 作固定止损基线）。端到端验证 trailing on/off 产生不同回测结果（链路通）。完整 8h 搜索留用户跑，回答 §5.4（top1 跨月 −72% 回撤能否被 trailing 救回）。

### P1 · scripts/ 治理

- **沉淀诊断脚本**：`diag_2024/diag_002882/diag_2026_*/analyze_fullscan/trend_filter_analysis/market_regime_filter/macro_regime_resonance/regime_micro_analysis/identify_param_scan/breakout_quality_analysis/bluechip_check/neckline_method_diagnose/calibrate_min_rr` 等十余个一次性诊断脚本，跑过即弃。建议统一挪 `scripts/oneoff/`（或直接清，git 可找回），与活跃工具（`param_iter/sync_*/market_breadth/neckline_backtest`）分离。
- **`probe_rate_fields.py` / `kbkg_trailing_verify.py`**：边界项，结论已沉淀者可删，待实施优化的先行验证脚本评估去留（见上一轮整理）。

### P1 · 策略增强（颈线法第二方面 · 证伪后的剩余方向）

四层动能作为"熊市过滤"已于 2026-07-19 B 原型**全部证伪**（§2.4），**不应再追求事前过滤熊市**——那是顺势突破策略的结构性盲区。剩余两条边际优化（均已验证有效方向，非投机）：
1. **②宽度顺势加权（仓位加权，非过滤）（✅ P1-c · 2026-07-21）**：`param_iter --breadth-boost` 已实现——加载 `market_breadth.parquet`（2367 日，≥0.4 占 52%），信号日 breadth≥0.4 → `avg_pnl×1.5`（等效加仓 1.5×）。端到端冒烟通过（5 只 ann 2.93→3.21%）。**待全市场搜索验证**：`param_iter --breadth-boost --time-budget 28800` 对比基线，看 2024 +1.95→+5.43% 能否复现。execution 层宽度加权为 follow-up。
2. **凯利仓位自适应**：整体近 N 笔胜率降时自动降仓（滚动凯利，不依赖事前预测）。这是应对 2022 宽货币型软肋的唯一可行路径——不预测、只反应（对应已清理的 `kbkg_top5_rolling_kelly` 实验方向，结论在 memory `neckline-momentum-scoring`）。
3. **前置依赖**：P0 核心算法单测——确保上述加权/自适应接入时不破坏 §2.1/§2.2 既有行为。

### P2 · 回测框架成熟度

1. **`Strategy` 协议契约测试**：`scan_at` 必返 `TRADE_REQUIRED_KEYS`、无前视（`df_T.equals(df.loc[:T])`）——这是框架「稳定可复用」的底线，目前 no covering tests。
2. **`ReplayReport` 回归**：`replay()` 函数本身 no covering tests（`ReplayReport`/`ReplayAborted` 有测，主函数没测）——补一个最小 price_data + DummyStrategy 的端到端。
3. **caisen 与 neckline 的 rr 口径统一**：颈线法 `avg_pnl/risk_pct`、caisen `(exit−entry)/(entry−stop)`，目前靠 `scan_at` 内分支兜底，建议在 `TRADE_REQUIRED_KEYS` 文档里写清「rr = 风险倍数」统一定义。

### P2 · 文档与知识体系

- `docs/superpowers/{plans,specs}/` 近 40 篇历史规划文档，`docs/caisen-methodology-summary.md` 已删——文档体系在重构中。建议本篇 `neckline-method.md` 作为「策略级技术文档」的新模板，后续 caisen/数据湖/执行层各起一篇，`docs/` 重建为「按子系统」的组织。
- 四层动能评分（§2.4）融合逻辑实现后，应在此文档补一节「§2.5 势能前置过滤」。

---

## 附录 · 关键文件速查

| 文件 | 角色 | 行数级 |
|---|---|---|
| `scripts/neckline_method_v0.py` | 识别层算法（detect/search_neckline/ATR/极值）+ DEFAULTS | ~320 |
| `scripts/neckline_backtest.py` | 执行层状态机（simulate_exit）+ 仓位（kelly_metrics）+ scan_symbol + EXEC_DEFAULTS | ~380 |
| `strategies/neckline_method.py` | Strategy 适配器（scan_at 复用 simulate_exit） | ~125 |
| `strategies/neckline_schema.py` | NecklineConfig（Pydantic 18 维） | ~45 |
| `strategies/base.py` | Strategy 协议 + TRADE_REQUIRED_KEYS | ~80 |
| `strategies/registry.py` | 策略注册表 | ~40 |
| `execution/backtest_replay.py` | 回测编排（逐 symbol×T 滚动 + ReplayReport） | — |
| `scripts/param_iter.py` | 参数迭代引擎（22 维概念·约束式 score 多目标·P1-c 宽度加权） | ~340 |
| `scripts/market_breadth.py` | 四层动能②层宽度指标生成 | ~80 |
| `scripts/sync_rate_data.py` | 利率三件套补采（shibor/lpr/cn_m） | ~100 |
