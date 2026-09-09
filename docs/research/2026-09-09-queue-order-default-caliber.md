# queue_order 默认口径（exit_date 先知序）提案材料 · 2026-09-09

> 状态：**待提案流人审**——本文只呈证据与选项，不改默认。触发：外部全项目评审
> 将其列为 P0（「回测默认资金调度口径借未来函数」），逐条实测后确认代码事实
> 成立、但定性（"不可采信"）与修复建议（直接改默认）需人审裁决。

## 一、代码事实（实测确认）

1. `backtest/models.py:63-71`：`queue_order` 默认 `"exit_date"`——同占用日候选按
   **最早出场日**排进场优先级 = 用未来信息做最短作业优先调度。注释自述「历史
   先知口径……外层 +402% vs 可部署族 −5%~+20%，历史全部读数与 A/B 的连续性锚，
   仅内部用」。
2. `backtest/worker.py:139-143`：replay 任务不传 `position_model` → 走默认
   `PositionModel()` → **web 回测队列、周度播报引用的净值/年化/回撤全部是
   oracle 口径**。
3. `research/autopromote.py` G2/G3 走 `evaluate_replay` → 同默认 → **晋升证据
   也是 oracle 口径**（09-06 oracle 污染审计已知，r68 vs B3「晋升证据虚高」
   不翻案的裁决即基于此认知）。
4. 可部署口径基建已存在：`queue_order="random"`（多种子中位数）+
   `portfolio_metrics_dual` 双口径并报（R7c 2026-08-26 用户裁决「双口径并报」）。

## 二、为什么当初默认 exit_date（不能只当缺陷看）

- **历史连续性锚**：全部历史读数、A/B 对照、KB 里的数字都在此口径上。改默认
  = 一刀切断与六个月实证库的可比性（golden 钉死、回归对照全要重锚）。
- **确定性**：exit_date 序确定可复现；random 单序方差大（升序 −4.7% vs 降序
  +20% 外层实测），必须多种子中位才有代表性——单任务默认 random 反而更易误读。
- **已知 lore**：verify 闸 oracle 口径对组合结构类候选盲视（ts8、cd2 两例实锤）、
  oracle 吞吐效应（部署序中位全期 +4.9% vs oracle +58% 差 12 倍）。这些结论
  的前提就是「研究线用 oracle、部署线另测」的双轨制。

## 三、选项

| 选项 | 内容 | 代价 |
|---|---|---|
| A 维持现状 | 默认不变；播报/报告页引用数时强制双标（oracle/部署双列） | 零回归；依赖纪律（已两次被外部评审打脸） |
| B 默认切 random | `PositionModel.queue_order` 默认改 random（多种子），exit_date 更名 oracle 并要求显式传入 | 断历史锚；所有 golden/对照需重锚；单任务多种子成本↑ |
| C 出口双列 | 默认不动，`ReplayReport` 增设 `annualized_return_deploy`（random 中位）字段，web/播报强制双列呈现 | 中改；历史读数不动，新读数自带双口径 |

## 四、建议（供人审）

- **不建议热修改默认**（选项 B 的断锚代价 > 收益，且评审自己的「修复」也没给
  出断锚迁移方案）。
- 倾向 **A+C 组合**：口径语义靠出关口的双列呈现固化，而非靠默认值翻转。落点
  清单：① `backtest/replay.py` 年化计算处（顺带修 P1-6：pos_cap 加总净值配
  `^(252/n)` 复利年化的几何放大——加总口径应线性年化或双列标注）；② web
  replay 报告页；③ brief 周报引用数处；④ autopromote G2/G3 证据栏。
- 本议题与「verify 闸口径升级」提案（0908 会战待人审项）同族，建议同批审。

## 五、关联

- 外部评审报告裁定全文见会话记录；`models.py` R7c 注释；09-06 oracle 审计
  （`docs/research/2026-09-06-oracle-contamination-audit.md`）；
  0908 会战两大元发现之一「verify 闸 oracle 口径对组合结构类盲视」。
