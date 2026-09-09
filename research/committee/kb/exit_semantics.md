# 出场结构参数语义(09-08 会战 R1 实测定谳)

- **tp1_portion 在 tp1>tp2 配置下不是死参数**:priority 3 分支不可达(触 tp1
  必先触 tp2 全平)≠ 参数无效——simulate_exit 在 tp2 全平日对 lot1 记账价 =
  当日 high≥tp1 ? tp1 : tp2(R6-4 路径),avg_pnl 的 lot1/lot2 加权比 =
  tp1_portion。真实语义=「强势日(tp2 日摸到 2H)高价档的仓位分配」。
  实测:0.9 档全面优于 0.5 档(纯收益栈 ann 22.6 vs 18.7/dd -28.9 vs -34.0,
  实盘口径)。exit_reason 标签零 tp1 兑现=标签归因 priority 2,非配置无效。
- **grace7 实盘口径不复现**:R6-7 oracle 口径「+1.4pp 改善」在 keep-top5+
  21 种子口径下 ann ±0.6pp 但 dd 恶化 2.3-2.4pp——bb1363 DRAFT 疑口径
  artifact,待人审降级/撤回。
- **教训(元)**:源码优先级链分析只覆盖决策层(decide_exit),记账层
  (simulate_exit 的成交价加权)是另一个生效面——「参数是否死」必须重扫
  实证,不能只读分支可达性。
