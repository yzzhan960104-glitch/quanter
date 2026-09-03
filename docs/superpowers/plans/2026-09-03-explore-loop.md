# 归因驱动全自动参数优化探索闭环（attribution-driven exploration · 2026-09-03）

> 用户需求：亏损持仓大模型分析并提出改进意见，自动按照改进意见做参数优化探索。
> 探索实证：提案闭环（digest 18:30 链 LLM 提案→A 档自动验证→DRAFT）已每日
> 在跑但 08-21 起 13 连拒——瓶颈正是提案缺实盘亏损视野。本方案=复用组装
> +两根新线，不新写引擎。

## 一、闭环全景（每日 cron 时序）

```
18:00 pipeline_then_eod（写湖，勿撞）
18:05 ops_loser_review    亏损归因（glm-5.3 max 优先→flash 兜底）：
                          每浮亏持仓 → 主/次因+证据+风险状态三档+param_directions
18:10 ops_plan_preview    （既有）
18:30 research_digest     提案链（--proposals --verify-proposals）：
                          digest md ←【新】实盘归因段注入 → LLM 提案 →
                          verify（evaluate_replay×2+_judge）→ APPROVED→DRAFT
18:35 autopromote_daily_brief （dry-run 日报，不动）
18:45 ops_explore_loop【新】定向参数探索环：
                          归因意见→网格→逐格 inner 回测→探索报告→
                          最优格走既有提案流 create→verify→publish DRAFT
```

## 二、两根新线

### 线① 实盘归因注入提案（loser_review → generate_proposal）
- loser_review prompt 增输出 `param_directions`（研究探索方向提示，0-2 键，
  白名单 stop_atr_mult/min_rr/max_holding/buy_limit_atr_mult/breakout_vol_mult/
  momentum_gate/trailing_grace——**研究 hint 非执行建议**，红线措辞内置）。
- digest main 在 generate_proposal 前注入「实盘亏损归因」md 段
  （`render_loser_section`：每腿 top3 主因/浮亏/探索方向）。缺产物空串降级。

### 线② 定向参数探索环 research/explore_loop.py
- **意见→网格**：LLM param_directions 键频次 top2 优先（白名单守卫）；
  回落主因关键词映射（止损→stop_atr_mult／入场时机→buy_limit_atr_mult／
  系统性→min_rr／…）。每维冠军基线值×相对比率 4-5 格，基线值不重跑，
  最多 2 维（成本 ~20-40 分钟）。
- **扫描**：`evaluate_replay(params, universe, split, start/end=inner)` 逐格
  （显式 inner 单段，省一半时长）；基线同窗对比。
- **升格门槛=_judge 同款**（research.proposals 准入闸单源：inner n≥30 +
  胜率+2pp/均rr+0.05/年化+1pp 任一改善）——最优格程序化 `create_proposal`
  （note=explore_loop）→ 复用 `verify_proposal` 全窗验证 → APPROVED 才
  `publish_proposal` 成 DRAFT（weight=0）。
- **失败语义**：无归因产物/无 ACTIVE 冠军/无可映射维度 → job_run skipped
  +reason，探索报告仍落 logs/explore_{day}.json。

## 三、安全边界（三条红线，08-25「自动参数优化全停」裁决对齐）

1. **止步 DRAFT**：绝不 promote——晋升仍走 autopromote 七门 dry-run
   （AUTO_PROMOTE_ENABLED 未设=false）+ 人审 CLI。探索环零写 experiment 库
   的直接路径（只经 proposals.publish_proposal 单源）。
2. **探索面=NecklineConfig 29 键**（replay cfg_override 可覆盖面）；
   掘金腿 TRADE_CFG/R10_FILTERS/硬闸不在探索面——测不了不假装能测。
3. **无前跑面**：输入全为当日已发生数据；验证窗口 holdout inner=2025/
   outer=2026 既有口径；LLM 零新增调用（复用 18:05 归因）。

## 四、历史教训对齐

- 07-15 训练环死因（自然语言人审中间态+写端点）→ 本环无人审中间态、
  无新端点；「人」只在 promote 闸（既有）。
- R10 VETO 纪律（外样本翻转即拒）→ verify_proposal 的 outer 闸单源复用，
  不自立标准。
- 08-25 全停裁决 → 全部自动化止步 DRAFT 池，autopromote/晋升边界原样。

## 五、验证

- pytest 6 件（映射优先级/白名单守卫/回落/维度上限/基线键跳过/渲染降级）
  + 契约测试十任务；vitest 零新增（探索报告经 digest md 与既有 /research
  提案卡、backtest_queue 可视）。
- 手动首跑当日全链（归因→探索环）实测。
