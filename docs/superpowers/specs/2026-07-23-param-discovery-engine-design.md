# 参数发现引擎（Parameter Discovery Engine）· 设计文档

> **维护范围**：升级 `discovery/tools/param_iter.py`（离线随机+贪心搜索脚本）→ 长期自治参数发现系统；复用 `backtest/worker.py`（ProcessPool）+ `backtest/tasks_db.py`（SQLite WAL）；打通 `experiment/` 闭环（冠军参数 → 影子验证 → 上线）。**新增 `discovery/` 包，回测内核（`strategies/neckline/`）零改动**。
> **创建日期**：2026-07-23
> **状态**：v1.3 · 设计稿（v1.2 评价体系 +探查实证订正：2026 OOS 普遍强、证伪过拟合假设、风险转移至 regime 依赖/评价失真/快照漂移，见 §1.4）
> **前置依赖**：颈线法策略已就绪（`strategies/neckline/`，识别内核 `method_v0.py` + 回测内核 `backtest.py`）；`experiment/` 已 100% 实现并接入二期引擎（`docs/superpowers/specs/2026-07-22-experiment-system-design.md`）。

---

## 0. 一句话定位

**参数发现引擎是把"零散手跑 param_iter"升级成"可信（样本外）+ 可复现（快照指纹）+ 高吞吐（并发）+ 自收敛（Pareto 前沿 + 收敛判据）+ 自闭环（→experiment 影子验证）"的长期参数发现系统**。**第一命门是先把样本外可信度立起来**——当前 `logs/param_iter_state.json` 的 `best_ann=115.8%`（v3，168 组）与文档固化的 99.7%（v2，179 组）**全样本内搜索、零样本外验证**，高度疑似多重比较过拟合；在可信度闭环建立前，任何"长期跑、堆算力"都只是在更快地拟合噪声。**第二命门是搜索有效性**——光有可信的目标函数还不够，还必须论证"长期跑"真能覆盖 21 维参数空间并收敛到相对最优；否则收敛判据会在采样盲区上**伪收敛**（早早停在一片从未采到的优质区域之外，一脸正经地交付"已收敛"），这是比过拟合更隐蔽的陷阱。

> **🌐 2026-07-24 探查实证（§1.4 详）**：原"过拟合、OOS 必塌"的核心赌注**已证伪**——颈线法 top-5 冠军在 2026 近期 OOS 普遍 ann 145-182%（高于 2025 样本内），未塌反最强。颈线法有真实 alpha，**真风险转移至 regime 依赖（熊市未测）/ 评价失真 / 数据快照漂移**，而非样本外过拟合。

---

## 1. 背景与动机

### 1.1 触发场景

需求：**做一个长期的参数优化任务，持续探索颈线法策略的参数空间，发掘出"相对最优"的参数**。这把 `discovery/tools/param_iter.py`（一次性离线脚本，8h 时间预算跑完即止）升级成一个**可持续运行、自收敛、结果可直接流转上线**的发现系统。

### 1.2 当前痛点（基于真实代码定位的 4 个缺口）

经三份代码盘点交叉确认（`param_iter` / `experiment` / 颈线参数全景），当前地基有 4 个硬伤：

| 缺口 | 现状（代码定位） | 后果 |
|---|---|---|
| **① 无样本外验证** | `param_iter.py:67` `START_DATE="2025-01-01"` 全量进 `run_one`；`grep walk-forward\|holdout\|OOS` 全仓零命中 | 冠军参数无法证明非过拟合；`best_ann=115.8%` 教科书级多重比较过拟合（21 维 × 随机+贪心挑最高 × 软约束 `score=ann×sharpe/(1+max_dd)`） |
| **② 数据快照不冻结** | `param_iter_state.json` schema（`load_state:188-200`）无 `timestamp/数据版本/md5/引擎 hash/seed` | 数据湖增量落湖天天变，168 组历史试验全在不同基准上比，**不可复现、不可比、不可审计** |
| **③ 串行 + 无约束裁剪** | 单进程 while 循环（`main:241-254`），单组 ~185s，8h 仅 ~170 组；21 维笛卡尔积里大量物理无意义组合（trailing 互锁、`min_rr` 死参数、`tp1>tp_h` 退化、`cancel<tp1` 冲突）在白跑 | 算力浪费在废组合；随机+贪心无代理模型，21 维空间效率极低（`grep optuna\|skopt\|hyperopt` 零命中） |
| **④ 闭环断裂** | `param_iter` / `training_loop` **都不调** `experiment`（grep `experiment\|resolve_active\|promote` 在 `backtest/optimize/` 与 `param_iter.py` 零命中） | 冠军参数靠人手敲 `python -m experiment create/promote` CLI 上线，无自动流转、无影子验证衔接 |
| **⑤ 搜索有效性未论证**（v1.1 补） | `search.py` 调度逻辑空心（`random→greedy→tpe` 一行带过，§7.2 把 TPE 当可选）；收敛判据（§3.5）只判"前沿不扩张"、无参数空间**覆盖度**度量；§12 全是工程验收，缺"发现引擎冠军 vs 手跑 168 组冠军"的基线对照 | 长期守护在采样盲区上**伪收敛**后自停，交付一个"采样撞到的孤峰"冒充相对最优；无法证明这套基建比手跑多发现了任何东西，工程自嗨 |

### 1.3 职责边界（做 / 不做）

| 做 | 不做 |
|---|---|
| 样本外目标函数（walk-forward）+ 数据快照指纹（可复现） | 不改回测内核 `strategies/neckline/`（识别 + 出场逻辑零改动，复用 `scan_symbol`/`simulate_exit`） |
| 约束裁剪搜索空间 + 可选贝叶斯 TPE（在 OOS 目标上） | 不重造回测引擎、不引入重型黑盒量化库（vectorbt/backtrader/qlib，反魔法红线） |
| 多目标 Pareto 前沿 + 收敛判据（长期自停） | 不做盘中出场、不下单、不动 `experiment/` 状态机（只 `create` 候选版本，promote 归人/归影子验证） |
| ProcessPool 并发 + SQLite 落库（复用既有基建） | 不实时算 PnL 看板、不自动 promote（MVP 仅产出"候选参数 + OOS 报告"，promote 仍需人工确认闸） |
| 冠军参数 → `experiment create --source "discovery:run_xxx"` 自动建候选 | 不替代 `backtest/optimize/training_loop.py`（人审训练 loop 保留，二者分轨：发现系统做广度自动搜索，training loop 做深度人审微调） |

---

### 1.4 L1 Go/No-Go 探查实证（2026-07-24，`discovery/tools/probe_champion_oos.py`）

在铺 discovery 系统前，先跑 L1 验收第 2 条最小版：当前 param_iter 冠军 **top-5** 的 2026 近期 OOS 去偏（方法：全历史跑 `scan_symbol` + 按 `signal_date` 分段，`full` 段作复现锚）。**结果证伪了 §0 / ADR1 的悲观预期**：

| rank | 全段 ann（复现锚） | 2025（样本内） | **★2026（OOS代理）** | 2026夏普 | 2026回撤 | 2026笔数 |
|---|---|---|---|---|---|---|
| top1 | 109.8% | 70.8% | **182.1%** | 14.04 | 1.5% | 1082 |
| top2 | 103.0% | 73.3% | **149.8%** | 14.02 | 1.2% | 950 |
| top3/4 | 117.0% | 91.4% | **145.5%** | 10.69 | 3.0% | 858 |
| top5 | 111.9% | 75.0% | **169.7%** | 12.04 | 1.7% | 1044 |

- **普遍强，非孤峰**：top-5 在 2026 **全部** ann 145-182%，全部远高于 2025（70-91%）与全段。颈线法在创板科创 2025-2026 有真实 alpha，2026 反而最强——**不是多重比较过拟合噪声**。
- **绝对值不可当实盘预期**：夏普 10-14 / ann 145-182% 是 `risk_metrics` 的 `freq_cap=150` 复利 + `夏普=per-trade×√年交易数` 放大产物（实证 §3.5 分层裁判重写动机）。同口径相对比较有效。
- **漂移实证 L0 必要性**：`full` 段 ann 无法精确复现 history 记录（漂移 1.8-7.2%）；两次间隔数分钟的跑批 universe 从 1334→1332 只、top1 full ann 从 115.8%→109.8%——**连"复现自己的冠军"都做不到**，正是 §1.2 缺口②"数据快照不冻结"的活证据。L0 快照冻结（§6.1）必须前置，否则任何历史 trial 不可比。
- **诚实边界**：2026 非纯 OOS（冠军用 2025+2026 全段 score 选出，2026 参与了选择）；2026 仅半年，ann 估计方差大；top3=top4 是 `history` 重复记录瑕疵（`tried` 去重但 `history` append 漏去重），不影响结论。
- **Go/No-Go 结论：强 Go**。颈线法值得继续铺。**真风险三转移**：(a) **regime 依赖**——2025-2026 无大熊市，软肋（2022 宽货币型）未测，须扩熊市段数据做 regime 覆盖；(b) **评价失真**——单一 score/Pareto 在放大口径排序（§3.5 分层裁判校准）；(c) **快照漂移**——L0 前置。discovery 优先级据此重排（§2.1）。

---

## 2. 目标与非目标

### 2.1 阶段化目标（顺序锁定，不可跳——见 ADR1）

分 5 层，**每层独立可验收、可回滚**。先可信、再放量、后自治：

- **L1 可信度闭环（最高优先，阻塞一切）**：数据快照冻结 + **嵌套验证**样本外目标函数（inner walk-forward 选参，outer holdout + embargo 纯评估冠军，防 test 段信息泄露——见 §3.3）。**用当前 168 组冠军补跑嵌套 OOS + 熊市 regime 覆盖**（§1.4 已证近期 OOS 未塌、真风险在 regime 依赖/快照漂移）。此层做完前不谈长期搜索。
- **★ 发现验收闸（L1 → L2 硬关卡，v1.1 补）**：在铺任何长期搜索基建前，先用最小可跑配置证明"搜索真能发现更优参数"——**(a) 基线对照**：发现引擎小规模跑后的嵌套 OOS 冠军，须显著优于当前 168 组手跑冠军的嵌套 OOS，或至少覆盖更稳健的参数区域；**(b) 参数邻域稳定性**：冠军在 21 维邻域 ±扰动下嵌套 OOS 不塌（高原而非孤峰）；**(c) 算力账可行**（§3.6）。三条任一不过 → 颈线法在该口径本就过拟合或算力不成立，停止铺 L2-L5（见 ADR1 决策风险）。
- **L2 吞吐层**：`param_iter_state.json` → SQLite（复用 `tasks_db.py` WAL）+ ProcessPool 并发（复用 `worker.py`，initializer 一次加载 data_lake 复用）。schema 补 `created_at / data_snapshot_hash / engine_hash / seed / oos_metrics`。
- **L3 搜索层**：先**约束裁剪**砍掉 6 处耦合的废组合；再上贝叶斯 TPE（可选，`optuna`，**仅在 OOS 目标确立后引入**）。
- **L4 自治层**：多目标 Pareto 前沿（ann / max_dd / sharpe / 交易笔数）+ 收敛判据（连续 K 轮前沿不扩张 / 后验预期提升 < ε / 预算耗尽）+ schtasks 夜间守护 + 断点续跑。
- **L5 闭环层**：冠军参数自动 `experiment create`（DRAFT，source 标记）→ 影子验证（`AUTO_TRADE_MODE=dry_run` ≥5 天）→ 人工 promote。**填 `param-lab-design.md:35` 留下的 Spec4 空位**。

### 2.2 非目标（MVP = L1+L2 外，follow-up）

- 贝叶斯 TPE / 进化算法（L3，先做约束裁剪）
- 多口径绩效切片（"五口径分层"在仓库不存在，实际仅全市场 / 创板科创 2025 至今两个口径；多口径属 follow-up）
- 自动 promote / 自动 archive（MVP 仅产候选 + 报告，promote 经人工 + 影子验证）
- 四层动能评分接入（2026-07-19 B 原型全部证伪，核心链路裸跑，非本引擎职责）
- 盘中分级止盈、实时 PnL 看板（归二期引擎 / experiment `report`）

---

## 3. 核心概念与数据模型

### 3.1 五层抽象（实施顺序即依赖顺序）

```
┌─ L5 闭环层    冠军 → experiment create(DRAFT, source=discovery:run_xxx) → 影子验证≥5天 → 人审promote
├─ L4 自治层    Pareto前沿 + 收敛判据 + schtasks夜跑守护 + 断点续跑
├─ L3 搜索层    约束裁剪(去6处耦合废组合) → 贝叶斯TPE(可选, 仅OOS目标上)
├─ L2 吞吐层    ProcessPool(复用worker.py) + SQLite落库(复用tasks_db.py, 补指纹/种子/OOS)
├─ L1 可信度层  ★阻塞一切★ 嵌套验证(inner选参+outer纯评估) + 数据快照指纹
└─ L0 数据层    冻结的 universe+行情 快照(sha256锁版本)
```

### 3.2 Trial 数据模型（单次参数试验，替代当前 `state.json` 的 `tried` 项）

| 字段 | 类型 | 说明 |
|---|---|---|
| `trial_id` | TEXT PK | `sha256(params+snapshot+seed)[:12]`，天然去重键 |
| `params` | TEXT(JSON) | 完整 21 维参数（识别 11 + 执行 7 + trailing 3） |
| `data_snapshot_hash` | TEXT | **L0 快照指纹**（universe + 行情 parquet 的 sha256，冻结版本） |
| `engine_hash` | TEXT | 回测内核代码 hash（`backtest.py`+`method_v0.py` 的 git blob），改内核后老 trial 标记 stale |
| `score_fn_version` | INT | 目标函数版本（score 公式改了，老 trial 不参与排序） |
| `seed` | INT | 随机种子（回测若有随机成分，可复现） |
| `split` | TEXT | 切分标识，如 inner 折 `wf_2022_test` / outer `holdout_2026` |
| `in_sample` | TEXT(JSON) | 样本内聚合（train 段，仅诊断）：`{ann, kelly, sharpe, max_dd, n_trades, curve}` |
| `out_of_sample` | TEXT(JSON) | **inner OOS 聚合（各 inner test 段）**：`{ann, kelly, sharpe, max_dd, calmar, n_trades, curve}`，分层裁判 L1 主目标 `calmar` 排序以本字段为准（见 §3.5）。**⚠ 这是 inner，参与过挑选、乐观偏误**；冠军去偏真实 OOS 由 `evaluate_champion` 在 outer holdout 跑，**不落本字段、只进发现验收报告**（见 §3.3 / §6.2） |
| `source` | TEXT | `random` / `greedy` / `tpe` / `manual` |
| `created_at` | TEXT(ISO) | 跑批时刻（当前 schema 缺失，补） |

**排序规则**：冠军 = `out_of_sample` 上的**分层裁判**胜出者（L0 可行域闸 → L1 calmar 主目标全序 → L2 DSR 统计裁决 → L3 邻域稳健），Pareto 降为 L1 候选筛选补充（见 §3.5）。

### 3.3 嵌套验证切分（L1 核心，v1.1 重写——防 test 段信息泄露）

颈线法数据 ~5 年（含 tushare 落湖的全市场日线）。**v1 的单层 walk-forward 有致命漏洞**：把全部 4 折 test 段当作 TPE/Pareto 的选择目标，等于"在 4 折 test 的聚合上挑最高"——**没有任何 test 段是"完全不参与参数挑选"的，多重比较从 train 段被搬到了 test 段重演**，号称的"样本外"实则已被污染（见 §8 拷问④）。v1 意识到了多重比较这个敌人，但选的武器（单层 walk-forward）恰恰把泄露留在了 test 段。

v1.1 改为**嵌套验证（nested CV）+ embargo**——金融时序防信息泄露的标准做法（López de Prado 的 purged/embargoed CV）：

```
全样本 = inner 段(2020-2025, 参数选择用) ⊎ outer holdout(2026至今, 仅评估冠军)

inner walk-forward（参数选择 / Pareto 排序的目标函数只在此聚合）:
  折1: train 2020-2021 → test 2022   ← 2022 宽货币型熊市在内(一票否决生效)
  折2: train 2021-2022 → test 2023
  折3: train 2022-2023 → test 2024
  折4: train 2023-2024 → test 2025

outer holdout: 2026 至今
  ↑ 与折4 test(2025) 之间插 embargo 间隔(初定 20 交易日) 防时序信息泄露
  ↑ 全程不参与任何参数选择; 冠军定盘后 evaluate_champion() 跑一次, 报去偏真实 OOS
```

- **inner 目标函数**（§6.2 `objective`）= 各 inner test 段 ann 的几何平均（取悲观侧）+ 熊市一票否决。**TPE 采样、Pareto 排序只认 inner**。注意：inner test 段参与了挑选，其聚合值是**乐观偏误**的，不可直接对外当"真实 OOS"报告。
- **outer 评估**（§6.2 `evaluate_champion`）= 冠军参数在 2026 holdout 上的单次 metrics，**只读、不反馈给搜索**，作为 §12 发现验收闸与上线决策的去偏锚点——这才是真正"完全没参与挑选"的样本外。
- **硬约束：inner test 段必须覆盖熊市**——2022 宽货币型流动性失效是颈线法已知的、事前不可过滤的结构性盲区（见 `docs/neckline-method.md` §2.4）。若某折 test 全是牛市，调出来的"最优"一进熊市必崩。**OOS 熊市表现单独列出，作为一票否决项**。
- **embargo 的物理必要性**：颈线法有持仓跨越（trailing grace 可达数日），折边界处的未平仓 trades 会把 test 段信息漏进相邻段；embargo 间隔吸收这种跨越，是 purged CV 在本策略的物理对应，不可省。
- **切分窗口选型**：信号稀疏（单组创板科创 ~几百笔），按"每折 inner test 段 ≥ N 笔成交"标定（N 待 replay 定，初定 30）。**outer holdout 仅半年（2026 至今），样本偏少是其固有局限**——点估计可信、置信区间宽，故 outer 仅作 Go/No-Go 参考而非唯一判据，须与 §12 邻域稳定性交叉印证。follow-up 可扩为多 outer 段轮流（留一折 outer）。

### 3.4 SQLite Schema（`discovery/trials.db`，复用 `tasks_db.py` WAL 模式）

```sql
CREATE TABLE trial (
  trial_id TEXT PRIMARY KEY,
  params TEXT NOT NULL,
  data_snapshot_hash TEXT NOT NULL,
  engine_hash TEXT NOT NULL,
  score_fn_version INTEGER NOT NULL,
  seed INTEGER NOT NULL,
  split TEXT NOT NULL,
  in_sample TEXT,                 -- JSON: {ann,kelly,sharpe,max_dd,n_trades}
  out_of_sample TEXT,             -- JSON: 同上 + bear_market_ann(熊市OOS一票否决)
  source TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE INDEX idx_snapshot ON trial(data_snapshot_hash);
CREATE INDEX idx_oos ON trial(out_of_sample);  -- 冠军查询走这里

CREATE TABLE snapshot (           -- L0 快照登记表
  snapshot_hash TEXT PRIMARY KEY,
  universe_def TEXT NOT NULL,     -- 创板科创 / 全市场 定义
  universe_count INTEGER,
  date_range TEXT NOT NULL,
  data_lake_commit TEXT,          -- 数据湖版本锚点
  created_at TEXT NOT NULL);

CREATE TABLE search_run (         -- 每次长期跑批
  run_id TEXT PRIMARY KEY,
  snapshot_hash TEXT NOT NULL,
  engine_hash TEXT NOT NULL,
  score_fn_version INTEGER,
  started_at TEXT, ended_at TEXT,
  n_trials INTEGER, status TEXT,  -- running/converged/budget_exhausted/stopped
  convergence_note TEXT);          -- 收敛判据命中记录
```

并发写用 SQLite WAL（`tasks_db.py` 既有的 Row 工厂 + 单点写防跨进程锁模式直接复用）。

### 3.5 分层裁判：评价体系（v1.2 重写——"相对最优"的可操作定义）

**重写动机（探查实证）**：L1 Go/No-Go 探查（`discovery/tools/probe_champion_oos.py`）实测当前冠军 inner 全段：`risk_metrics` 算出夏普 11.7 / ann 115.8%，2026 段更高到夏普 15.5 / ann 201.8%——**这些不是实盘可达值**，是 `risk_metrics` 的 `freq_cap=150 笔复利 + 夏普=per-trade×√年交易数` 在高频短段下的**算法放大产物**（顶级高频真实夏普仅 3-5）。单一 `score=ann×sharpe/(1+max_dd)` 或 Pareto 在这种失真口径上排序，等于在放大的噪声上挑最高。**评价体系必须分层——把"可行/排序/统计显著性/稳健性"分开裁判，各自用物理贴切的度量。**

**分层裁判（tournament，逐层收紧）**：

| 层 | 裁判 | 度量 | 物理意图 |
|---|---|---|---|
| **L0 可行域闸**（一票否决） | max_dd≤0.4 ∧ 熊市段 ann≥0 ∧ n_trades≥30 | 硬约束 | 砍掉"风险失控/熊市崩/样本不足"；颈线法熊市软肋必须硬约束，不能靠主目标平均掉 |
| **L1 主目标排序**（可行域内全序） | **calmar = ann / max_dd** | 单一主目标 | "每单位回撤换多少收益"。颈线法主风险是回撤（熊市）非波动，calmar 比 sharpe 贴；单目标→全序，化解 Pareto 高维稀疏 |
| **L2 统计裁决**（防多重比较膨胀） | **Deflated Sharpe Ratio**（§3.7） | 概率 | 即便没偷看，在 M 组里挑最高本身虚高期望；DSR 修正试验次数 M + 非正态，给"优势是真实而非运气"的概率 |
| **L3 邻域稳健裁决**（防孤峰） | 冠军 21 维 ±扰动下 calmar 方差小 | 邻域方差 | 高原（邻域稳）放行，孤峰（邻域塌）降级；可操作化见 §12⑥ |

- **Pareto 降级**为 L1 的**候选筛选补充**（前沿上的解进 L1 全序，而非主排序）——保留多目标结构信息，但不让它退化成"前沿一堆点谁也压不住谁"。
- **冠军 = 过四层、calmar 最高、DSR 显著、邻域稳的参数**。每层都有明确物理意图和可操作阈值，避免单一 score 的"权重即偏见"与 Pareto 的"前沿稀疏 + 噪声敏感"。

**收敛判据（v1.1 加覆盖度维度，命中即停该 run）**：
1. 连续 K 轮（初定 K=3）新 trial 无一进入 Pareto 前沿（前沿不扩张）；
2. TPE 后验的预期提升 EI < ε（L3 引入 TPE 后生效）；
3. 预算耗尽（时间 / 组数 / token，见 ADR1 的 L4 预算）；
4. **★ 参数空间覆盖度达标（v1.1 补，防伪收敛）**：Sobol/TPE 采样已覆盖有效搜索区域 ≥ ρ（初定 ρ=0.8，按 §3.6 算力账标定的"覆盖所需夜数"反推）。

**⚠ 伪收敛陷阱（v1.1 拷问）**：判据 1-3 只能证明"当前采样策略下没新东西"，**不能证明参数空间被充分探索**。若初始采样有盲区（21 维随机/贪心极易整片错过有效区域），判据 1 会在一个采样不足的前沿上早早命中、自停，交付一个"撞到的孤峰"冒充相对最优。**故判据 4（覆盖度）是 1-3 的前置否决**——覆盖度不达标，判据 1 即使命中也不许停，须扩采样（§7.2 Sobol + 邻域扰动）继续探索。没有判据 4，长期任务会在算力空烧与伪收敛之间二选一。

---

### 3.6 算力账与覆盖预算（v1.1 补——回答"长期跑要跑多久"）

搜索有效性不能停留在口号，必须有一张"空间规模 × 单组成本 × 夜跑预算 = 覆盖所需夜数"的粗账，否则 L4 守护是无底洞：

| 量级 | 估算（待 L1 replay 标定） | 说明 |
|---|---|---|
| 单组嵌套成本 | ~150-200s | inner 4 折 test 段总长 ≈ 全样本（train 段仅诊断可省）；较 v1 全样本单组 ~185s 基本持平，**不会因嵌套暴涨**（嵌套只多一次 outer 评估，且 outer 仅对冠军跑） |
| 有效搜索空间 | 裁剪后 ~10³-10⁴ 量级 | 21 维经 §7.1 六处耦合裁剪 + 离散化后；精确规模待 `constraints.py` 标定 |
| 夜跑吞吐 | ~70-90 组/夜 | `--budget 4h` × 单组 ~180s × ProcessPool(核数-2) |
| 覆盖 ρ=0.8 所需 | ~10²-10³ 夜（粗估） | 取决于 Sobol 覆盖效率；**此量级直接决定 L4 是否可行** |

**结论与决策**：
- 若覆盖所需夜数 > 30 夜 → **纯随机/贪心不够**，必须上 Sobol 准随机初始覆盖（§7.2，低差异序列覆盖效率远高于纯随机）+ TPE，否则 L4 在可接受周期内根本采不满。
- 若即便 Sobol+TPE 仍 > 60 夜 → 触发 ADR1 决策风险：**参数空间对当前算力过大，应缩 universe / 降维（固定更多执行层参数）/ 接受更稀疏覆盖**，而非空烧。
- 这张账是 L1→L2 之间"发现验收闸"的输入之一（§12 ⑦）：**算力账不可行 → 整个长期系统在物理上不成立，及时止损**。

---

### 3.7 统计裁决：Deflated Sharpe Ratio（v1.2 新增——防多重比较的统计膨胀）

**问题（与 test 段泄露并列的第二大方法论洞）**：ADR1 点名了多重比较过拟合，v1.1 用嵌套验证防了"数据窥探"（参数偷看 test 段），但**没防"选 bias"**——即便完全不偷看，在 M 组参数里挑 calmar/sharpe 最高，这个"最高"的期望本就随 M 虚高（order statistics）。颈线法 21 维搜成百上千组，选 bias 不可忽略。

**武器：Deflated Sharpe Ratio（DSR，López de Prado）**——闭式公式，不需重跑回测，输入：top 候选的 sharpe、收益序列偏度/峰度（修正非正态）、试验次数 M（修正多重比较）。输出：零假设（最优策略≈基准）下观察到当前 sharpe 的概率。DSR 概率高 → 优势大概率是运气；低 → 统计显著。

- **为什么不朴素 sharpe**：试 M 组后"最高 sharpe"期望被多重比较抬高，必须 deflated；DSR 还修正颈线法 trades 的尖峰厚尾（skew/kurt correction）。
- **位置**：L2 裁判，作用在 L1 calmar 排序后的 top-N 候选（如 top-20），不全局算（全局 M 太贵且无意义）。
- **诚实边界（评价天花板）**：DSR 依赖样本量。颈线法信号稀疏（单组几百笔、切折后每折几十~百笔）→ DSR 置信区间宽。极端情况 DSR 可能说"top-5 优劣在噪声内不可辨"——**那就承认"相对最优"在该数据量下不可辨识，而非硬选一个**（见 §13）。
- **follow-up**：若 DSR 功效不足，升级 White's Reality Check / Hansen's SPA（bootstrap，无分布假设，更通用更贵）。

---

## 4. 架构与组件

### 4.1 新增 `discovery/` 包

```
discovery/
  __init__.py        # 导出 run_trial / resolve_champions / 公开 API
  snapshot.py        # L0: 冻结 universe+行情 → sha256 指纹，登记 snapshot 表
  split.py           # L1: 嵌套切分（inner walk-forward 折 + outer holdout + embargo，纯函数）
  objective.py       # L1: objective(inner 选参,含熊市一票否决) + evaluate_champion(outer 纯评估,不反馈搜索)
  worker.py          # L2: ProcessPool + initializer 复用 data_lake（复用 backtest/worker.py 模式）
  store.py           # L2: SQLite CRUD（复用 tasks_db.py WAL），trial/snapshot/search_run 三表
  constraints.py     # L3: 参数约束裁剪（6 处耦合的合法组合过滤器，纯函数）
  search.py          # L3: 搜索调度（Sobol 初始覆盖 → TPE），约束裁剪后采样;含覆盖度统计
  pareto.py          # L4: Pareto 非支配前沿 + 收敛判据(含覆盖度判据④,纯函数)
  daemon.py          # L4: schtasks 夜跑守护 + 断点续跑
  publish.py         # L5: 冠军 → experiment.create（DRAFT + source 标记）
  cli.py             # python -m discovery snapshot|run|champions|publish|report
```

### 4.2 依赖方向（零反向依赖红线）

```
strategies/neckline/ ←── scan_symbol(params, df, dates) ──← discovery/ (objective/worker, 只读内核)
   ↑ simulate_exit/detect_neckline_method (识别+出场内核, 零改动)
discovery/ ──publish(DRAFT)──→ experiment/ (store.create, 仅 create 不 promote)
   │
   └── 复用 backtest/worker.py(ProcessPool模式) + backtest/tasks_db.py(SQLite WAL模式)
```

- **`discovery/` 零依赖** `trading/`（不做实盘下单）；只读 `strategies/neckline/` 内核 + 写 `experiment/` 的 DRAFT 版本。
- 回测内核 `strategies/neckline/` **零改动**——`scan_symbol` 已是同源契约守护的研究侧入口（`tests/test_param_iter_kernel_same_source.py`），发现引擎直接调用，保证"调出来的参数在实盘真生效"。

### 4.3 复用既有基建（不重造，ADR6）

| 既有设施 | 复用方式 |
|---|---|
| `backtest/worker.py`（ProcessPool + initializer 加载 data_lake 复用） | `discovery/worker.py` 套同模式，每 worker 加载一次快照数据，多组试验复用 |
| `backtest/tasks_db.py`（SQLite WAL + Row 工厂 + 单点写） | `discovery/store.py` 套同模式，三表 schema 见 §3.4 |
| `strategies/neckline/backtest.py::scan_symbol` / `risk_metrics` | `objective.py` 直接调用，pos_cap=0.05/freq_cap=150 口径保持（执行参数，不进搜索空间） |
| `experiment/cli.py::create` | `publish.py` 调用建 DRAFT 候选 |

---

## 5. 数据流

### 5.1 单组 Trial 执行流（含 OOS）

```
discovery.search.next_params()                    ← 约束裁剪后采样(sobol初始覆盖→tpe)
  ├─ 1. for each walk-forward 折 wf_i:
  │     train_df = snapshot.load(universe, wf_i.train_dates)   ← L0 冻结快照
  │     test_df  = snapshot.load(universe, wf_i.test_dates)
  │     for sym in universe:
  │       # 参数在 train 段无任何调参，scan_symbol 只是"用这组参数跑一遍"
  │       trades_train += scan_symbol(sym, train_df, params)
  │       trades_test  += scan_symbol(sym, test_df,  params)   ← 同一组参数跑 test
  │     is_metrics_i = risk_metrics(trades_train)              ← 样本内
  │     oos_metrics_i = risk_metrics(trades_test)              ← 样本外(含熊市子段)
  ├─ 2. out_of_sample = aggregate(oos_metrics_i over folds)    ← 取悲观侧(几何均/min)
  │     bear_oos = risk_metrics(trades_test ∩ 熊市段)           ← 一票否决项
  ├─ 3. store.write_trial(trial_id=sha256(params+snapshot+seed),
  │       params, snapshot_hash, engine_hash, in_sample, out_of_sample+bear_oos, ...)
  └─ 4. pareto.update_frontier(trial)                          ← 更新 Pareto 前沿
```

**关键（v1.1 订正）**：`scan_symbol` 是纯函数式回测（给定参数+数据→trades），参数在 train/test 上都不"学习"。**但"没参与挑选"这层语义，v1 错安在了 inner test 段上**——inner test 段聚合正是 TPE/Pareto 的选择目标，它**参与了挑选**。真正"完全没参与挑选"的是 §3.3 的 **outer holdout**：单组 trial 流程（上图）只在 inner 上跑 `objective`；**冠军定盘后**，由 `evaluate_champion(champion_params, outer_holdout)` 单独跑一次 2026 段，结果只进发现验收报告（§12），**不回写给搜索**。颈线法无在线学习、参数静态，故 train 段 metrics 仅诊断，inner 排序只认 inner test 段，outer 只认 outer holdout——三层各司其职，堵住 test 段泄露。

### 5.2 长期守护调度（L4）

```
schtasks: 每日 02:00 触发 discovery.daemon --run --budget 4h   ← 夜间闲置算力
  ├─ load search_run(上次未收敛的 run) ── 断点续跑(trial_id 去重)
  ├─ while 预算未耗尽 AND 未命中收敛判据:
  │     trial = next_params() → run_trial() → store → pareto.update
  │     if pareto.converged(K=3): break                       ← 收敛自停
  ├─ search_run.status = converged | budget_exhausted
  └─ if 有新冠军进入前沿: 推钉钉告警(供人/影子验证决策)
```

### 5.3 闭环流转（L5）

```
discovery.publish(champion_trial_id)
  ├─ experiment.create(strategy="neckline",
  │     params=trial.params, source=f"discovery:{run_id}",
  │     experiment_id=f"neckline_disc_{date}", status="DRAFT")
  ├─ 人工 review OOS 报告(含熊市段表现) → promote 到 weight 小份额(如 0.1)
  ├─ set AUTO_TRADE_MODE=dry_run → 二期引擎 _eod resolve → scan_live 影子跑 ≥5 天
  │     (trading/__main__.py:103-111 已有的 ≥5 天硬闸)
  └─ report 对比 prod vs candidate → 人审决定扩量/下线
```

### 5.4 可比性边界

- **数据快照变更 = 新 search_run**：`data_snapshot_hash` 一变，老 trial 的 OOS 不可直接与新 trial 比（数据不同）。`resolve_champions` 只在**同一 snapshot_hash + engine_hash + score_fn_version** 内排序。
- **引擎/目标函数变更 = 老 trial 标 stale**：`engine_hash` / `score_fn_version` 不一致时，老 trial 不进前沿、保留作历史。

---

## 6. 核心实现：可信度闭环（L1，阻塞一切）

### 6.1 数据快照冻结（L0）

当前 `param_iter` 每次直接读 data_lake，数据湖一更新，所有历史试验作废。L0 给每次 `search_run` 冻结一个快照：

- 快照 = `universe 定义`（创板科创 / 全市场，`is_target_board` + 近 30 日成交额≥1 亿过滤）+ `行情 parquet 内容` 的 sha256。
- 落 `snapshot` 表登记（universe_def / count / date_range / data_lake_commit）。
- **数据湖更新不自动失效老 trial**——老 trial 仍可查、可复现（只要 parquet 还在），只是不能与新快照的 trial 混排。

### 6.2 嵌套目标函数（L1，v1.1 重写——inner 选参 + outer 纯评估）

把当前 `score_of`（`param_iter.py:70-81`，全样本内单一 score）替换为 §3.3 的**嵌套双函数**：

```python
def objective(params, snapshot, inner_folds):
    """inner 目标：供 TPE 采样 / Pareto 排序。各 inner test 段 ann 几何均 + 熊市一票否决。
    注意：inner test 段参与了挑选，其聚合值是乐观偏误的，不可直接当"真实OOS"对外报告。"""
    oos_anns, bear_anns = [], []
    for fold in inner_folds:
        test_trades = run_on_fold(params, snapshot, fold.test_dates)
        oos_anns.append(risk_metrics(test_trades)["ann"])
        bear_trades = [t for t in test_trades if t.date in BEAR_MARKET_DAYS]  # 熊市段
        bear_anns.append(risk_metrics(bear_trades)["ann"])
    geo = geomean(oos_anns)
    if min(bear_anns) < 0:                 # inner test 任一折熊市段为负 → 一票否决
        geo = -inf                         # 进不了 Pareto 前沿
    return {"oos_ann": geo, "oos_sharpe": ..., "oos_max_dd": ..., "bear_ann": min(bear_anns)}

def evaluate_champion(params, snapshot, outer_holdout):
    """outer 纯评估：冠军定盘后单次调用，在 2026 holdout 上跑一遍。
    不参与任何参数选择，结果只进发现验收报告(§12)与上线决策——这才是去偏的"真实OOS"。"""
    trades = run_on_fold(params, snapshot, outer_holdout.dates)  # 已含 embargo 间隔
    return risk_metrics(trades)  # {ann, kelly, sharpe, max_dd, n_trades, bear_ann}
```

**实现说明**：`run_on_fold` 即遍历 universe 调 `scan_symbol`（复用 `param_iter.py:run_one` 的内核，切分 dates）。`BEAR_MARKET_DAYS` 来自四层动能评分的流动性识别（2018 钱荒型 / 2022 宽货币型），即使核心链路不接入动能过滤，熊市日期标签仍可用于 OOS 切片评估。**`objective` 与 `evaluate_champion` 严禁共享状态**——后者读不到前者的采样历史，物理隔离信息流，是防 test 段泄露的代码级保障。

### 6.3 OOS 熊市覆盖（一票否决的物理意图）

颈线法的软肋是熊市（`docs/neckline-method.md` 已证：2022 宽货币型事前不可过滤）。所以"相对最优"**必须定义为"在覆盖熊市的样本外仍稳健"**，而非"全样本年化最高"。这是本引擎与当前 `param_iter` 最大的方法论分野——**§1.4 实证近期 OOS 反最强，但熊市 regime 未测**；"可信"的真义从 v1 的"去偏样本外不塌"转向"**覆盖熊市 regime 仍稳健**"（见 §8 拷问）。

---

## 7. 搜索层（L3）

### 7.1 约束裁剪（先于贝叶斯，ADR4 反魔法）

21 维笛卡尔积里有 6 处耦合的物理无意义组合，**裁剪掉再搜**（纯函数过滤器，零新增依赖）：

| # | 耦合（代码定位） | 裁剪规则 |
|---|---|---|
| 1 | trailing 三元组互锁（`backtest.py:125`）：`grace>0 AND step>0` 才激活，否则等价固定止损 | `grace=0` 时 `step/floor` 固定（不搜），避免白跑 |
| 2 | `min_rr` 是死参数（`method_v0.py:46`，结构恒 `rr=2.0`） | 固定不搜 |
| 3 | `tp1_h_mult ≤ tp_h_mult`（`backtest.py:78-79`） | 剔除 `tp1>tp_h` 退化组合 |
| 4 | `cancel_thresh_mult ≥ tp1_h_mult`（语义：未到 tp1 就撤单 = 放弃突破） | 剔除 `cancel<tp1` 过保守组合 |
| 5 | `min_suppression` ↔ `decay_tau` 同开关（`method_v0.py:163-173`） | `decay_tau=None` 时压制等权；二者捆绑调 |
| 6 | `buy_limit_atr_mult < cancel_thresh_mult×(H/ATR)`（挂单区间非空） | 按当时 H/ATR 剔除挂单区间为空的组合 |

裁剪后有效搜索空间显著缩小，随机+贪心也能更高效，**不必第一刀就上 optuna**。

### 7.2 初始覆盖 + 贝叶斯 TPE（v1.1 重写——先覆盖再优化）

v1 把 TPE 当"可选增强"是 L3 空心的根源。v1.1 确立 **Sobol 准随机初始覆盖 → TPE 序贯优化** 两阶段，回答"长期探索为什么能收敛":

- **阶段一·Sobol 初始覆盖**（前置，必做）：纯随机/贪心在 21 维空间覆盖极不均匀（聚集+盲区并存），是 §3.5 伪收敛的元凶。**Sobol 准随机序列**（低差异序列，`optuna.samplers.SobolSampler` 或自写）以远低于纯随机的样本数达到空间均匀覆盖——这是判据④（覆盖度）达标的物理手段，**先铺满再谈优化**。
- **阶段二·TPE 序贯优化**（Sobol 覆盖达标后）：在 Sobol 撒的点上拟合 TPE 后验，向预期提升 EI 高的区域集中采样。**关键前提：TPE 的目标函数必须是 `objective`（inner OOS ann），否则只是更快拟合噪声**。TPE 后验不确定性天然支撑 §3.5 判据②（EI < ε）。

**为什么这套够用 / 何时不够**（搜索动力学论证）：
- 颈线法参数空间经 §7.1 裁剪后是**中等维度、可离散化**的（识别层 11 维多为阈值/窗口，执行层 7 维多为乘数），Sobol+TPE 在此类空间是公认够用的（对比：>50 维强非凸才需 CMA-ES/进化算法）。
- **升级条件（follow-up）**：若 §3.6 算力账显示 Sobol+TPE 覆盖 ρ=0.8 仍 >60 夜，或前沿长期在多个相隔很远的区域跳动（多模态），再评估 CMA-ES / NSGA-II（多目标进化，天然出 Pareto 前沿）。**不第一刀就上重型优化器**（反魔法，ADR4）。

`optuna` 是 `requirements.txt` 唯一新增依赖（轻量，非黑盒量化库），同时提供 Sobol + TPE + 多目标 ParetoFrontSampler，一套到位。

---

## 8. 错误处理与风控边界（CLAUDE.md 拷问三连）

| 边界 | 处置 |
|---|---|
| **流动性与极端行情**（拷问①） | 回测内核 `scan_symbol` 遇停牌/缺数据内部跳过（既有）；OOS 评估**覆盖 2022 熊市段**，冠军必须在熊市段 ann≥0，防止"牛市冠军进熊市滑点失控"。回测用前复权日线 `.loc[:date]` 无前视，避免地缘危机段的未来函数 |
| **接口与状态机边界**（拷问②） | ProcessPool worker 崩溃 → 单 trial 失败标 `failed` 不影响 run（`parallel` 的 null 过滤模式）；SQLite WAL 单点写防跨进程锁；断点续跑靠 `trial_id` 去重，kill/重启自动接续；数据快照 hash 不匹配时 fail-fast（数据漂移则拒绝混排） |
| **策略风险敞口**（拷问③） | 颈线法非做空偏好、无杠杆，敞口风险=持仓时间+回撤。分层裁判 L0 闸用 `max_dd≤0.4` 硬约束防极端回撤冠军（v1.2：原"Pareto 约束"并入分层裁判 L0）；`freq_cap=150`（既有）模拟实盘并发约束；冠军经影子验证 ≥5 天才 promote，逼空/保证金风险在影子期暴露 |
| **过拟合 / 数据窥探**（方法学风控，本引擎核心） | **嵌套验证（inner 选参 / outer embargo 纯评估）** + 熊市一票否决 + 数据快照冻结 + engine/score_fn 版本戳，五重防多重比较污染。**v1 单层 walk-forward 把 test 段当选择目标致泄露，v1.1 嵌套修正**（见 §3.3） |
| **算力枯竭 / 伪收敛**（v1.1 补，搜索有效性风控） | 收敛判据④（覆盖度）前置否决判据①——覆盖度不达标不许自停；§3.6 算力账不可行时触发 ADR1 止损（缩 universe/降维），不空烧；ProcessPool worker 崩溃单 trial 失败不影响 run |

---

## 9. 测试策略（新建 `tests/discovery/`）

- **L0 快照**：`test_snapshot_freeze`（同 universe+行情 → 同 hash；行情变 → hash 变）、`test_snapshot_reproducible`（同 hash 重载结果一致）
- **L1 切分/目标**：`test_walkforward_folds`（inner 折数/日期/无重叠）、`test_outer_holdout_isolated`（outer holdout 与 inner 无重叠且含 embargo）、`test_objective_uses_inner_only`（选参只读 inner test，不碰 outer）、`test_evaluate_champion_no_feedback`（outer 评估结果不回写给搜索——信息流物理隔离）、`test_bear_market_veto`（熊市段 ann<0 → 进不了前沿）、`test_current_champion_oos`（**拿当前 168 组 state 冠军补跑嵌套 OOS，固化 outer 去偏真实水平**，作为 L1 验收锚点）
- **L2 落库**：`test_store_trial_dedup`（trial_id 去重）、`test_concurrent_write_wal`（多进程并发写不锁死）、`test_stale_trial_excluded`（engine_hash 不符不进前沿）
- **L3 裁剪**：`test_constraints_filter_invalid`（6 处耦合的非法组合全被滤）、`test_valid_combination_kept`
- **L4 收敛**：`test_pareto_frontier`（L1 候选筛选：非支配解正确）、`test_convergence_k_rounds`（连续 K 轮无新前沿）、`test_coverage_gate_blocks_early_stop`（**覆盖度 <ρ 时即便前沿不扩张也不许停**——伪收敛防护）、`test_sobol_coverage_uniformity`（Sobol 初始采样空间均匀性 ≥ 纯随机）
- **评价体系（v1.2 补）**：`test_feasibility_gate`（L0 闸：max_dd/熊市 ann/n_trades 不达标直接淘汰）、`test_calmar_ordering`（L1 主目标 calmar 全序正确）、`test_dsr_significance`（L2 DSR 对 top-N 给出显著/运气判定）、`test_neighborhood_stable`（L3 邻域 ±扰动 calmar 方差小=高原放行，方差大=孤峰降级）
- **L5 闭环**：`test_publish_creates_draft`（冠军 → experiment DRAFT，source 标记）、`test_no_auto_promote`（MVP 不自动 promote）
- **★ 发现验收闸（v1.1 补）**：`test_baseline_outperform`（发现引擎冠军嵌套 OOS 显著优于手跑 168 组冠军，或覆盖更稳健区域——否则 Go/No-Go 不过）、`test_champion_neighborhood_stable`（冠军 21 维 ±扰动下嵌套 OOS 不塌，高原非孤峰）
- **零回归**：`tests/test_param_iter_kernel_same_source.py`（同源契约）保持绿；回测内核零改动

---

## 10. 部署与调度

- **L1-L2**：手动单机跑（`python -m discovery snapshot && run`），白天/夜间均可
- **L4 长期守护**：`schtasks` 注册 `discovery.daemon --run --budget 4h`，每日 02:00 夜间触发（复用观测运营层 schtasks 模式，见 `quanter-ops-layer-phase1`）。断点续跑靠 trial_id 去重。
- **L5**：手动 `python -m discovery publish <champion_id>` → 人工钉钉确认 → experiment promote
- **环境**：`.venv310`（与 miniQMT xtquant 同环境，回测与实盘同 Python）；数据来自 data_lake（tushare 35 数据集落湖）

---

## 11. 关键决策记录（ADR）

- **ADR1（最高）**：**可信度优先于吞吐，顺序锁定不可跳**。L1（OOS + 快照）阻塞一切——不解决过拟合，L2-L5 越强越在拟合噪声。先做 L1，拿当前 168 组冠军补跑 OOS 暴露真实水平，再决定是否铺长期系统。
- **ADR2**：**walk-forward 而非简单 holdout**。颈线法无在线学习，但单次 holdout 信号稀疏且仍可在 train 段过拟合；多折滚动 + 熊市覆盖更稳健。
- **ADR3**：**数据快照指纹冻结**（snapshot_hash + engine_hash + score_fn_version）。没有冻结就没有可比性，长期任务的基石。
- **ADR4（反魔法）**：**约束裁剪先于贝叶斯 TPE**。先用纯函数砍掉 6 处耦合废组合，不第一刀就上 optuna；TPE 仅在 OOS 目标确立后作为可选增强。
- **ADR5**：**多目标 Pareto 前沿替代单一 score**。"相对最优"本质是多目标（年化/回撤/夏普/笔数），单一 `score` 会选出"高年化+爆炸回撤"的伪最优。
- **ADR6**：**复用 `worker.py`/`tasks_db.py`，不重造**。ProcessPool + SQLite WAL 既有基建成熟，零新增重型依赖（符合 Karpathy 极简）。
- **ADR7**：**填 Spec4 闭环（→experiment DRAFT）**。冠军参数自动建 DRAFT 候选 + source 溯源，promote 经人工 + 影子验证（不自动 promote，防过拟合参数直冲实盘）。
- **ADR8**：**回测内核 `strategies/neckline/` 零改动**。`scan_symbol` 同源契约守护，发现引擎只读内核，保证"调出来的参数实盘真生效"。
- **ADR9（v1.1，方法论）**：**嵌套验证替代单层 walk-forward**。v1 把 inner test 段当选择目标致多重比较泄露；v1.1 inner 选参 + outer holdout + embargo 纯评估，test 段才真正"不参与挑选"。可信度闭环的物理基础。
- **ADR10（v1.1，验收）**：**发现验收闸（基线对照 + 邻域稳定性）作为 L1→L2 硬关卡**。工程验收（schema/并发/Pareto）只证"基建搭成"；发现验收证"基建比手跑多发现了东西"。无发现验收，长期系统无交付价值。
- **ADR11（v1.1，搜索）**：**Sobol 初始覆盖 → TPE 序贯优化，收敛判据加覆盖度前置否决**。纯随机/贪心在 21 维有盲区会伪收敛；Sobol 低差异序列先铺满、判据④保证"自停=真探索完"而非"采不动了"。配套 §3.6 算力账定 ρ。
- **ADR12（v1.2，评价）**：**分层裁判替代单一 score / Pareto 主排序**。探查实证 `risk_metrics` 夏普/ann 在高频短段算法放大失真（夏普 15 / ann 201%），单一 score 在失真口径排序=在噪声上挑最高；分层把可行/排序/统计/稳健分开，主目标用 **calmar**（贴颈线法回撤风险，非波动）。
- **ADR13（v1.2，统计）**：**DSR 防多重比较选 bias**。嵌套防偷看、DSR 防选 bias——两个不同的敌人，缺一不可。颈线法信号稀疏致 DSR 功效弱是固有天花板，须如实报置信区间、不强选。

---

## 12. 验收标准（分阶段，每层独立可验收）

**L1（可信度闭环，最高优先）**：
1. `discovery.snapshot` 能冻结快照并产稳定 sha256；同数据重载结果一致
2. ✅ **已实证（§1.4，2026-07-24）**：当前冠军 top-5 补跑 2026 近期 OOS，普遍 ann 145-182%（**未塌、反最强**），过拟合假设证伪。**真正的 L1 验收**升级为：嵌套 OOS（inner 选参 / outer 纯评估）+ **熊市 regime 覆盖**（2022 段，当前 2025 截面 universe 不含、需扩数据）+ **快照冻结复现**（修 §1.4 漂移）
3. 熊市段一票否决生效（熊市 ann<0 的组合进不了前沿）
4. outer holdout 信息隔离：`evaluate_champion` 结果不回写给搜索（`test_evaluate_champion_no_feedback`）

**★ 发现验收闸（L1 → L2 硬关卡，v1.1 补——不通过则停止铺 L2-L5）**：
5. **基线对照**：发现引擎小规模跑后冠军的嵌套 OOS（outer ann），显著优于当前 168 组手跑冠军的嵌套 OOS，或至少覆盖更稳健参数区域（分层裁判候选前沿扩张）
6. **参数邻域稳定性**：冠军在 21 维 ±扰动（每维 ±10-20%）采样下，嵌套 OOS 不塌（高原而非孤峰）；孤峰冠军直接否决
7. **算力账可行**：§3.6 覆盖 ρ=0.8 所需夜数 ≤ 30 夜（Sobol+TPE），否则触发 ADR1 止损（缩 universe/降维）

**L2（吞吐）**：
8. SQLite 落库（trial/snapshot/search_run 三表）+ ProcessPool 并发，吞吐较单进程 ×N（N=核数-2）
9. 断点续跑：kill 后重启，已写 trial 不重跑

**L3（搜索）**：
10. 约束裁剪：6 处耦合非法组合 100% 被滤，有效搜索空间缩小
11. （可选）TPE 在 OOS 目标上跑通，后验收敛判据②可用

**L4（自治）**：
12. 分层裁判四层正确（L0 可行域闸 / L1 calmar 全序 / L2 DSR 显著性 / L3 邻域稳健）；连续 K=3 轮无新候选前沿 → run 自停
13. schtasks 夜跑守护 + 断点续跑稳定

**L5（闭环）**：
14. 冠军 → `experiment create` 建 DRAFT（source=`discovery:run_xxx`）；影子 dry_run ≥5 天链路通；**不自动 promote**

**全程零回归**：`test_param_iter_kernel_same_source.py` 绿；回测内核零改动；既有测试不新增失败。

---

## 13. 风险与 follow-up

- **L1 决策风险（v1.3 实证更新）**：~~原赌"OOS 塌到个位数 → 过拟合，No-Go"~~ **已被 §1.4 探查证伪**——top-5 在 2026 普遍 ann 145-182%，颈线法有真实 alpha。**真风险三转移**：(a) regime 依赖（2025-2026 无大熊市，软肋未测，须扩 2022 段数据做 regime 覆盖）；(b) 评价失真（夏普/ann 算法放大，§3.5 分层裁判校准）；(c) 数据快照漂移（full 段复现漂移 1.8-7.2%，实证 L0 快照冻结必须前置）。发现验收闸（§12 ⑤⑥⑦）仍须过——证明不了"比手跑强"就不铺 L2-L5。
- **样本稀疏**：walk-forward 切分后单折 test 笔数可能不足（创板科创单组~几百笔，切 4 折后每折~百笔）。需按"每折 ≥30 笔"标定窗口，否则统计意义不足。
- **熊市样本不足**：5 年内有效熊市段有限（2018 钱荒型 / 2022 宽货币型），OOS 熊市覆盖可能样本偏少——这是颈线法的固有软肋，无法靠调参解决，只能如实标注。
- **outer holdout 样本短（v1.1）**：2026 至今仅半年，outer 去偏点估计置信区间宽；须与邻域稳定性（§12⑥）交叉印证，不单独判生死。follow-up 扩多 outer 段轮流（留一折 outer）。
- **伪收敛残余风险（v1.1）**：判据④覆盖度用 Sobol 均匀性近似"探索充分"，但低差异序列对高度非凸/离散跳跃区域仍可能盲；邻域稳定性测试（§12⑥）是最后一道兜底——孤峰会被邻域扰动暴露。
- **贝叶斯依赖**（L3）：引入 optuna 是新增依赖，遵循反魔法原则，仅在 OOS 确立后、且约束裁剪仍不够时才上。
- **多口径**（follow-up）：当前仅全市场 / 创板科创两个口径，未来按牛熊/板块切多口径属 follow-up。
- **自动 promote**（follow-up）：MVP 不做，待影子验证闭环成熟 + 人工 promote 跑顺后再考虑自动化。

链接 [[neckline-paramiter-baseline]] [[quanter-experiment-system]] [[neckline-method]] [[neckline-trailing-stop]] [[quanter-param-training-platform]] [[global-architecture-before-details]]。
