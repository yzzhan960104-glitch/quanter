> 最近复核：2026-08-08 · 维护者：wayfinder-session ·
> 权威归宿：**数据模型 ER 图**（α 可视补集）。**prose 规则 / 域表 / 护栏 / 巡检权威在 [`docs/data-source-of-truth.md`](../data-source-of-truth.md)**——本图只补它缺的 ER 可视化，不重抄规则（[T0 Decisions](../../plans/wayfinder/T0.md) (c) α）。

# #7 SSoT 数据层（9 域 ER 图）

[`data-source-of-truth.md`](../data-source-of-truth.md) 列了 9 个数据域的真相源（prose），本视图给出它们的**实体关系图**（classDiagram）——这是该 prose 文档唯一缺的可视化。每条规则的权威解释仍归 prose，本图链而不抄。

## ER 图（9 域 + DB 归宿 + 键）

```mermaid
classDiagram
  class fill {
    UNIQUE(order_id, traded_time)
    symbol / direction / price / qty
    ★第1域·成交流水幂等
  }
  class order {
    PK: order_id
    state: SUBMITTED/FILLED/CANCELED
    ★第2域
  }
  class position {
    symbol / qty
    strategy + entry_rationale
    current_stop
    ★第3域·含归因
  }
  class trade_event {
    UNIQUE(account_id, trade_id, action)
    action: SIGNAL→CONFIRMED→VETOED
      →ORDERED→SUBMITTED→REJECTED
      →DRY_RUN→FILLED→CLOSED
    meta: JSON
    ★第4域·生命周期
  }
  class account_daily {
    PK(account_id, date)
    start_total_asset  ~ C-1 熔断基线
    close_total_asset  ~ 日终闭合
    ★第5域
  }
  class plan {
    当前: plan_date.json 文件
    Phase C: trade_event(SIGNAL).meta
    ★第6域·过渡态
  }
  class data_ready {
    PK(date, dataset)
    get_ready 单口读
    ★第7域
  }
  class job_ledger {
    brief_bot begin/finish 成对
    ★第8域·播报幂等
  }
  class experiment_active {
    ACTIVE 表
    ★第9域·参数迭代
  }

  fill }o--|| order : order_id
  trade_event }o--|| order : trade_id
  position ..> fill : post_close reconcile 累加(±)
  account_daily ..> trade_event : 日终闭合校验
  plan ..> trade_event : "Phase C 升格 SIGNAL.meta"
  data_ready ..> job_ledger : 就绪+播报双守
```

## DB 归宿映射（3 库 + 1 过渡文件）

| 真相源 | DB / 文件 | 域 |
|---|---|---|
| `state_store.fill` / `order` / `position` / `trade_event` / `account_daily` / `data_ready` | `logs/trading_state.db` | 1-5, 7 |
| `job_ledger` | `logs/trading_job_run.db`（**独立库**，不与 trading_state 混） | 7, 8 |
| `experiment.ACTIVE` | `experiment/experiments.db` | 9 |
| `plan_<date>.json` | `logs/trading_plans/`（过渡文件，Phase C 删 `save_plan/load_plan` 后降级为按需导出） | 6 |

## 防回归守卫（细节见 prose）

- **静态护栏** `tests/test_ssot_static_guard.py`：BANNED pattern 精确正则（跳注释），CI 时炸。
- **运维巡检** `scripts/audit_ssot.py`：5 项检查（fill↔position / account_daily 闭合 / trade_event 孤儿 / engine 进程数 ≤1 / BANNED 同口径），任一 FAIL exit 1。
- **BANNED 模式**：读 `live_trades.csv` / `param_iter_state.json` 回退、写 `live_trades.csv` 等（已删，命中即 FAIL）。

> 规则全文、Phase A/B 治理历史、遗留风险 → [`docs/data-source-of-truth.md`](../data-source-of-truth.md)。
