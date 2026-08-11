> 最近复核：2026-08-11 · 维护者：wayfinder-session ·
> 权威归宿：**技术债 / 痛点 / god module 判定**（单一归宿）。模块结构（不判债）见 [#2](02-module-dependencies.md)；本视图是 #2 的「债务切片」——只画债-bearing 项 + 严重度，不重复依赖边。

# #6 技术债 / 已知缺口分布

路线图即技术债热力图。严重度四级（Critical / High / Medium / Low），每项链对应 wayfinder 工单（→ 治理归宿）。

## 债务热力图

```mermaid
flowchart LR
  subgraph SEV["严重度"]
    C["Critical"]:::crit
    H["High"]:::high
    M["Medium"]:::med
    L["Low"]:::low
  end

  E["✅ engine.py god module<br/>T1 完成 (2026-08-10)<br/>3437→1546 行 · 8 集群外迁<br/>_ACTIVE_ENGINE 单例桥清零"]:::done
  DI["data 完整性<br/>生产 gate 只校验实时性<br/>scan/repair 孤立 CLI<br/>历史缺口永不发现<br/>✅ L1 写入守卫已治理(T13-A)"]:::crit
  AS["account_daily.start 漏采<br/>非盘前启动→NULL<br/>熔断基线裸奔"]:::crit

  Q["broker/qmt.py 1540 行<br/>业务层堆补丁<br/>连接层不需重构"]:::high
  TB["双向耦合 trading↔broker<br/>4/3 边·回调写 DB<br/>T2 适配层缝合点"]:::high
  PC["Phase C plan 未升格<br/>plan_date.json 仍生产写入口"]:::high

  TD["双向耦合 trading↔data (3/2)"]:::med
  TP["双向耦合 trading↔presentation (2/3)"]:::med
  SS["state_store SSoT 演进半成品"]:::med
  CN["连接韧性：health_guard 无主动探针<br/>嵌套父子未探测"]:::med

  FV["前端 caisen 死视图<br/>api/caisen.ts→已下线路由"]:::low
  DOC["过时文档<br/>data_pool.md / caisen-summary"]:::low
  DC["死代码/死参（P3 follow-ups）"]:::low

  E -.->|T1 完成| T1D["✅ T1 done (2026-08-10)"]
  DI --> T13["→ T13 治本"]
  AS --> T13B["→ live P0 运维"]
  Q --> T2["→ T2 适配层"]
  TB --> T2
  PC --> T6C["→ T6 / Phase C"]
  SS --> T6["→ T6"]
  CN --> T9["→ T9/T10/T11"]
  FV --> T2B["→ 适配层顺带"]
  DOC --> DEL["→ T0 丙删（本工单）"]

  classDef crit fill:#f88,stroke:#c00,color:#400
  classDef high fill:#fc8,stroke:#a60,color:#420
  classDef med fill:#ffd,stroke:#990,color:#440
  classDef low fill:#eef,stroke:#88a,color:#335
  classDef done fill:#cfc,stroke:#090,color:#030
```

## 债务清单（按严重度）

### Critical（阻塞 live / 阻塞演进主脊柱）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| ✅ **engine.py god module —— T1 完成（2026-08-10）** | **已治理**：3437→1546 行（-55%），10 集群外迁 8 个（A critical / B data_ctx / D eod_plan / E-F-G-H phases×4 / I order_state），`_ACTIVE_ENGINE` 单例桥代码清零（仅注释/docstring 引用历史），engine 仅留调度/装配/gate/job wrapper + re-export 兼容块。终验：trading 515 单测 + e2e 长周期 26 测全绿（行为等价）。历史态内部结构 → [deep-dives/engine-current-state](deep-dives/engine-current-state.md) | ✅ [T1 done](../../plans/wayfinder/T1.md) |
| **data 完整性 gate 缺陷** | 生产 gate 只校验实时性不校验历史连续性；`scan_integrity`/`repair_gaps` 孤立 CLI 无调度；历史缺口被动跳过永不发现（[T5](../../plans/wayfinder/T5.md)）。**L1 写入守卫 + daily 双轨收口 + freshness 行数骤降已治理（T13-A · 2026-08-11，已合并 master）；L1 覆盖面 deferred：`sync_data_lake.py:158/181` + `sync_macro_credit.py:212` 三处非 daily 写入口仍裸 `to_parquet` 未接入守卫（非 T12 主路径，docstring 已声明 deferred）；L2 scan gate + L3 自动补采仍欠（T13-B）** | [T13](../../plans/wayfinder/T13.md) |
| **account_daily.start 漏采** | 模拟盘/非盘前启动 → `start_total_asset` NULL → C-1 熔断 -3% 基线裸奔 | live P0 运维（pre_open 窗口内必须起） |

### High（演进主脊柱缝合点）

| 项 | 物理事实 | 治理归宿 |
|---|---|---|
| **broker/qmt.py 业务层堆补丁（1540 行）** | 连接层不需重构（[T4](../../plans/wayfinder/T4.md) 裁定）；债在业务层（串通挂撤/拒涨停/撤单延迟的处理逻辑） | [T2](../../plans/wayfinder/T2.md) |
| **双向耦合 trading↔broker（4/3）** | engine 调 broker 下单，broker 回调 engine 写 trade_event/state_store —— T2 适配层契约核心切点 | [T2](../../plans/wayfinder/T2.md) |
| **Phase C plan 未升格** | `plan_<date>.json` 仍是生产写入口（过渡态，不违规）；Phase C 删 save_plan/load_plan 未做 | [T6](../../plans/wayfinder/T6.md) / Phase C |

### Medium（横向治理）

| 项 | 治理归宿 |
|---|---|
| 双向耦合 trading↔data (3/2)、trading↔presentation (2/3) —— T1 engine 拆分时理顺 | [T1](../../plans/wayfinder/T1.md) |
| state_store SSoT 演进半成品（Phase B+C 收口后剩余） | [T6](../../plans/wayfinder/T6.md) |
| 连接韧性：health_guard 无主动探针 watchdog / 嵌套父子进程未探测 | [T9](../../plans/wayfinder/T9.md) / [T10](../../plans/wayfinder/T10.md) / [T11](../../plans/wayfinder/T11.md) |
| **【测试卫生】全量跑隔离污染源未定位**：`data.tushare_sync` 限频器单例被先前测试替换模块属性未恢复，`test_tushare_sync_quota` 原 `patch.object(单例,acquire)` 失效。已改 patch 模块属性自隔离（2026-08-11 全量 1671 测试验证绿），但**污染源未定位**，潜在影响其它依赖全局单例的测试 | 工程债：pytest `--forked` 进程隔离 / 排查 `data.tushare_sync` 模块属性裸写入点 |

### Low（清理类）

| 项 | 治理归宿 |
|---|---|
| 前端 caisen 死视图（`presentation/web/src/api/caisen.ts` → 已下线后端路由，首页 `/caisen` 空态） | T2 适配层顺带 / 独立清理 |
| 过时文档 `data_pool.md` / `caisen-methodology-summary.md` | **本工单 T0 丙删** |
| 死代码 / 死参数（P3 follow-ups：消息重复 / pro 死参等） | 各源工单 follow-up |
| **【测试流程】风控闸变更未同步测试**：T1 删 confirm/allow_live 闸时 `test_submit_order_no_confirm` 未同步删（2026-08-11 已删）+ 时间依赖测试 `test_low_power_discovery`（已 mock 时间窗口修）。过时测试积累成「既有红」掩盖真回归（曾阻塞 T13-A 合并判断）。范畴已排查仅此一例（`_allow_live` 无其它遗留） | CI 全量绿门 + 行为变更时 grep 测试同步 |

## 非痛点（明确不在债内 — MAP Out of scope）

- `broadcast` / `config` / `discovery` / `experiment` / `ops` / `compute_unit`：非痛点模块，仅当三维扩展（[T3](../../plans/wayfinder/T3.md)）要求时由适配层工单驱动改造。
- 颈线法策略算法本身（缺口在 [neckline-algorithm-gaps] memory 独立跟踪，非架构债）。
