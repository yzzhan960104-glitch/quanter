# 回测与策略深度 Review + 长周期 Agent 自主优化研究报告

> 2026-08-03 · 基于当前 HEAD（00169cc8）代码、data_lake、experiment.db、discovery_trials.db、
> 运行日志与实盘流水的一手证据。结论：**代码工程质量和回测/实盘单源化非常好，但回测保真度
> 存在 3 个 P0 级乐观偏差，且当前两套参数优化循环（legacy param_iter + discovery daemon）
> 在并行跑、冠军口径已经分叉**——这恰好是"长周期 Agent 自主优化"必须先解决的问题，
> 否则 Agent 会在错误的收益数字上自我强化。

---

## 0. 结论摘要（TL;DR）

1. **回测引擎架构是健康的**：识别（`detect_signal`）与离场（`decide_exit`）回测/实盘单源、
   严格无前视（`df.loc[:T]`）、数据完整性 gate 回测/实盘共用、快照冻结 + engine_hash 可比性
   三件套、199 个相关测试全绿。这些是长周期自主优化的稀缺地基，绝大部分团队没有。
2. **回测可信度有 3 个 P0 乐观偏差**（必须先修）：
   - 止损/止盈成交按目标价"完美成交"，不建模跳空低开、**跌停无法卖出**（创板/科创板 ±20%，
     跌停风险真实存在）；
   - 主回测链路零滑点（`PositionModel.slippage_bps=0.0`），`MockBroker` 有滑点/部分成交模型
     但未接入主链路，且部分成交后无补全逻辑；
   - 同一根日 K 内"先摸 cancel_on 还是先回踩成交"假设为 cancel 优先，方向性偏差未量化。
3. **指标口径存在系统性不一致**：`max_drawdown` 基于全部信号 rr 曲线，净值/年化基于
   `PositionModel` 过滤后的曲线；discovery 搜索目标（kelly/calmar 复利近似）≠ 主回测报告口径
   ≠ 实盘下单口径。**Agent 优化什么，就应该用什么口径**。
4. **实证发现（今晨日志）**：
   - legacy `param_iter`（317 组/夜）与 `discovery daemon`（19 trial/夜）仍在**双轨并行**，
     冠军参数已分叉：param_iter best（`stop_atr_mult=1.5, tp_h_mult=1.5, trailing_step=0.15`）
     vs 实盘 ACTIVE（`stop_atr_mult=1.0, tp_h_mult=2.5, trailing_step=0.0`）；
   - 周度自动回测（`weekly_replay.py`）读的是 `logs/param_iter_state.json`（legacy 冠军），
     而实盘用 `experiment.db` 的 ACTIVE → **播报/周度回测与实盘参数不一致，不可比**；
   - discovery daemon 最新一夜：`rho=0.000`（覆盖度判据实际不可达）、inner 冠军 calmar=112、
     **outer 去偏只有 ann=1.9% / max_dd=7.8%** → 当前搜索在 inner 上严重过拟合，OOS 边缘极薄；
   - legacy param_iter 报出"年化 115.8%、回撤 0.9%、夏普 11.7"这类数字，是 kelly/freq_cap
     近似口径的人为放大，不是可实现收益。
5. **长周期 Agent 自主优化方案**：三环架构（每日观察环 → 每周研究环 → 灰度发布环），
   原则是"**自动研究、人工放行、fail-closed**"，全部复用现有 discovery/experiment/training-loop/
   钉钉/APScheduler 资产，分 Phase A-E 落地，详见 §5。

---

## 1. 回测引擎 Review

### 1.1 架构优点（维持现状，勿破坏）

| 优点 | 位置 | 说明 |
|---|---|---|
| 识别单源 | `strategies/neckline/method_v0.py:302 detect_signal` | 回测 `scan_at` / `scan_symbol` / 实盘 `scan_live` 三入口同源 |
| 离场单源 | `strategies/neckline/execution.py:131 decide_exit` | 回测 `simulate_exit` 与实盘 `stop_loss_monitor` 共用纯函数 |
| 无前视纪律 | `backtest/replay.py:190-193` | 每 T 传 `df.loc[:T]`，ATR 预算截断等价有测试守护 |
| 数据完整性 gate | `backtest/replay.py:92 _apply_continuity_filter` | 与实盘 `_eod` 共用 `filter_universe_by_continuity`（300214 漏采教训） |
| 可比性三件套 | `compute_unit/hashes.py` | git_commit + engine_hash + parquet_sha256 跨机一致性 |
| 资金模型单源 | `backtest/models.py PositionModel` | pos_cap 口径对齐实盘 `capital × pos_cap × weight` |
| 任务生命周期 | `backtest/scheduler.py` | 断点续跑、heartbeat 超时、取消、重启恢复完整 |
| 测试 | `tests/backtest` + neckline/discovery 相关 | 本次实测 199 passed / 31s |

### 1.2 P0：回测可信度问题（Agent 自主优化前必须修）

#### P0-1 止损按目标价完美成交，未建模跳空与跌停

- 位置：`strategies/neckline/backtest.py` 持有期循环，STOP_LOSS 分支
  `lot1_pnl = lot2_pnl = (stop - entry) / entry`——**假设止损单在 stop 价精确成交**。
- 现实：若当日 `open < stop`（跳空低开），市价/限价止损单实际成交价≈open（更差）；
  若触及**跌停**（创板/科创 ±20%，且低开直接封死），**根本卖不出去**，持仓顺延，
  回测却已按 stop 价离场。A 股跌停流动性问题在小市值/极端行情下是真实的尾部风险。
- 同样，TIMEOUT 平仓假设收盘可成交，未建模跌停封死。
- 影响：回测系统性**高估止损保护能力**。自主优化会倾向选择"止损更紧"的参数档
  （回测里止损紧=亏损小），实盘却可能被跌停卡住，方向性过拟合。

#### P0-2 主回测链路零滑点、零部分成交；MockBroker 未接入且部分成交无补全

- 位置：`backtest/models.py` `PositionModel.slippage_bps=0.0`；`backtest/replay.py:206`
  `position_model = PositionModel()` 默认全链路零滑点；`backtest/mock_broker.py` 的
  slippage/partial_fill 逻辑只被测试引用，主回测走 `simulate_exit` 直接成交。
- 位置：`backtest/mock_broker.py:execute_order` 部分成交后订单停在 PARTIAL_FILLED，
  **剩余部分没有自动补成交机制**（注释声称"由后续 execute_order 完成"，但没有任何调用方
  会持续喂盘口数据补单），账户持仓与下单量会不一致。MockBroker 目前实质是死代码。
- 建议：
  a) `simulate_exit` 增加 gap 成交模型：止损触发日 `open < stop` → 按 `min(stop, open)` 成交；
     TP 触发日 `open > tp` → 按 `max(tp, open)` 成交（保守/乐观各给一档或取 open）；
  b) 至少把 `PositionModel.slippage_bps` 默认改为保守值（如 5-10bps 双边），并在 replay
     报告中显式标注所用滑点；
  c) 跌停/涨停不可成交建模（需涨跌停价数据，Tushare 有 limit_list_d；当前 lake 无此表，
     可先用 ±20%/±10% 近似或按前收推算），并统计"假成交次数"进报告 metadata。

#### P0-3 日 K 内事件顺序假设（同日 cancel vs fill）未量化

- 位置：`simulate_exit` 等待期循环，先判 `high >= cancel_on` 撤单、再判 `low <= buy_limit`
  成交。同一根 K 线两者都满足时，回测一律记 `skip_target_met`（假设"先摸高后回踩"）。
- 实证（本报告复核脚本）：`low=9 <= buy_limit=10.5 且 high=15 >= cancel_on=14` 的 K 线
  → 回测结果 `skip_target_met`，不成交。
- 若真实顺序是"先回踩成交、后摸高"，订单已成交，回测漏记交易（保守）；若真实顺序相反，
  回测正确。方向性偏差真实存在但未量化。
- 建议：在回测 metadata 记录 `n_same_day_cancel_vs_fill`，并在报告中给出敏感性（按成交优先
  重算一版对比）；长期可用分钟线/盘口数据校准。

### 1.3 P1：指标与资金口径不一致（直接影响"优化目标"）

#### P1-1 max_drawdown 与净值曲线不是同一条曲线

- 位置：`backtest/replay.py:_compute_stats` 的 `max_drawdown` 基于**全部信号**的累计 rr
  曲线（含被资金约束跳过的笔）；`equity_curve`/`annualized_return` 基于
  `PositionModel` 过滤后的组合曲线（并发上限 6、现金不足跳过）。
- 影响：同一报告里"回撤"和"年化"不是同一组合口径，Agent/人审无法正确做风险收益判断。
- 建议：`max_drawdown` 改为从 `equity_curve` 计算；同时保留 signal-level 口径字段
  （如 `max_dd_signal`），双口径显式化。

#### P1-2 discovery 搜索目标 ≠ 主回测口径 ≠ 实盘口径

- 位置：`discovery/objective.py:metrics_of` 用 `risk_metrics`（kelly 仓位 + freq_cap=150
  按年截断 + 复利 + calmar）；主回测/前端报告用 `PositionModel`（pos_cap 加总不复利）；
  实盘下单用 `build_orders_from_signals`（`capital × pos_cap × weight`，整手取整）。
- 影响：冠军按 kelly/calmar 排序，展示/实盘按 pos_cap 口径——**排名用的数字不是展示的
  数字，展示的数字也不是实盘会拿到的数字**。`evaluate_replay`（P0-2）已提供 replay 口径，
  但搜索主排序仍走旧口径。
- 建议：把搜索目标切换为 `evaluate_replay`（或 PositionModel 口径），旧 kelly/calmar 只做
  报告旁证；至少让 `RunSummary` 同时输出两口径 calmar，供对比。

#### P1-3 快照指纹不含数据内容，长周期可比性有洞

- 位置：`discovery/snapshot.py:snapshot_hash` 只含 `universe_count + date_range + lake_start
  + universe_def`；`compute_unit` 的 `parquet_sha256` 只在跨机任务校验用，**不参与 discovery
  trial 可比性**。
- 现实风险：`data/tools/sync_daily_incremental.py:156` 已自承认"除权标的历史 qfq 基准未重算"；
  一旦未来重算，同一 `snapshot_hash` 下历史价格变化 → 新旧 trial 共用同一指纹却不可比。
- 建议：freeze 时对 universe 的价格数据算内容指纹（如逐 symbol 价格序列 sha256 或全湖
  parquet hash + 行数 + 更新时间），并入 snapshot_hash 或至少写入 snapshot 表；trial_id
  派生加入数据指纹，保证"数据一动，旧 trial 自动不可比"。

#### P1-4 replay 单 T 异常静默吞掉

- 位置：`backtest/replay.py:195-199` 单 T 异常 `logger.debug` 后 continue。
- 影响：某类 symbol/T 系统性报错时，回测照常出报告只是信号变少，**Agent 无从感知**。
- 建议：异常计数进 metadata（`n_exceptions`、top exception 类型），超过阈值在报告中标记
  `degraded=True`；自主优化管道把 degraded 报告视为无效。

#### P1-5 回测策略实例"一次一跑"契约脆弱

- 位置：`backtest/replay.py:159-162` 文档明确警告实例带跨 T 状态（cooldown 锚点），复用
  会静默污染结果。worker/compute_unit 已遵循，但这是约定而非机制。
- 建议：`replay()` 入口检测 `strategy._last_signal_pos` 非空即拒绝（fail-fast），或提供
  `reset()`；Agent 自动跑大量实验时最易踩。

### 1.4 P2：细节问题（可排期修复）

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| P2-1 | 平台极值重复计数 | `method_v0.py:local_maxima/local_minima` | 用 `>=` 双端比较，`[4,4,4]` 平台计 3 个局部极大值；实证确认。会虚增"touches/双底"聚集度。建议相邻等值去重或改严格不等 |
| P2-2 | `local_extrema_window` 是半死参数 | `method_v0.py:112,222` | 底部用该参数，顶部 `search_neckline` 写死 `top_window=3`；21 维搜索里这个参数只影响一半语义 |
| P2-3 | min_rr 过滤是"几何盈亏比" | `method_v0.py` R3 口径 | rr=(tp2-entry)/(entry-stop) 只衡量最优情形；未计入 tp1 部分止盈/超时/止损概率。建议研究 EV 口径或经验 rr 分位过滤 |
| P2-4 | 周度回测参数源陈旧 | `backtest/weekly_replay.py:31` | 读 legacy `logs/param_iter_state.json`，与实盘 ACTIVE（experiment.db）分叉（§4 实证） |
| P2-5 | 回测成本无 min5 | `backtest.py` 费率口径 | 大资金可忽略；小资金账户实盘 min5 占比显著，PositionModel 应支持 min_commission 近似 |
| P2-6 | `scan_symbol` vol5 口径与 detect 不一致 | `backtest.py:scan_symbol` | 报告字段 vol5 用前 5 日（不含 T），detect 用含 T 的 tail(5)；仅影响 CSV 归因分析 |
| P2-7 | 同 symbol 并发持仓未约束 | `backtest/models.py:build_equity_curve` | 实盘一 symbol 一持仓；模型允许同 symbol 两笔重叠占用资金。cooldown 降低概率但 max_wait+max_holding 组合下可能发生 |
| P2-8 | `REPLAY_LAKE_MIN_DATE` 3 年截断 | `backtest/worker.py:50` | 默认按今天-3 年截断湖；老窗口回测需显式 env，且"今天"依赖导致跨日可复现性弱。建议任务表记录 lake 指纹 |
| P2-9 | 周度回测窗口硬编码 90 天 | `weekly_replay.py` | 与发现引擎 2025/2026 holdout 无衔接，长期 Agent 需要滚动窗口统一 |

---

## 2. 策略代码 Review

### 2.1 识别层（method_v0.py）

- 整体评价：显式、参数化、防御到位（ATR≤0、H≤0、数据不足、risk_dist≤0 均显式返 None）。
- 值得注意的设计：
  - 颈线定位用"顶部聚集"而非"压制最大化"，压制只做验证——正确，避免选到窗口最高价；
  - R3 实际口径 rr（tp2-entry)/(entry-stop) 与执行层 base_stop 同源；
  - cancel_on close 守卫是 execute 层 high 守卫的保守近似，识别期挡"冲天突破"合理。
- 风险点：
  - P2-1 平台极值；P2-2 半死参数；
  - `search_neckline` 是 O(tops²)（tops≈10，成本可接受，无需过早优化）；
  - 所有过滤器（breakout_vol/min_suppression/max_h_atr/min_rr）都是启发式，无统计显著性
    检验；`backtest/tools/` 下散落的归因脚本（breakout_quality/regime/trend_filter）没有
    进主链路——这正是 Agent 研究环应系统化的第一桶金。

### 2.2 执行层（execution.py / backtest.py）

- `decide_exit` 优先级链（止损 > tp2 > tp1 > 超时）清晰，state/bar/cfg 三分离；
- trailing 的 `grace and step` 双条件使 grace=0 或 step=0 都退化为固定止损，当前 ACTIVE
  参数（0/0/0）即固定止损，逻辑自洽；
- 事件顺序假设见 P0-3；止损/超时成交假设见 P0-1；
- TP2 同日 lot1 按 tp1 价卖（若 lot1 仍开）隐含"同日先摸 tp1 再摸 tp2"的顺序假设，
  与 P0-3 同类，建议归入同一次校准。

### 2.3 组合/资金

- `PositionModel.pos_cap` 加总不复利与实盘 budget 语义一致，方向正确；
- 缺口：整手取整损失未建模（实盘 `build_orders` 取整到 100 股）；同日多信号竞争
  （先到先得）未建模；experiment weight 灰度未建模。长周期 Agent 若做"收益归因到实验版本"，
  这些缺口会放大。

---

## 3. 实证发现（2026-08-03 一手证据）

### 3.1 两套优化循环双轨并行，冠军已分叉

| 循环 | 入口 | 昨夜运行 | 当前冠军关键参数 | 去向 |
|---|---|---|---|---|
| legacy param_iter | `discovery/tools/param_iter.py`（夜间日志 09:02 收尾，317 组） | ✅ 仍在跑 | `stop_atr_mult=1.5, tp_h_mult=1.5, trailing_step=0.15, min_suppression=0.5` | 写 `logs/param_iter_state.json` |
| discovery daemon | uvicorn lifespan 02:00 cron | ✅ 在跑（19 trial） | 见下 | `discovery_trials.db` → publish DRAFT |
| 实盘 ACTIVE | `experiment.db` | — | `stop_atr_mult=1.0, tp_h_mult=2.5, min_suppression=0.6, trailing_step=0.0`（25c602，07-27 激活） | 实盘 `_eod` 使用 |

- `weekly_replay.py`（周度自动回测）读 param_iter_state.json → **播报的回测参数与实盘 ACTIVE
  不一致**，回测-实盘归因链条断裂。
- 建议：停用或归档 legacy param_iter（317 组/夜 ≈ 10 小时算力），统一走 discovery daemon；
  周度回测改读 `experiment.resolve_active()`。

### 3.2 discovery 覆盖度与 OOS 现实

- 昨夜 daemon 汇总：`run_id=e0ae3442_4f2b0d10 n_new=19 frontier=10 top_calmar=112.10 rho=0.000
  k=0/3 冠军 outer: ann=1.9% calmar=0.24 max_dd=7.8%`。
- `rho=0.000`：21 维离散网格单元数 = Π len(cands) 巨大，`grid_coverage` 判据④实际不可达；
  收敛只能靠预算耗尽——"覆盖度防伪收敛"的机制在当前参数空间下是空转。
- inner calmar 112 vs outer ann 1.9%：**严重过拟合信号**。discovery 在 inner 上选出高
  calmar 组合，但 outer（未参与排序的真实 OOS）几乎没有超额收益。
- 结论：长周期自主优化的第一优先不是"多跑搜索"，而是**换更诚实的目标与验证结构**
  （walk-forward / 多段 OOS / 参数扰动稳健性 / DSR 门槛），否则 Agent 会在过拟合上越走越远。

### 3.3 实盘流水质量

- `logs/live_trades.csv` 出现同笔 `600000.SH BUY 100@10.5` 多次重复 fill 回报（08-02/08-03
  多条），且有主板 600000.SH 成交——**实盘 universe 是创板科创，600000.SH 不应出现在
  策略交易池**；fill 幂等/归因需要核实（state_store 有幂等，CSV 为审计旁路，但仍值得查
  一次回放路径）。
- 09:22 预挂单因 `max_amount: 单笔金额超上限` 与 `connection: 网关未连接` BLOCKED 后，
  09:47 又重提成功——pre_open 与手工补单混流，事件链上"计划确认→挂单"有非幂等窗口。
- 对 Agent 观察环的影响：**live 收益统计必须先做 fill 去重与策略归因清洗**，否则漂移检测
  输入就是脏的。

---

## 4. 长周期 Agent 自主优化研究

### 4.1 现状资产盘点（已具备，不必重造）

| 资产 | 位置 | 能力 |
|---|---|---|
| 参数发现引擎 L0-L5 | `discovery/` | 快照冻结、holdout、Sobol/TPE、Pareto、DSR、跨夜收敛、publish DRAFT |
| 人审训练 loop | `backtest/optimize/training_loop.py` | 钉钉自然语言审核 → GLM 解析 → 回显确认 → 多轮调参状态机 |
| 实验版本中心 | `experiment/` | DRAFT/ACTIVE/ARCHIVED + weight + 审计 + promote/rollback 护栏 |
| 钉钉交互 | `broadcast/connect_manager.py` + 5 bot | 主动推送 + 对话路由；`infra/tools/dingtalk_review_bridge.py` 审核桥 |
| 调度与一致性 | APScheduler + `trading/job_ledger.py` + `trading/catchup.py` | 生产机不 7x24 的最终一致性、补跑、台账 |
| 跨机可复现 | `compute_unit/hashes.py` | git/engine/parquet 三 hash |
| 风控 | `trading/compute/breaker.py`、影子期闸 | 日内熔断、`TRADE_SHADOW_MIN_DAYS` |
| 归因分析脚本 | `backtest/tools/*` | breakout_quality、regime、trend_filter、macro_resonance 等散装工具 |
| Agent 执行技能 | `.agents/skills/`（loop-me/ask-matt/code-review/verification 等） | 长循环、拷问、验收前的既有范式 |

### 4.2 差距分析（为什么现在还不能"长周期自主优化收益"）

1. **搜索空间封顶在 21 个参数档**；结构性改进（新过滤器、市场状态过滤、出场结构、数据
   特征）必须人工写代码 → 需要一个能"改代码但被护栏锁住"的研究 Agent。
2. **目标函数与实盘口径不一致**（P1-1/1-2）→ Agent 优化的是近似收益。
3. **回测保真度 P0 问题** → Agent 会过拟合乐观偏差（尤其止损/跌停）。
4. **无漂移检测**：live 表现 vs 回测期望没有自动对比/告警/回滚。
5. **无滚动重训**：inner/outer 固定 2025/2026；长周期需要 walk-forward 与数据版本治理。
6. **训练 loop 的 LLM 只能调参**：不能提"加 regime filter 试试"并自动验证；分析输入无
   trades 明细（只有 6 字段摘要），诊断能力弱；且 AWAITING_REVIEW 无超时（spec §9 写了
   24h 超时但实现没有）。
7. **发布后无自动监控降级**：promote 后人审一次，之后靠人工盯。

### 4.3 目标架构：三环自主优化（自动研究 · 人工放行 · fail-closed）

```
┌─────────────────────────── 环 1：观察（每日 18:30 盘后） ───────────────────────────┐
│ pipeline done 事件 → 组装研究摘要：                                                   │
│   · live 滚动表现（state_store fill 去重归因后） vs 回测期望分布（均值±σ 分桶）       │
│   · 信号频率/胜率/avg_rr/exit_reason 分布漂移                                        │
│   · 数据完整性/新鲜度/复权事件（除权标的清单）                                        │
│   · 市场 regime 指标（复用 backtest/tools 的 regime 逻辑，常驻化）                    │
│ 输出：research_digest（钉钉推送 + docs/research_digest.md）                           │
│ 漂移超阈值 → CRITICAL 告警 + 冻结自主发布（fail-closed）                              │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                   ↓ 每周 / 漂移事件
┌─────────────────────────── 环 2：研究（Agent 提案 + 自动验证） ──────────────────────┐
│ Agent 输入：research_digest + 实验历史（trial/提案成败库）+ trades 归因               │
│ Agent 输出：结构化提案 JSON（假设/变更类型/涉及文件/预期/风险）                       │
│  变更类型分三档：                                                                    │
│    A 参数档（零代码）：直接进 discovery 搜索/单点回测                                 │
│    B 过滤器/特征开关（feature flag）：代码变更但走受控开关，默认关                     │
│    C 结构进化（出场/识别逻辑）：git 分支 + 测试 + 黄金回归 + 双口径回测                │
│ 自动验证管道（全自动，无 Agent 参与决策）：                                          │
│   快照冻结 → inner/outer/walk-forward → DSR/Pareto/参数扰动稳健性 → 报告              │
│ 通过门槛 → research_proposal 落库（含 git/engine/parquet 三 hash）→ 候选池 DRAFT      │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                   ↓ 钉钉人审（approve/reject/modify）
┌─────────────────────────── 环 3：发布（灰度 + 监控 + 回滚） ─────────────────────────┐
│ 批准 → experiment DRAFT → 影子期（≥5 天，既有闸）→ 小额灰度（weight<1）               │
│ → 自动监控：live vs 影子期/历史期望 逐笔归因 → 达标逐步提权 → 未达标自动降权/回滚     │
│ → 月度 walk-forward 重训：rolling snapshot（数据 hash 进指纹）重新跑 discovery        │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 交互模型（你 ↔ Agent 的长周期协作）

- **推送节奏**：每日盘后研究摘要（钉钉）；每周候选提案列表；发布/回滚/漂移事件即推。
- **审批语言**：复用 training loop 的"自然语言审核 + GLM 解析 + 回显确认"模式，动作扩展为
  `approve / reject / modify / defer / stop`；提案用稳定 ID 索引，长周期可追溯。
- **状态持久化**：Agent 状态全部落 DB（仿 `training_loops` + `discovery_trials`），
  重启/断线可恢复（复用 job_ledger + catchup 模式），Agent 不是"活在上下文里的会话"，
  而是"活在数据库里的研究工作流"。
- **双 Agent 载体可选**：
  a) **寄生 uvicorn 的 research_agent 服务**（推荐，零新守护进程，与现有架构一致）：
     状态机 + GLM/钉钉，负责提案生成与审批对话；代码变更由它调用受控执行器
     （git 分支 + pytest + 回测）；
  b) **Codex 桌面 recurring automation**：注册每日/每周研究任务，由 Codex 直接做
     review/提案/执行，仓库内留 `docs/research_digest.md` 与提案 DB 作为记忆锚点。
     适合"Agent 重活、人在环外"的场景；两者可以并存（研究环用 b，发布环用 a）。

### 4.5 护栏清单（fail-closed，缺一不可）

1. **代码变更必过测试**：任何 B/C 类提案必须附带测试且全绿，黄金回归（现有 golden gate）
   零漂移；回测内核文件变更必须重算 engine_hash 并作废旧 trial。
2. **数据版本进指纹**：snapshot_hash 加入数据内容 hash（P1-3），数据一动旧实验自动作废。
3. **Agent 永不直接 promote**：只能产 DRAFT；promote 必须人审 + 影子期 + 灰度 weight。
4. **预算护栏**：每夜算力预算（已有 `estimate_budget`）、每周期 LLM 调用/轮次上限
   （已有 max_rounds）、提案数上限。
5. **熔断与自动回滚**：沿用 breaker；新增"live 20 日滚动指标低于回测期望 2σ 或回撤超阈值
   → 自动降权/回滚 + 钉钉 CRITICAL"。
6. **审计**：所有 Agent 动作写 audit（experiment 已有；新增 research_proposal 表 +
   action log）。
7. **一键停用**：`RESEARCH_AGENT_ENABLED=false` 或钉钉「停」即冻结全部自主动作
   （沿用 training loop stop 模式）。
8. **LLM 降级**：GLM 不可用时研究环退化为"只读摘要"，不产出提案（fail-closed）。

### 4.6 阶段化路线图

| 阶段 | 内容 | 周期 | 验收 |
|---|---|---|---|
| **Phase A 地基** | 修 P0-1/2/3（跳空/跌停/滑点/事件顺序量化）；统一指标口径（max_dd 从 equity_curve；discovery 搜索切 replay 口径）；snapshot 数据 hash；replay 异常 telemetry + degraded 标记 | 1-2 周 | 回测对拍实盘 30 笔内误差可解释；golden 更新 |
| **Phase B 观察环** | 停用 legacy param_iter；weekly_replay 改读 experiment.resolve_active；live fill 去重归因清洗；漂移检测（期望分布 vs live 滚动）；research_digest 生成器 + 钉钉推送 | 1 周 | 每日盘后收到可行动摘要；漂移告警有误报率记录 |
| **Phase C 提案工作流** | research_proposal 表 + 提案 schema + Agent 提案 prompt；A 档（参数）全自动验证管道；钉钉审批（approve/reject/modify）| 2-3 周 | 首条"A 档提案从提出到 DRAFT"全自动闭环 |
| **Phase D 发布环** | 灰度 weight 自动监控 + 自动降级/回滚；B 档 feature flag 基础设施（过滤器开关化，默认关）| 1-2 周 | 一次灰度从 0.1→1.0→回滚全流程有日志可审计 |
| **Phase E 长周期自治** | walk-forward 滚动重训；C 档受控结构进化（Agent 写代码 + 测试 + 双口径验证）；提案成败学习库（喂 Agent 避免重复错误）| 持续 | 月度重训自动触发；结构进化提案平均通过率记录 |

### 4.7 下一步建议（本周即可启动）

1. 先把 `param_iter` 停掉/归档，weekly_replay 与播报改指 experiment 单一真相源
   （消除 §3.1 分叉，纯收益）。
2. 立 Phase A 任务：跳空/跌停成交模型 + 滑点默认值 + max_dd 口径统一 + snapshot 数据 hash。
3. 写一页 `research_digest` 生成器设计（复用 brief_strategy + state_store + replay report），
   作为 Agent 观察环的第一个可运行原型。

---

## 5. 附录

### 5.1 复核脚本与结果

- 平台极值重复计数：`local_maxima([1,2,3,4,4,4,3,2,1], 2) → [4.0, 4.0, 4.0]`（3 个极大值）。
- 同日 cancel/fill 竞争：`low=9<=buy_limit 且 high=15>=cancel_on` → `skip_target_met`。
- 测试健康度：`pytest tests/backtest tests/test_neckline_core.py
  tests/test_neckline_recognition.py tests/test_decide_exit.py tests/test_detect_signal.py
  tests/discovery -m "not slow"` → **199 passed, 10 deselected（31s）**。

### 5.2 关键文件索引

- 回测引擎：`backtest/replay.py` / `backtest/models.py` / `backtest/worker.py` /
  `backtest/scheduler.py` / `backtest/mock_broker.py`
- 策略：`strategies/neckline/method_v0.py` / `backtest.py` / `execution.py` / `strategy.py`
- 发现引擎：`discovery/{snapshot,split,objective,sampler,search,runner,daemon,publish}.py`
- 训练 loop：`backtest/optimize/training_loop.py` / `training_analyzer.py`
- 实验中心：`experiment/store.py` / `models.py`
- 设计文档：`docs/superpowers/specs/2026-07-14-caisen-ai-training-loop-design.md`（人审闭环，
  Spec 3 vs Spec 4 的定位已在本报告 §4 延续）

---

## 6. 实施记录（2026-08-03 下午 · ①②③ 已按序落地）

### ① 双轨治理（已实施）

- `discovery/tools/param_iter.py`：入口 fail-closed——不带 `--legacy` 一律拒绝运行并
  提示改用 `python -m discovery daemon`（PARAM_SPACE 仍被 discovery.sampler 复用）。
- `backtest/weekly_replay.py`：周度回测参数源改为 `experiment.resolver.resolve_active()`
  （实盘同源）；无 ACTIVE 才回退 legacy state 文件。
- `broadcast/brief_strategy.py` + `broadcast/__main__.py`：策略播报改读 experiment.db
  ACTIVE（版本号 + outer 去偏年化），legacy param_iter_state.json 降级为告警回退。
- 测试：`tests/discovery/test_param_iter_retired.py`、`tests/backtest/test_weekly_replay.py`
  （+3）、`tests/broadcast/test_broadcast_snapshot_experiment.py`（+2）。

### ② Phase A 回测保真与口径（已实施）

| 项 | 改动 | 测试 |
|---|---|---|
| P0-1 跳空止损 | `simulate_exit` 止损成交价 = min(stop, open)；返回 `stop_gap` 标记 | `tests/test_neckline_gap_fill.py`（+4） |
| P0-2 滑点 | `PositionModel.slippage_bps` 默认 0→5（双边共 10bps），metadata 可见 | `test_models.py` / `test_replay_stats.py`（+2） |
| P0-3 事件顺序量化 | 同日 cancel/fill 标记 `same_day_both`；策略级 `skip_stats` 聚合进 replay metadata | `tests/test_neckline_skip_stats.py`（+4） |
| P1-1 口径统一 | `max_drawdown` 改从 equity 曲线计算（净值百分比），旧 rr 口径保留为 `max_dd_signal`；播报回撤改百分比渲染 | `test_replay_stats.py` / `test_brief_strategy.py` |
| P1-2 discovery 双口径 | `RunSummary.top_replay_metrics`（冠军 replay 引擎复评），daemon 生产默认开 | `test_runner.py`（+2）/ `test_daemon.py`（+1） |
| P1-3 数据指纹 | `SnapshotMeta.data_hash`（close 序列 sha256 聚合）+ snapshot 表列迁移 + daemon `data_changed` 显式标注 | `test_snapshot.py`（+3）/ `test_store.py` / `test_daemon.py`（+2） |
| P1-4 异常 telemetry | replay 单 T 异常计数 → `n_exceptions`/`degraded` 进 metadata | `test_replay_stats.py`（+2） |
| P1-5 实例 fail-fast | replay 拒绝带 cooldown 状态的复用实例；**顺带修复 evaluate_replay inner/outer 复用同一实例的真实污染 bug** | `test_replay_stats.py`（+1） |

### ③ research_digest 原型（已实施）

- 新包 `research/`：`digest.py` 提供漂移摘要纯函数 + live fill 去重清洗 +
  回测期望 loader + `main()` 落盘入口（`docs/research_digest.md`）。
- 已用真实数据跑通：实盘 7 笔 fill（去重后）、期望取最近 SUCCESS 回测
  （1314 笔 / 胜率 31.1% / 均 rr −0.31）、ACTIVE 实验溯源。
- 诚实性：live 的 win_rate/avg_rr 待 state_store 平仓归因接入（Phase B TODO），
  缺失时渲染「—」且不做漂移判定。

### ④ 周期钉钉同步 + 真实引擎启动（2026-08-03 晚间追加）

- `research/digest.py`：新增 `load_live_perf_from_state_store`（TP_FILLED realized_pnl
  金额统计，CLOSED 无 pnl 时诚实降级）与 `push_digest`（build_default_manager 装配
  通道 + asyncio.run 同步等待，**子进程必须 load_dotenv 否则通道 0/0**——本次实测
  修复后 1/1 通道投递成功）。
- `presentation/server/main.py`：注册 `research_digest_push` cron（默认每交易日
  18:30，`RESEARCH_DIGEST_CRON` 可覆盖），DETACHED 子进程跑 `research.digest --push`，
  与 discovery cron 同范式。
- 真实启动验证：重启引擎（新代码，mode=live，QMT 网关已连接），日志确认
  "research digest cron 30 18 * * 1-5 已注册到 engine.sched"；手动推送研究摘要
  到钉钉成功（1/1 通道）；提交全市场真实回测任务验证新回测链路
  （task 7146fdce…，窗口 2026-07-01~08-03）。

### ⑤ APScheduler 工作日语义 bug 修复（2026-08-03 22:0x 追加）

- 实证：APScheduler 3.x `day_of_week` **0=周一**（非标准 cron 0=周日），
  `"1-5"` 实际匹配周二~周六 → 周一 18:00 pipeline 从不触发（当日断链实证）。
- 修复：engine 三个盘后 cron 默认值 + `.env` 覆盖值 + digest cron 全部改 `mon-fri`；
  新增 `tests/test_workday_cron.py` 钉死语义；C-8 启动补跑已补齐当晚 pipeline。

### ⑥ Phase C 研究提案工作流 + 24h 低功率 discovery（2026-08-03 深夜追加）

**Phase C（Agent 提案 → 自动验证 → 钉钉审批 → DRAFT 发布）：**
- `research/proposals.py`：提案表 + 状态机（PENDING→VERIFYING→APPROVED/REJECTED→
  PUBLISHED）+ A 档自动验证（基线 ACTIVE vs 提案 evaluate_replay，门槛：inner 改善 +
  outer 不显著劣化）+ LLM 提案生成（NecklineConfig 值域护栏）+ 钉钉审核解析
  （"通过/否决 p_xxxxxxxx"）+ publish 到 experiment DRAFT（promote 仍留人审）。
- API：`presentation/server/api/v1/research.py`（list/generate/review/verify/publish），
  main.py 注册；钉钉 bridge 按文本含提案 id 自动路由到 research/review。
- digest cron 子进程加 `--proposals`：每日 18:30 推送摘要时自动生成 0-2 条提案。
- 真实验证：LLM 生成 `p_2675fa90`（基于 digest 负均 rr 提出放宽 tp_h_mult/收紧
  stop_atr_mult）→ 钉钉审核通过 → publish 建 experiment DRAFT
  `neckline_prop_20260803_75fa90`（v3，weight=0）。

**24h 低功率 discovery：**
- `DISCOVERY_SCHEDULE=low-power`（.env 已启用）：每小时+5 分触发，窗口跳过
  9-16 点（盘中）与 18 点（pipeline/digest），每轮 1 组/单进程/K=24（≈1 天不扩张
  才收敛）；cli daemon 新增 `--budget-groups` 直给组数；低功率子进程关闭冠军
  replay 复评（--no-eval-replay-top）并把 TPE 降为 0（默认 10 个 TPE 会拖到
  ~80min/轮，实测后修正）。
- 真实验证：`python -m discovery daemon --budget-groups 1 --n-proc 1 --k-rounds 24`
  两轮实测（verify/verify2）：1 组/轮 + 单进程 + K=24 + **trial 幂等去重
  （n_new=0 不重跑）+ data_changed 显式重置 k**；冠军 outer 去偏 ann=16.9%
  calmar=4.36 max_dd=3.9%（新数据版本 0e8b5c51）。

### ⚠️ 当前阻塞：QMT 客户端登录

多次强杀引擎后 QMT 客户端（XtMiniQmt）会话残留，重启客户端后停在登录界面
（窗口标题无账户号）→ 引擎 connect -1、TradingEngine 未装配（API 正常、discovery/
digest cron 未注册）。**需在 QMT 客户端手动登录账户 10110356**，然后重启引擎
（`schtasks /run /tn QuanterServer`）即恢复完整装配。

### ⑦ 最终装配完成 + 两个新 bug 修复（2026-08-03 23:0x 收尾）

- 环境澄清：QMT 客户端为**模拟客户端**（D:\东北证券NET专业版(测试版)\bin.x64，
  账户 10110356，用户已登录）。
- **bug：`upsert_account` 用 INSERT OR REPLACE**——REPLACE 先 DELETE 旧行，被子表
  `ON DELETE RESTRICT` 挡住 → `FOREIGN KEY constraint failed` → QMT 已连接但
  TradingEngine 装配失败。修复为 SQLite UPSERT（ON CONFLICT DO UPDATE），
  新增 `tests/trading/test_state_store_account_upsert.py` 钉死。
- **最终装配验证**（23:04）：QMT 网关已连接 → TradingEngine 已装配并启动 →
  **discovery cron 每小时+5 分（低功率）已注册** → digest cron mon-fri 已注册 →
  C-8 补跑正常；23:05:00 低功率 cron 自动触发并拉起 daemon 子进程（下次 00:05）。

### ⑧ discovery 进展可见 + 实验平台自动打通（2026-08-03 23:3x 追加）

- `research/discovery_bridge.py`：
  - `load_discovery_status()`：trial 总数/最新 run（n_trials/frontier/k/daemon_run_count）/
    新冠军（params + inner/outer metrics）→ digest 与 API 共用；
  - `auto_publish_champion()`：新冠军 **outer ann 优于当前 ACTIVE** 才自动 publish
    experiment DRAFT（weight=0，promote 留人审）；不优于 → 跳过（防垃圾候选刷屏）。
- 进展获取途径（5 种）：CLI（report/champions）、discovery_trials.db 直查、
  钉钉 daemon 告警、每日 digest「参数探索」段、`GET /api/v1/research/discovery/status`。
- daemon 侧 `auto_publish_fn` 注入（cli 装配，discovery 包零 research 依赖）。
- 真实验证：status API 返回 311 trial/最新 run/新冠军 a1b12e33df50
  （inner calmar 14.58、outer ann 10.2%）；digest 含探索段并推送钉钉 1/1；
  自动 publish 护栏正确（新冠军 outer 10.2% < ACTIVE 18.4% → 不建 DRAFT）。

### 验证

- 全量回归：`pytest tests -m "not slow and not e2e_long"` → **1384 passed,
  11 deselected**（含新增 ~20 个测试）。
- 已知后续（未在本轮实施）：跌停无法成交的顺延建模、walk-forward 滚动重训、
  discovery 搜索目标彻底切换 replay 口径（当前为双口径报告）、research 观察环
  接入 APScheduler 每日盘后任务与钉钉推送。
