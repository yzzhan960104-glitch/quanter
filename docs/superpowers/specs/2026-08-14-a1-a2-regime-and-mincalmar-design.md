# A1+A2 设计：regime 闸产品化 + 各年 min calmar 搜索目标（DG-G4 落地）

---
title: A1+A2 regime 闸 + min-calmar 搜索目标
date: 2026-08-14
status: draft（待用户审阅）
author: session（peer reviewer / risk officer 姿态）
scope: 策略可信波 A1/A2 两工单的实施设计（治理 spec §3 A1 + DG-G4）
related:
  - docs/superpowers/specs/2026-08-13-full-audit-and-governance-spec.md（§3 A1/A2 + §6 DG-G4）
  - docs/architecture/deep-dives/2026-08-14-critical-review.md（§4.1 停损判据 + §6.1 优先级）
---

## 0. 触发证据（为什么是现在）

2026-08-14 晚对 ACTIVE 冠军 `neckline_disc_20260725_25c602` 实弹验证（数据已含当日
8/8-8/14 补采），停损判据三件两死一未验：

| 证据 | 结果 | 判据 |
|---|---|---|
| wf 四折折外 | 0.00 / **-0.62** / 1.58 / 3.93 | ❌ 分年 Calmar 全正（2022 熊市折外负） |
| oos 去偏 | inner 44.87 → outer 10.22（衰减 77%） | ❌ 折外衰减 <50% |
| 滑点敏感性 | A3 未动工 | ⚪ 前两条已死，非决定性 |

结论：冠军是「2025 特化」参数；regime 过拟合 + 熊市负期望坐实。用户裁定 A1+A2 全速
（2026-08-14），且 P1-1 已批（老 trial 作废，今晚 daemon 新基线重搜）——A2 必须赶在
新基线首搜前落地，否则首搜仍用旧 inner-calmar 目标，大概率复刻下一个 25c602。

## 1. A2：搜索目标改「inner 各年 min calmar」

### 1.1 现状与落点

- `discovery/split.py::holdout_split`（embargo 二分：inner 2025 / outer 2026，
  lake_start=2025-01-01）→ `discovery/objective.py::evaluate` 返回
  `inner_metrics{ann, calmar, sharpe, max_dd, n}` → 搜索排序/收敛消费 inner calmar。
- 问题①：inner 只有 2025 一个自然年，「各年 min」退化为单年——**必须扩窗含 2022 熊市**。
- 问题②：排序目标若仍是整段 calmar，单段特化参数仍会登顶。

### 1.2 设计裁定

1. **窗口扩至 2021-01-01**（新基线默认）：inner = 2021-2024（四个自然年，含 2022 熊市
   + 2023 震荡 + 2024 结构牛），outer = 2025-2026。Why 2021 起非 2020：湖深 P0-2 实证
   2016 起全覆盖，2020 多一年数据量 +33% 但 2020 是单边牛（对 min-calmar 无增量判别力）；
   2021 抱团瓦解震荡年恰是好的第一考场。
2. **inner_metrics 新增 `yearly_calmar: {year: float}`**（evaluate 内按自然年分块算
   calmar；某年 n<30 笔 → 该年 calmar 记 0.0——保守：不奖励信号稀疏年，逃不过差年）。
   既有字段（ann/calmar/...）保留为整段值，兼容 feasibility_gate 等既有消费者。
3. **排序目标改 `min(yearly_calmar.values())`**。落点：run_search/排序消费 inner score 处单点切换，
   **实施裁定（2026-08-15 评审后）**：原设想的 `score_key` 参数开关未实现——回退经
   「键缺失回退 calmar」实现（`get(min_yearly_calmar, get(calmar))`），且 evaluate 恒
   注入该键使回退在生产不可达；加显式开关即造死代码（YAGNI），记偏差不补参数。
   TPE 目标函数同源（信息隔离红线不动：outer 仍只进报告）。
4. **per-eval 成本预算**：窗口 2025+→2021+ 数据量 ×3，P1 后 35.5s/组 → 预估 ~110s/组；
   P2 后 4h 夜 × 12 worker ≈ 1500+ 组/夜（对比旧串行 80 组），可接受。diag 冒烟实测后
   若 >180s/组再议收窗。
5. **engine_hash 影响**：objective/split 属 ENGINE_FILES → hash 再变 → 与 P1-1 重搜基线
   同批次（本就今晚新基线，无额外代价）。

### 1.3 验收门

- 单测：合成两年数据（一好一差）→ min_yearly_calmar = 差年值；n<30 年份记 0；
  旧行为回退参数生效。
- 同参对比：25c602 参数在新目标下重跑 inner——预期 min(2021-2024) 显著低于旧 inner
  calmar 44.87（熊市年拖累）——作为「新目标确有判别力」的实证。
- daemon 夜跑 smoke：新基线首夜 trial 落库 score 字段 = min_yearly_calmar。

**验收结果（2026-08-14 实录，diag/a2_mincalmar_probe.py）**：
- 同参对比 PASS：25c602 在扩展口径下各年 calmar = {2021: 118.43, 2022: 0.0,
  2023: 0.0, 2024: **-1.106**}，min_yearly_calmar = **-1.106**（vs 旧口径整段
  44.87）——判别力拉满；2022/2023 触发 n<30 记 0（熊市年信号稀疏本身即不适配
  证据），min 实际由 2024 结构牛年的 -1.106 决定。
- 耗时锚：freeze 11.7s + eval **108.7s/组**（1193 标的）——estimate_budget 的
  110s 预估命中，无需校正。
- freeze WARN 4 只尾部陈旧标的：16371 段 repair 收敛中的正常痕迹，非阻断。

## 2. A1：市场状态闸产品化（熊市停手）

### 2.1 现状与落点

- 基准：`backtest/tools/market_regime_filter.py`（诊断脚本，生产零接入）；
  `judging.py:13` L0 闸无熊市否决（A2 治搜索侧，本工单治**执行侧**）。
- 治疗目标：产品化 regime 闸，接入 `engine._eod` 选股前置 + plan 下单前置
  （治理 spec §3 A1 原文），让 live 在空头环境停手。

### 2.2 设计裁定

1. **正式模块 `trading/compute/regime.py`**（从诊断脚本产品化，纯函数 + 缓存）：
   - 判据（DG-G4 定稿）：**沪深300 收盘 > MA200 ∧ 市场宽度 > 0.5** → `BULL`（可交易）；
     任一不满足 → `BEAR`（停手）。宽度 = a_shares_daily 全市场 close>MA200 占比。
   - 数据源：`index_daily.parquet`（000300.SH，18:00 增量已恢复）+ `a_shares_daily.parquet`。
   - 阈值（MA200 / 宽度 0.5）**模块常量固定，绝不进 TPE**（DG-G4 红线）。
   - 降级语义：数据缺失/长度不足（MA200 需 200 根）→ 返回 `UNKNOWN` + reason——
     **fail-closed：UNKNOWN 等同 BEAR 停手**（G 波 DG-G3 同哲学：缺信息时收紧）。
2. **接入点 ①（eod 前置）**：`_pipeline_then_eod` 事件链在 `engine._eod()` 前调
   `regime.classify()`；BEAR/UNKNOWN → 跳过选股产计划（落空计划 + 钉钉播报
   「regime 停手：reason」），post_close/pre_open 对空计划天然 no-op。
3. **接入点 ②（pre_open 前置）**：`_pre_open_gate` 增第 ④ 段 regime 复核（防 eod 后
   隔夜转空；读取与 eod 同一 classify 单源）。BEAR/UNKNOWN → skip + 台账记录。
4. **不碰存量持仓**：regime 停手只断新单（选股+挂单）；已有持仓的止损/止盈照常
   （stop_loss 链路不接 regime——退出永远允许，A1 只管进场）。Why：停手 ≠ 清仓，
   清仓决策属人审范畴（避免闸门越权变相变成自动清仓策略）。
5. **可观测**：classify 结果与 reason 写 eod 产物 JSON + 钉钉播报；`/health` 或
   status 端点暴露当前 regime 态（观测面）。

### 2.3 验收门

- 单测：合成 HS300/宽度序列三态（BULL/BEAR/UNKNOWN fail-closed）；阈值边界
  （MA200 恰等/宽度恰 0.5）。
- 集成：eod 前置 BEAR → 无计划落盘 + 播报文案；pre_open ④ 段 BEAR → skip 台账。
- 实证：用 2022-01~2022-10（熊市段）数据跑 classify → 全段 BEAR（判据有效性的
  历史回放验证）。

## 3. 执行顺序与分支

1. **A2 先**（今晚 02:00 daemon 前）：objective/split 改造 + 测试 + diag 冒烟。
2. **A1 随后**（同夜或次日盘前 9:22 前——pre_open 前置生效即保护 T+1 挂单）。
3. 分支 `opt/a1-a2-regime`，独立 commit 可 revert；合入 master 须等价守卫/全量测试绿。
4. 与后台 repair（16371 段收敛中）无冲突（A2 改代码，repair 写数据湖）。

## 4. 风险与对抗推演

| 风险 | 缓解 |
|---|---|
| 扩窗后 per-eval >180s（夜预算折半） | diag 冒烟实测；超限则收窗 2022+（保熊市考场的最小扩窗） |
| min-calmar 让所有参数都难看（无冠军可 promote） | 这正是诚实信号：宁可空缺不推 2025 特化参数；feasibility 门槛不变 |
| regime 阈值 0.5 宽度线武断 | DG-G4 已定「固定经验值不进 TPE」；回放 2022 段验证有效性后入 ADR，后续人审可调常量 |
| UNKNOWN fail-closed 误伤（数据延迟日） | 停手只影响当日新单；播报 reason 含数据缺失细节，人工 repair 后次日恢复 |
| yearly 分块在 inner 边界年（2021 首年）warmup 短 | n<30 记 0 保守处理（不剔除——剔除=逃考） |
