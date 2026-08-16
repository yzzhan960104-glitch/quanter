# R0-A3 报告：成交真实性/滑点敏感性实证（2026-08-16）

> 脚本 `diag/a3_fill_realism_probe.py`（可复现）；engine_hash `53090190e55f`；snapshot `3fdcbbcb2e3160e1`（universe 1193，lake_start 2021-01-01，holdout inner 2025 / outer 2026 embargo=5）；原始数字 `logs/a3_fill_realism_results.json`。

## 一、结论（本轮最大金块）

1. **两参数集在 replay 引擎口径（实盘同源）outer 段 0bps 滑点即为负**——ACTIVE -23.9%、DRAFT 47d350 -44.5%。滑点敏感性已退居次要：**问题不是摩擦成本吃掉边缘，是组合约束口径下本无正边缘**。
2. **口径裂缝实锤（符号反转）**：ACTIVE 实验 note「outer ann=+18.4%」是 discovery scan 口径（`kelly_metrics` 假设所有信号独立可下注、无并发约束、逐笔 5% 复利至年化）；replay 组合口径（`PositionModel` max_positions=6、资金/现金约束）给 **-23.9%**。+18.4% vs -23.9%、+53.4% vs -44.5%——**搜索目标与实盘口径的裂缝是当前系统最大单点失真**。
3. **执行假设侧写**（ACTIVE outer 2026）：stop_gap=43/611 笔（**7% 的止损单发生在跳空环境**，目标价完美成交假设高估了这 7% 的出场价）；same_day_both=0（无同日双向）；n_skipped=455（611 成交外 455 信号被跳过——组合约束吃掉 43% 的信号流）。
4. **唯一正边缘证据链**：提案 p_553e383d（收紧 min_rr 2.0→1.5、max_h_atr 5.0→2.5，信号从 518→67）replay 口径 outer **+5.5% / dd -1.4%**——「少而精」信号在组合约束下反而活。这为 R1 指了方向：**质量优先于数量**。

## 二、衰减曲线（replay 口径 annualized_return）

| 滑点 | ACTIVE inner | ACTIVE outer | DRAFT inner | DRAFT outer |
|---|---|---|---|---|
| 0bps | -3.5% | **-23.9%** | +2.7% | **-44.5%** |
| 5bps | -4.4% | -25.2% | +1.4% | -46.0% |
| 10bps | -5.3% | -26.5% | +0.1% | -47.6% |
| 20bps | -7.1% | -29.0% | -2.4% | -50.7% |
| 50bps | -12.5% | -36.4% | -10.0% | -59.4% |

- 盈亏平衡滑点：ACTIVE inner/outer 均 ≈0bps（本就负）；DRAFT inner ≈**10.5bps**（唯一正段）。
- 斜率健康度：每 10bps 滑点侵蚀 ann 约 0.6-1.7pp——**摩擦敏感度本身温和**，再次印证主问题是口径/组合约束而非交易成本。
- 胜率（信号 rr 口径）全程不变（42.5%/30.1% 与 46.7%/35.1%）——滑点只作用 equity，识别质量不受影响，符合 PositionModel 语义。

## 三、判定

- 判据「outer ann@10bps ≥ ann@0bps×50% 且 @20bps>0」：**双参数集均 FAIL（薄边缘→实为无边缘）**。
- **G7 常量 `A3_SURVIVES_10BPS = False` 已回填** → autopromote 在出现 replay 口径正边缘候选并重跑本探针翻绿之前，整体 fail-closed（任何候选不可自动 promote）。这是 R0 的诚实产出：**当前没有任何参数集配得上自动上实盘权重**。

## 四、对 R1 的指向（下一步假设）

1. **主攻：搜索目标口径切换**——inner 快筛从 scan 口径切到 replay 口径（`evaluate_replay` 或轻量 replay 代理），让搜索直接优化组合约束下的真实目标。成本：单组评估从 35.5s 涨到分钟级——先在「快筛用 replay 单段+抽样 universe」上找平衡。
2. **信号质量线**：p_553e383d（3e383d DRAFT）作为 R1 首个工作基线——它有唯一正 outer 证据链；围绕它的邻域（min_rr/max_h_atr 收紧方向）做受控探索。
3. **止损跳空建模**（7% 笔数）：stop_gap 场景按 open 而非 stop 价成交的保守化已在 simulate_exit 部分覆盖（backtest.py:276 注释），replay 引擎侧的逐笔 audit 待 R1 深挖。

## 五、诚实边界

- 2026 外样本含 BEAR 段（A1 regime 闸实盘会停新单，回测不停）——replay 口径 outer 负值部分反映「无 regime 闸的裸策略」在熊段的表现；但 scan 口径同样不含闸，口径对比结论不受影响。
- n=611/763 笔样本量充分；universe 1193 含 4 只真实停牌陈旧标的（freeze 警告，C5 已核实为停牌真值非缺口）。
- 只测了滑点单一摩擦轴；印花/佣金已内含在 avg_pnl_pct（万三+0.05% 卖出），未测冲击成本的规模效应（仓位 5% 单笔对创板科创流动性深的标的影响小，属合理近似）。
