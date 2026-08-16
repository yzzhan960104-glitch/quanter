# 策略与参数现状全景（2026-08-16）

> 长周期策略及参数优化模式的**阶段 0 交付物**。数据源：experiment/experiments.db、logs/research_proposals.db、logs/discovery_trials.db 只读直查 + 代码实证（HEAD `6db624fa`）。
> 配套：循环日志 `docs/research/ROUND_LOG.md`；循环协议 `docs/superpowers/specs/2026-08-16-tuning-loop-protocol.md`。

## 一、策略与 21 维参数

仓库当前**唯一在册策略：颈线法（neckline）**（`strategies/registry.py` 注册，`strategies/neckline/strategy.py:60` 装饰点）。caisen 形态已退役删除。

参数 schema 三处对齐：`strategies/neckline/schema.py NecklineConfig` == `method_v0.DEFAULTS`（识别）== `backtest.EXEC_DEFAULTS`（执行/trailing）；价位五参另有 `price_levels.PRICE_LEVEL_DEFAULTS` 单源（commit `fbbeb82a` golden 钉死）。搜索离散档出自 `discovery/tools/param_iter.py:44-70`。

### 识别层（11 维）

| 参数 | 默认 | 值域 | 搜索档 | 语义 |
|---|---|---|---|---|
| window | 60 | 20-120 | [40,60,80] | 识别窗口（ATR 窗口同源对齐，非写死 14） |
| min_touches | 2 | ≥2 | [2,3] | 颈线聚集足够性（顶部数） |
| min_suppression | 0.6 | 0-1 | [0.5,0.6,0.7] | 压制时长（close<颈线比例下限） |
| local_extrema_window | 3 | ≥1 | [3,5] | 底部极值窗 |
| min_bottoms | 2 | ≥2 | [2,3] | 双底/三底门槛 |
| breakout_vol_mult | 1.5 | ≥1.0 | [1.0,1.5,2.0] | 突破带量倍数（vs 5 日均量） |
| min_rr | 1.5 | ≥0.5 | [1.0,1.5,2.0] | 盈亏比守卫（P4 已复活为活参数） |
| max_h_atr | 4.0 | ≥1.0 | [3.0,4.0,5.0] | 形态深度上限（H/ATR） |
| stop_atr_mult | 1.0 | ≥0 | [1.0,1.5] | 初始止损 = 颈线−N×ATR（trailing 基准） |
| tp_h_mult | 2.0 | ≥1.0 | [1.5,2.0,2.5] | 止盈2 = 颈线+N×H |
| decay_tau | None | Opt | [None,30,60] | 颈线时间衰减（None=等权；方案A 净-3.2点教训后回 None） |

### 执行层（7 维）

| 参数 | 默认 | 值域 | 搜索档 | 语义 |
|---|---|---|---|---|
| max_holding | 15 | ≥1 | [10,15,20] | 超时持仓日 |
| max_wait | 5 | ≥1 | [3,5,8] | 挂单等回踩有效期 |
| cooldown | 5 | ≥0 | [3,5,8] | 信号去重冷却（回测/实盘双消费） |
| buy_limit_atr_mult | 1.0 | — | [0.5,1.0,1.5] | 挂单价 = 颈线+N×ATR |
| tp1_h_mult | 1.0 | — | [0.5,1.0,1.5] | 止盈1 = 颈线+N×H |
| tp1_portion | 0.5 | 0-1 | [0.3,0.5,0.7] | 止盈1 减仓比例 |
| cancel_thresh_mult | 1.0 | Opt | [None,1.0,2.0] | 撤单阈值（None=放飞不撤） |

### trailing 层（3 维，海龟式时间驱动移动止损）

| 参数 | 默认 | 搜索档 | 语义 |
|---|---|---|---|
| trailing_grace | 0 | [0,5,10] | 宽限天数（grace=0∧step=0 退化为固定止损，normalize 互锁） |
| trailing_step | 0.0 | [0.05,0.1,0.15] | 收紧速度（ATR/日） |
| trailing_floor | 0.5 | [0.0,0.5] | 收紧下限 |

### 参数耦合（调一个必须想到另一个）

1. **trailing 互锁**：grace=0 → step/floor 强制归 0（`discovery/constraints.py` TRAILING_OFF）。
2. **止盈序/撤单序**：tp1_h_mult ≤ tp_h_mult；cancel_thresh_mult ≥ tp1_h_mult（None 放飞合法）。
3. **stop_atr_mult 双角色**：识别层 rr 分母 + 执行层 base_stop。
4. **window 三角色**：识别窗 + ATR 标尺 + 实盘完整性 gate 窗。
5. **regime 阈值绝不进搜索**（audit spec A1 红线，改值须 ADR）。

### 硬编码常量（不可调、不搜索）

TOPS_WINDOW=3、费率三件套（佣金万三/印花 0.05% 卖/过户 0.001%）、kelly 硬顶 0.5、pos_cap=0.05、freq_cap=150、max_positions=6、滑点 5bps（PositionModel 默认）、熔断 -3% 单日、universe=创板科创+近 30 日均额≥1 亿（第 22 个"概念参数"）、min_trades=30（年 calmar 记 0 线）。

## 二、实验台账（experiment/experiments.db 直查 2026-08-16）

| experiment_id | 状态 | weight | 创建 | 来源 | vs ACTIVE 差异 | note |
|---|---|---|---|---|---|---|
| **neckline_disc_20260725_25c602** | **ACTIVE** | 1.0 | 07-25 | discovery:77c5dc3f | 基线 | outer ann=18.4% calmar=7.24 max_dd=2.5% |
| neckline_disc_20260726_a1693e | DRAFT | 0 | 07-26 | discovery | 7 维（min_suppression 0.5、min_rr 1.0、cooldown 3、tp1_h 1.5、trailing 5/0.15/0.5） | outer ann=14.3% calmar=10.76 |
| neckline_prop_20260803_75fa90 | DRAFT | 0 | 08-03 | 提案 p_2675fa90 | 仅 stop_atr_mult 1.5 | — |
| neckline_prop_20260816_e5cb49 | DRAFT | 0 | 08-16 06:08 | 提案 | **15 维大改**（window 60、decay_tau 60、tp_h 1.5、trailing 10/0.15/0…） | — |
| neckline_prop_20260816_47d350 | DRAFT | 0 | 08-16 08:09 | daemon 自动 publish | 9 维（cancel→None 放飞、trailing 10/0.05/0.5、max_holding 10…） | outer ann=53.4% calmar=18.32 max_dd=2.9% |
| 提案 p_553e383d | PENDING | — | 08-06 | agent | min_rr 2.0→1.5、max_h_atr 5.0→2.5 | **从未 verify（悬置 10 天）** |

ACTIVE 实值（25c602）：window=80, min_touches=2, min_suppression=0.6, local_extrema_window=5, min_bottoms=3, breakout_vol_mult=1.0, min_rr=2.0, max_h_atr=5.0, stop_atr_mult=1.0, tp_h_mult=2.5, decay_tau=null, max_holding=20, max_wait=8, cooldown=8, buy_limit_atr_mult=0.5, tp1_h_mult=1.0, tp1_portion=0.3, cancel_thresh_mult=2.0, trailing=0/0/0。

实验治理链：discovery daemon 夜搜（02:00 schtasks）→ `auto_publish_champion` DRAFT →（本次战役新增：autopromote 七门 → 灰度 → ACTIVE）；CLI：`python -m experiment {create,promote,set-weight,archive,rollback,list}`。`_LEGAL_TRANSITIONS` 现仅 promote/archive/rollback 三迁移（DRAFT discard 路径本次战役 T2.2 补全）。

## 三、实盘 env 分叉清单（冠军档 vs 实盘生效）

实盘执行侧价位参数读 `.env`（`trading/critical.py::_trade_cfg`）而非 ACTIVE 实验 params——**已实质分叉 5 项**：

| 参数 | ACTIVE 档 | env 缺省（实际生效） | .env 显式 |
|---|---|---|---|
| tp_h_mult | 2.5 | 2.0 | 未设 |
| tp1_portion | 0.3 | 0.5 | 未设 |
| cancel_thresh_mult | 2.0 | 1.0 | 未设 |
| max_wait | 8 | 5 | 未设 |
| max_holding | 20 | 15 | 未设 |
| stop_atr_mult | 1.0 | 1.0 | 已设（对齐） |
| trailing 三件套 | 0/0/0 | 5/0.1/0.5 | 已设但**消费方已删（死配置）** |

其余死变量：`TRADE_MAX_TOTAL_EXPOSURE`、`QMT_ORDER_MAX_AMOUNT/MAX_SHARES`（闸已删）。`critical.py:198-200` 的 TODO 自认待收口。收效权威已就位（`PRICE_LEVEL_DEFAULTS` 单源）——收敛方案=实盘读 ACTIVE 实验 params，属后续工单。

## 四、基建地图

- **回测**：`backtest/replay.py`（ReplayReport：trades 逐笔/equity_curve/monthly_returns；无 calmar/夏普）+ `PositionModel`（capital 1e6、pos_cap 0.05、max_positions 6、slippage_bps 5）。全市场 3 个月 ≈5min。
- **discovery**：`evaluate`（kelly/calmar，P1 向量化后 ≈35.5s/组）/`evaluate_replay`（replay 口径，inner/outer）/`evaluate_wf`（四折）。CLI：oos/verify/run/champions/wf/report/publish/daemon。切分：holdout（2025/2026）、extended（2021-24/2025-26）、wf 四折（每折 universe 时点重建）。目标：inner `min_yearly_calmar`（A2 后）。
- **提案管线**：`research/proposals.py`——LLM（GlmClient）→ `_validate_params`（NecklineConfig 护栏）→ PENDING → `verify_proposal`（A 档：inner 改善任一[胜率+2pp/rr+0.05/年化+1pp] ∧ outer dd 劣化≤5pp ∧ outer ann≥-30%，MIN_HITS=30）→ APPROVED → publish DRAFT。钉钉人审环 + digest cron（工作日 18:30）。
- **训练环**：`backtest/optimize/training_loop.py` 状态机（RUNNING→ANALYZING→AWAITING_REVIEW→CONFIRMING），钉钉人审；分析输入仅 6 字段摘要（已知缺口）。
- **调度**：discovery daemon 02:00（`.env DISCOVERY_SCHEDULE=low-power`）；digest 18:30（mon-fri）；周度回测 weekly_replay（≥7 天间隔，冠军参数 90 天窗口）。
- **指纹**：engine_hash=`53090190e55f`（ENGINE_FILES 9 文件）、snapshot_hash（universe 冻结）、data_hash（除权重算防线）；discovery_trials.db 516 trial / 当前快照 3fdcbbcb 仅 3 trial（新起点）。
- **AI 回路已知缺口（10 条中关键 3 条）**：①分析输入无逐笔归因 ②verify 不自动触发 ③建议→提案断链——阶段 3 沉淀对象。

## 五、风险清单

| 风险 | 现状 | 处置 |
|---|---|---|
| **A3 成交真实性未验证** | 滑点 5bps 默认存在但无敏感性实证；跳空/跌停封死未建模（stop_gap/same_day_both 已计数可量化） | 本次 T1.1 |
| **A4 Kelly 薄边缘** | audit 标定"4% Kelly 薄边缘"；DG-G5 定稿分数 Kelly 0.25× 未落地（当前固定 pos_cap=0.05） | 本次 T1.2/T1.3 |
| 实盘 env 分叉 | 5 项价位参数与 ACTIVE 档不一致（上表） | 登记，后续收口工单 |
| DRAFT 无处置路径 | 状态机缺 DRAFT→ARCHIVED；08-04 曾手工 SQL discard | 本次 T2.2 |
| auto_publish UNIQUE 冲突 | 08-16 06:08 崩溃一次（`discovery_bridge.py:151`） | 本次 C4 |
| universe 陈旧标的 | 4 只尾部 K 线早于湖最新日 >14 天（freeze 警告） | 本次 C5 |
| 提案悬置 | p_553e383d PENDING 10 天未 verify | 本次 C3 |
| 多口径不一致 | discovery（kelly/calmar）≠ ReplayReport ≠ 实盘；verify 已用 evaluate_replay 对齐方向 | 循环协议内声明口径 |

## 六、循环入口

- 每轮记录：`docs/research/ROUND_LOG.md`（假设→命令+指纹→数字→决策→下一步）。
- 循环协议：`docs/superpowers/specs/2026-08-16-tuning-loop-protocol.md`（双层闸 + autopromote 七门）。
- ADR-14（分数 Kelly）/ ADR-15（autopromote 门槛）：`docs/architecture/14-*/15-*`。
