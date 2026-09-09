# 研究摘要 2026-09-09

## 实盘表现（fill 去重归因）
- 实盘成交：0 笔
- 胜率：—
- 均 rr：—
- 已实现盈亏（TP 平仓，金额）：—（0 笔）
- 平仓事件：311 笔
- 事件计数：跳过 — / 同日竞争 — / 跳空止损 —

## 回测期望
- 回测期望（task 6d90f498，窗口 2026-06-04~2026-09-02）（ACTIVE 参数 ✓）：
- 期望：成交 12661 笔 / 胜率 53.8% / 均 rr 0.05

## 漂移对比
- 状态：**样本不足**

### 逐笔归因（n=12661）

**按出场原因**
| 分组 | n | 胜率 | 均笔% | 累计% |
|---|---|---|---|---|
| stop_loss | 3885 | 0% | -11.85 | -46051.3 |
| timeout | 4876 | 60% | +0.64 | +3133.5 |
| tp2 | 3900 | 100% | +10.78 | +42025.5 |

**按月度**
| 分组 | n | 胜率 | 均笔% | 累计% |
|---|---|---|---|---|
| 2026-06 | 2066 | 44% | -1.67 | -3444.7 |
| 2026-07 | 3557 | 44% | -2.63 | -9349.7 |
| 2026-08 | 6572 | 62% | +1.77 | +11629.1 |
| 2026-09 | 466 | 60% | +0.59 | +272.8 |

## 数据与实验状态
- 数据版本（data_hash）：—
- 当前生效实验：20260826

## 参数探索（discovery 低功率）
- 已评估 trial：814
- 最新 run：bb3c0f47，本轮 85 组，frontier=9，k=0
- 新冠军：calmar=13.64 outer ann=35.9%

## 委员会评审台（质证工序 · 近 2 日）
- plan_preview_2026-09-10 [Tier A] **NOTES**（9 次调用）｜KB冲突: 无重提七波否决形状（2.5ATR帽/skip@3.0/T1 均为 KB 实证终态，rr 维度不在否决面内）；deep 定
- plan_preview_2026-09-09 [Tier A] **NOTES**（10 次调用）｜KB冲突: 无硬冲突；三处标注：①deep=负期望系口径压缩（throttle_design 09-06分化证据：deep双峰202
- plan_preview_2026-09-08 [Tier A] **NOTES**（10 次调用）｜KB冲突: T1两区冻结版在deep停新仓 vs throttle_design三区DRAFT只帽缠线带——已登记未决分歧，DRAF
- plan_preview_2026-09-08 [Tier B] **NOTES**（3 次调用）｜KB冲突: T1 二元闸在 deep 区停新仓，与 throttle_design 三区版 DRAFT（仅缠线带停、deep 保留抄
- opt0908_r8_exit_grid [Tier A] **NOTES**（13 次调用）｜KB冲突: 无同形状否决:1.75纯键在rejected_proposals(8案全捆绑)无先例,不触发T3.3重提驳回;但报告基线
- opt0908_r7_pertrade_grid [Tier A] **NOTES**（10 次调用）｜KB冲突: 六年分年全正判据与 regime_dependence/above_bull_proxy（指数阴年全负、净期望 77.6
- opt0908_r6b_limit_model [Tier A] **NOTES**（12 次调用）｜KB冲突: 无方向冲突（plm 保守方向、非七波/R10 同型）；缺口：R6 NOT READY 判定与中间态长扫教训未入档，随采纳
- opt0908_r6_pool_expansion [Tier A] **PASS**（11 次调用）｜干净带 +0.73% 在本席语料上分毫不差地复现，是报告最强外部证据；拒绝性裁决零参数风险故 PASS——三项强制条件：
- opt0908_r5_validation [Tier A] **NOTES**（11 次调用）｜设计强度过中评级风险关但 OOS 为零、平台仅两点——附条件放行：fresh_window 成熟复核+排名6-7边际笔监
- opt0908_r4_final [Tier A] **NOTES**（11 次调用）｜KB冲突: 无新冲突：throttle_design 含 R1 终弃+R4 重走记录，cooldown_semantics 含 R4
- opt0908_r3_validation [Tier A] **NOTES**（11 次调用）｜KB冲突: r10_veto: cd2格outer ann负让渡39.1vs46.1且outer dd/calmar未报——报告自认
- opt0908_r2_validation [Tier A] **NOTES**（11 次调用）｜KB冲突: 无硬冲突（cooldown 不在 rejected_proposals 库，cd2 非重提死方向）