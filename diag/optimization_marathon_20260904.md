# 优化马拉松总报告（2026-09-04 01:45-03:30 · 4 批并行+3 模拟+1 提案+1 流程修复）

## 产出一：time_stop_days=8 提案已 APPROVED → DRAFT（neckline_prop_20260904_faf2e4）

**证据链**：持有期画像（16-30 日组 26% 笔数均笔仅 1.56%=收益悬崖；1-3 日组
5.54%/87.4% 胜率=主结构）→ inner +9.8pp（240.1→249.9）→ outer 零损
（558.0 持平）→ **六年六窗稳健性**（2021 +2.7 / 2022 -3.6 / 2023 +2.6 且
均rr 负转正 / 2024 +3.0 且 dd -10.8→-9.1 / 2025 +9.8 / 2026 0）——
4正1平1微负、dd 两改善零恶化。**保险型参数**：砍「磨到死」持仓换周转，
快速兑现主结构零影响；胜率 -6pp 是代价（强平部分本会 TP2 的仓）。
promote 走 autopromote 七门/人审，红线不破。

## 产出二：提案流 verify 口径缺陷修复（13 连拒冤案的根源）

**bug**：verify_proposal 拿 partial params（1-3 键）直接跑回测=
「NecklineConfig **默认参数**为底」，而基线是 ACTIVE 冠军全参数——
不对称比较。ts8 提案实证：默认底 inner 胜率/rr 全崩（REJECTED 冤案）→
对称化（冠军+diff vs 冠军）后同提案 **APPROVED**。
08-21 起连续 13 拒大概率多为口径冤案（此前归因于「提案质量」的结论需重估）。
修复：一行（{**baseline_params, **proposal}）+ 测试隔离补丁。

## 产出三：否定性知识（同样值钱）

| 方向 | inner | outer | 裁决 |
|---|---|---|---|
| 溢价分层过滤 | ≤基线 | ≤基线 | 否决（昨） |
| 量能/动量过滤 | 降 | — | 否决（量比平坦+倒U） |
| **动量区间 sizing 加权** | 全降（216-233%） | — | **否决**：低动量组均笔+3.54% 仍为正，减仓即损失 |
| min_rr 2.25 | +6.4pp | **-61pp** | 翻转否决 |
| min_suppression 0.35 | +7.7 | -8.2 | 不采 |
| decay_tau 30 | **+11.4** | -3.9 | 增益不兑现，不采 |
| c2 组合(decay30+ts8) | +22.6 | -3.9 | 组合增益不兑现（ts8 单独已足） |
| trailing grace5/step/max_holding | 微/平/平 | 平/平/— | 不采（grace5 被 ts8 掩盖） |

**元结论**（三天四次实锤）：inner 2025 的参数敏感度在外样本普遍不兑现；
「外样本零损+跨年稳健」是唯一敢上线的标准——ts8 是全因子面唯一通过者。

## 全景附：扫描基础设施
并行批扫（ProcessPoolExecutor 3 并发，7-8 格/~5min）+逐笔重放模拟
（build_equity_curve 同款资金模型）——本轮共 ~40 格回测+3 组画像。
脚本：diag/batch1_exec_sweep|batch2_id_sweep|batch2b_exec_fix|batch3_combo|
batch3b_outer|batch3c_c2outer|batch4_yearly|sizing_weight_sim.py
