# 信号质量深度探索 P0→P3 收官报告（R7 · 2026-08-26）

> 方案：`docs/superpowers/plans/2026-08-26-signal-quality-deep-dive.md`（唯一设计真相源）。
> 本文是 P0-P2 执行结果 + P3 裁决与交接。一切读数口径：B3 ACTIVE
> （neckline_r610_b3_20260826）× freeze(2021-01-01) 今日截面 × 冻结 PM
> （100 万 / 4 并发 / 7.5% / 整手+min5 / freeze_pending）。

## 结论一览

| Phase | 交付 | 裁决 |
|---|---|---|
| P0 特征库 | `logs/quality/trades_features.parquet` **101,318 行**（主池 64,532 + wf 四折 OOS 36,786），五族 41 列 + at_signal/at_fill/env 可用性标签 | 锚验证过：同湖对拍原脚本 445 taken/+400.7%/76.0% |
| P1 判别力普查 | 22 单特征 + 10 交互，四闸（Bonferroni p<1.35e-03 ∧ \|ΔQ5−Q1\|≥0.5pp ∧ ≥4/5 年同向 ∧ wf2022+2024 双折同向） | **存活 2：bottom_disp（底部离散度）、vol5_slope（5 日量能斜率）**，双双「分层候选」（过滤负增益） |
| P2 分层受控 A/B | 预登记五闸（`diag/quality_p2_layering.py` 写死先于看数） | **VETO（五闸四否，等权/IC 加权双型一致）**——线性映射 [0.03,0.10] 平均敞口 0.064 vs 0.075 的机械劣势主导 |
| P3 双轨实测 | —— | **不启动**（未过闸）。分歧台账 + 下一假设登记见下 |

## P0 · 特征库与基线画像

- 产物：`diag/quality_p0_features.py`（逐符号扫描 + 事件四元组捕获 + 五族富化）、
  `diag/quality_p0_profile.py` → `logs/quality/baseline_profile.md`、
  `logs/quality/features_meta.json`。
- **锚验证与湖漂移事件（重要运维知识）**：r6_11c 今晨 442/+399.8%/75.8% 在当前湖
  不可复现——18:00 EOD 同步使湖尾 08-25→08-26 + qfq 前进。决定性证伪 = 原样重跑
  `diag/r6_11c_freeze.py`（覆写了该 json，今晨值留档于 r6_11d json 与 features_meta）：
  当前湖 445 taken/+400.7%/76.0%。本管线同湖对拍外层笔集逐位一致（11520/445/0.760）
  ——**+400% 对照线口径不变，读数随湖版本微幅前进**。教训：跨 EOD 同步时刻的基线
  对拍必须同湖重跑原脚本，不能拿旧 json 当锚。
- 画像要点（描述性）：出场结构 tp2(+10.3%)×54% / stop(−10.4%)×40% / timeout(−1.0%)×6%；
  年段 2022 −1.47pp/2023 −0.49pp（信号口径年均值）vs 2024 +3.04/2025 +2.77/2026 +1.07；
  **rr_id≡h_atr（ρ=1.0，tp_h_mult=stop_atr_mult 参数退化）**；拥挤日（同日>80，占 40%）
  均值 +2.10pp 反而更高（突破潮=rally 年 regime 效应——只标注）。
- **可用性设计（P2 红线）**：分层分只允许 at_signal 特征（signal_date 收盘可知）；
  rr_exec/entry_depth_atr/wait_days/is_chase 成交后才可知，仅归因；环境族 ADR-16 只标注。

## P1 · 存活 2 个，最大 IC 者死于年段硬闸

- 产物：`diag/quality_p1_discriminance.py` → `logs/quality/discriminance_report.md` +
  `survivors.json`。预登记四闸写死在脚本，Bonferroni 无条件执行（n_tests=37，严于
  任务 >50 才收紧的红线）。
- **bottom_disp**：IC +0.0165，ΔQ5−Q1 +0.72pp，年段 4/5（2023 翻转 −0.55），
  wf 折 +0.66/+1.69 双正。
- **vol5_slope**：IC +0.0231，ΔQ5−Q1 +0.79pp，年段 4/5（2025 翻转 −0.44），
  wf 折 +0.36/+1.18 双正。
- 被硬闸正法的重案：**same_day_n（IC +0.0624、Δ +1.87pp 全场最强）年段仅 2/5**——
  拥挤红利是 rally 年特化，年段一致性硬闸的价值实证；suppression（IC +0.036）3/5；
  pattern_days 4/5 但 wf2022 折 −0.32 翻转死于 G3；h_atr Δ 仅 0.38pp 量级不足
  （R4 深形态死方向仍在但被 max_h_atr=5.5 闸削平）；交互 10 对全灭。
- 环境族：pool_mom20 IC +0.058（p=2.7e-49）全场最强——ADR-16 只标注不过滤。
- 过滤测试（判别力≠可治疗性分类）：两存活特征剔底 30% 后外层 ann **负增益**
  （−0.21/−0.08）→ 双双记「分层候选」——与三案（H1 动量闸/R6-2 交互/L1 时间止损）
  同向：**该策略族在组合约束下丢信号必丢收益**。

## P2 · 中心假设受控否决（VETO）+ 机理拆解

- 工程：`backtest/models.py` 加 `quality_alloc`（默认关零回归）+ `return_taken`；
  `discovery/objective.py` portfolio_metrics 透传 `pos_cap` 键（惰性）。守护测试
  +4（手推净值钉死），models/objective/replay_stats 49 绿。
- 预登记五闸结果（等权主型；IC 加权副型更差）：
  - ① 外层 ann：固定 +402.0% vs 质量 +348.3%（**Δ−53.7pp**）❌
  - ② 逐年段：五年全负（−1.6 ~ −35.5pp）❌
  - ③ wf2022 折：+109.0% vs +107.0%（Δ−2.0pp）❌
  - ④ 滑点 25bps：Δ−47.3pp ❌
  - ⑤ 拥挤日不降级：✅（两臂 taken 集合同构——冻结闸按占用序定 taken，与仓位大小无关）
- **机理拆解（事后敏感性，非闸非采纳）**：均值匹配映射 0.075+0.07×(p−0.5)
  clip[0.03,0.10]（平均敞口 0.073≈固定臂）下：外层 Δ**+8.8pp**，逐年
  **五年全正**（+3.1/+9.9/+9.1/+0.5/+8.8pp）。即：**否决的大头是任务规格线性映射
  的平均敞口机械劣势（0.064 vs 0.075），倾斜本身方向正确但量级小**（两特征
  IC≈0.02 级，单笔 Q5−Q1 差 0.7-0.8pp，撑不起 ±54% 的仓位重分配产生大增益）。

## P3 · 裁决：不启动（未过闸）——分歧台账与下一假设

**不硬上模拟盘**（任务书红线）。既有双轨（QMT 主腿 + 掘金对照腿，固定 7.5%）
照常积累数据，不因本轮改动任何实盘路径（quality_alloc 默认关、ACTIVE 参数未动、
trading/ 零改动）。

### 分歧台账（登记，编号 D-*）

- **D1 映射形状 vs 倾斜方向**：预登记线性映射 [0.03,0.10] 否决的主因是平均敞口
  −15%，非质量信号无信息。均值匹配下五年全正但幅度小（外层 +8.8pp / +400% 基线
  的 ~2% 相对）。**这是「已偷看」的观察**——同数据重跑不构成新证据。
- **D2 质量信号上限**：P1 全库最强 at_signal IC 仅 0.023（vol5_slope）——54k 笔
  流水里「参数不可见」的微观结构信息量本身就薄；same_day_n（0.062）与环境族
  pool_mom20（0.058）更强但前者年段不稳、后者 ADR-16 禁入决策。质量分层的天花板
  可能就在 +10pp 量级，不足以撼动 +400% 读数的量级结构。
- **D3 组合约束下 taken 集与仓位大小解耦**：闸⑤实证两臂 taken 完全同构——冻结
  资金闸下进净值与否由占用顺序决定，质量分改变的是「进场的笔吃多少」而非「哪笔
  进场」。分层的全部作用面=仓位加权，无选股面。
- **D4 拥挤日红利是 regime**：same_day_n>80 均值 +2.10 vs 41-80 桶 +0.56——
  突破潮与反弹期耦合，年段 2/5 翻转。与 ADR-16 池子侧结论同族（只标注）。

### 下一假设登记（H-R7a，需新证据窗口）

- **假设**：均值匹配质量映射（0.075±0.035 clip）在冻结口径下逐年段非负、外层
  增益 >0 且 |Δ|>噪声带。确认窗口：**新数据 ≥20 交易日**（2026-09 下旬起）或
  双轨实盘质量分对照（若届时用户裁决上线对照腿）。
- 预登记闸（届时写死先于看数）：外层 ann↑ ∧ 逐年段 ≥−2pp ∧ 摊入 min_fee/整手
  摩擦 ∧ taken 平均 pos_cap ∈ [0.070,0.080]（防再犯敞口机械差）。
- 数据资产已就位：`trades_features.parquet`（增量续扫脚本 `quality_p0_features.py`
  可直接重跑新湖）；`survivors.json`；P2 A/B 机（`quality_p2_layering.py` 改
  POS 映射一行即试新映射）。

### 交接清单（下一 session）

1. 湖已前进（尾 2026-08-26）：任何基线对拍先同湖重跑原脚本（P0 教训）；
2. `logs/r6_11c_freeze.json` 已被同湖重跑覆写（445/+400.7%/0.760），今晨值在
   `r6_11d_maxwait.json` 与 `features_meta.json` 留档；
3. H-R7a 确认窗口 2026-09 下旬；届时增量重扫 → 新窗口五年段 + 外层分段重估；
4. 双轨数据自动积累不受影响；掘金 schedule 验证点（次日 09:31/10:31）在
   R6-9 主线，与本轮无关。

## 复现

```bash
PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p0_features.py   # ~22min（含 4 折）
PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p0_profile.py
PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p1_discriminance.py
PYTHONIOENCODING=utf-8 ./.venv310/Scripts/python.exe -u diag/quality_p2_layering.py
```
