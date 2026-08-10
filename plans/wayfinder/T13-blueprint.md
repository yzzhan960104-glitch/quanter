---
id: T13-blueprint
title: data 模块总体治理蓝图（数据完整性 gate + 数据架构收口）
type: design/blueprint
labels: [wayfinder:blueprint]
status: draft
governs: [T13]
graduates_to: [T13-A, T13-B, T13-C, T16]
graduated_from: [T5, T12, T14]
date: 2026-08-10
author: wayfinder-session（brainstorming 产出，待用户复核）
---

# data 模块总体治理蓝图

> 本蓝图是 [T13](T13.md)（data 完整性生产 gate + 自动补采重构）的**总体治理设计**。
> T13 工单自身注明「领取时先 grilling 定是否拆子工单」——本蓝图完成该 grilling：把 T13 原始 5 项 + T12 置顶项，扩展为**双支柱**结构，毕业为 4 个可独立交付的子工单（T13-A/B/C + T16）。
> 权威归宿：本蓝图持有**设计决策与共享原则**；各波/柱的逐行实现计划由对应子工单的 spec→plan 承担。

---

## 0. 地基核实（2026-08-10，开工前置）

| 工单/事实 | 状态 | 结论 |
|---|---|---|
| [T1](T1.md) engine god module | ✅ done | 已治理（与本蓝图无关，仅确认前置主脊柱稳） |
| [T12](T12.md) 活债真因（T13 原 blocker） | ✅ closed | 实证：daily 双轨 + 通用同步器**无写入前历史行数守卫** → 1020万行被残片覆盖 |
| [T14](T14.md) P0 恢复 | ✅ closed | 湖已恢复 + 增量到最新交易日 |
| **湖真实状态**（实测 `a_shares_daily.parquet`） | — | **10,218,475 行 / 2016-07-06 ~ 2026-08-07 / 2451 天**，全量可信 |
| [T15](T15.md) tushare 代理 env 根治（P0） | ⚠️ 未做 | schtasks 环境仍带失效 `ALL_PROXY` → Wave B / 支柱 2 的**生产验证前置**（不阻断设计/单测） |
| `repair_gaps --auto`（T14 实证） | ⚠️ | 跑 10min 无输出 → 印证 T5「孤立 CLI 难用」，repair 自动化含**性能/可观测性**问题 |

**结论：地基已稳，T13 可正式设计。** 两个隐患（T15 / repair 可用性）在蓝图共享原则中显式处理。

---

## 1. 缺陷模型：三层静默失效级联

「完整性 gate 缺陷」不是单点，而是**写入→检测→兜底三层全部静默**的级联。任何一层单独修都不够（T5 实证：单日湖上三层全失效）。

| 层 | 现状（破） | 灾难实例 |
|---|---|---|
| **L1 写入侧** | 通用同步器 `to_parquet` 直接覆盖，**无写入前历史行数守卫**；daily 双轨（增量正确 vs 全量重建无守卫） | T12：1020万→3200 行被残片覆盖 |
| **L2 检测侧 / 生产 gate** | `freshness.check_freshness` **只校验 `latest_date ≥ expected_date`**（源码 `data/freshness.py:80`），不校验连续性/行数骤降/复权/列完整 | 单日湖 max date 仍是今天 → freshness **PASS**；300214.SZ 缺 07-14~07-21 不被发现；连 1020万→3200 抹除都通过 |
| **L3 兜底 / 交易侧** | `filter_universe_by_continuity` 是最后一道，但 **fail-open + 静默**（跳过缺标的不告警） | 缺口「被动跳过永不发现」，且**永不被补采** |

**而能修的工具全是孤立 CLI**：`scan_integrity`（能扫历史缺口，全市场 ~1.2s）无任何调度；`repair_gaps` 裸调 `get_pro()`（`data/tools/repair_gaps.py:168`）**不走 `_fetch_with_guard`**，且 `--auto` 实测 10min 无输出——既有性能问题也无限频守卫。

---

## 2. 支柱 1：数据完整性 gate 治理

### 2.1 目标三层架构——逐层硬化，每层 FAIL 语义不同

核心思想：**把三层「静默」逐一改成「有声」，并用 scan→repair 闭环补上「修复」这一缺失环节。** 6 项映射到 3 层：

```
L1 写入侧守卫（防抹除）────────────────────────────────────
  ① 写入前历史行数守卫：new_rows < existing×90% → 拒写 + CRITICAL 告警
  ② daily 双轨收口：删 TUSHARE_DATASETS["daily"]，唯一写入口 = sync_daily_incremental
  ③ freshness 加「行数骤降」维度（同湖行数环比阈值，补 max-date 盲区）
  ▸ FAIL 语义 = 硬阻断（拒写），不可降级
        │  写入安全后，检测方有意义
        ▼
L2 检测侧生产 gate（发现缺口）──────────────────────────────
  ④ scan_integrity 升级生产强制 gate：
       - 接现有数据就绪 checkpoint（freshness 同一插入点），加连续性维度
       - 每周独立全扫调度（周期性 backstop，防日级 gate 漏网）
  ⑤ 完整性维度收敛 data/integrity.py：D5 复权一致 / D6 列完整 / D7 跨湖时区 / D9 行数阈值
  ▸ FAIL 语义 = 分层分级：告警 + 入补采队列，不阻断当日交易
        │  scan FAIL → 触发补采
        ▼
L3 自动补采闭环（修复缺口）────────────────────────────────
  ⑥ repair_gaps 裸调 pro → _fetch_with_guard（限频守卫，⑦的前置）
  ⑦ repair 自动触发：scan FAIL → repair，受 配额 + 熔断
       - 单次配额：最多补 N 段 / M 行
       - 熔断：连续 K 次失败（含 T15 代理失败）→ 暂停 X 小时 + 告警
  ▸ FAIL 语义 = 熔断时降级告警（绝不把代理失败当成功吞掉）
        │  兜底
        ▼
交易侧（已存在，去静默化）─────────────────────────────────
  filter_universe_by_continuity：保留 fail-open 跳过缺标的，但接 L2/L3 告警通道 → 不再静默
```

**关键设计判断**：
- **L1 必须最先做且独立**——它是后两层的前提（湖能被静默抹除，再精巧的 scan→repair 都是舞台剧）。且 L1 无外部网络调用，**不依赖未解的 T15 代理债**，可立即交付。
- **L2/L3 是「gate 缺陷」本体**，但生产验证依赖 T15（否则补采全失败、scan 看着全绿是假象）。
- scan 的精确插入点（`trading/orchestrate/pipeline.py` checkpoint vs `ops/data_pipeline.py` sync 钩子）留 Wave B spec 实证——codegraph 显示 `check_freshness` 生产调用方为前者。

### 2.2 波次（6 项 → 3 波 → 3 层）

| 波次 | 层 | 工单项 | 内容 | 规模 |
|---|---|---|---|---|
| **Wave A · 写入侧安全网** ✅ (2026-08-11) | L1 | **#6** 写入守卫 + **#4** 双轨收口 + freshness 行数骤降 | 写入前行数守卫、删 daily 双轨、freshness 加骤降维度 | 小 ✅ |
| **Wave B · scan→repair 闭环** | L2+L3 | **#1** scan 生产 gate + **#5** 限频守卫 + **#2** 自动补采触发 | 闭环本体：scan 升级强制 gate → FAIL 自动 repair（受守卫） | 大 |
| **Wave C · 维度扩展** | L2 | **#3** D5/D6/D7/D9 | 在 Wave B 已验证的 scan 上叠加复权/列/时区/行数阈值 | 中 |

**为什么 #3 单独成波**：Wave B 的使命是**证明 scan→repair 闭环在生产链跑通**，本身已是最大风险块；混入 4 个异质新维度会稀释验收焦点。Wave C 在「已证可行的环」上做增量增强，风险隔离更干净。

### 2.3 降级语义（分层分级 · 已定）

| 触发点 | FAIL 处置 | 依据 |
|---|---|---|
| L1 写入守卫 | **硬阻断**（拒写 + CRITICAL） | 防抹除，不可降级 |
| L2 生产 scan | 告警 + 入补采队列（**不阻断当日交易**） | 不因历史缺口硬阻断实盘 |
| L3 自动 repair | 熔断时降级告警（**绝不把代理失败当成功吞掉**） | T15 解耦 |
| 交易侧 filter_universe_by_continuity | 保留 fail-open 跳过缺标的 + **接告警通道去静默** | 修复「永不发现」根因 |

---

## 3. 支柱 2：数据架构收口（毕业为独立工单 T16）

> 来自用户补充指令：「干掉计算单元，所有数据回归统一维护到数据湖里。」
> 经实证澄清，真实意图 = **拆 Win→Mac 跨机回测架构（compute_unit）+ discovery 回迁 Win 本地 + 回测产出回湖统一维护**。

### 3.1 现状实证（codegraph + 源码）

- **discovery 本就有 Win 本地实现**：`discovery/`（`cli.py`/`daemon.py`/`worker.py`/`neighborhood.py`/`publish.py`），调同一套 `evaluate`/`freeze`/`holdout_split`，已有 spawn 并行。
- **compute_unit 是 discovery 的 Mac 远程副本**：`compute_unit/runner.py` 原文「**完全沿用 discovery.worker 的 spawn 四铁律**」；靠 `task.json`/三件哈希跨机同步，产出 `result.json`（docstring 钉死「**不回传**」）。
- **结果已部分入库但散落**：`discovery/store.py`（SQLite，snapshot/trial 表，`task_export` 调 `trial_id_of`）+ Mac 本地 `result.json` → 两套结果存储未统一。
- **compute_unit 不拥有湖外市场数据**：对湖只读（`parquet_sha256` 做环境校验，`freeze` 读湖）；唯一湖外产物是 `tasks/*.json`（task 规格）与 `result.json`（指标）。故「数据回归湖」的数据层面动作 = **把回测产出（指标）收口进湖**，而非搬迁市场数据。

### 3.2 三子项

| 子项 | 内容 |
|---|---|
| **2a 拆 compute_unit** | 删 Mac 远程跨机回测（task.json 协议/三件哈希/env_check），discovery 收敛到 Win 本地 `discovery/`（已存在）。24 文件引用需逐一改线 |
| **2b 结果回湖** | discovery 产出（`TrialResult` 指标：inner/outer kelly/calmar/replay report）从 `result.json`+SQLite 收口进数据湖统一维护（受 Wave A 写入守卫保护） |
| **2c SSoT 原则** | 数据湖=唯一 SSoT；所有计算（discovery/backtest/compute 残留）只读湖、产出回湖；计算模块不拥有数据 |

### 3.3 ⚠️ 资源隔离风险与决策（已定：时段隔离）

Win 是**实盘交易机**，discovery 是重型计算（12 worker × ~0.3GB、单组 params ~720s）。当初上 Mac 即为**把探索算力隔离出实盘机**。回迁 Win 本地的致命副作用 = 与实盘抢 CPU/内存、拖累下单延迟。

**决策（已定）：时段隔离**——discovery 仅盘后/周末（trading engine idle）在 Win 本地跑，零实盘 contention 风险。调度触发机制留 T16 spec。

---

## 4. 总依赖图与排期

```
   T15（代理 env 根治，P0，独立并行轨）──────────────┐
        │ 生产验证前置（非设计前置）                  │
        ▼                                            ▼
 ┌─────────────┐    无代码依赖     ┌─────────────────────┐
 │  Wave A     │ ────(仅时序)────▶ │  Wave B              │
 │  L1 写入守卫 │  A 先做：否则 B   │  #5 限频守卫 ──┐      │
 │  (⊥ T15)    │  是舞台剧         │  #1 scan gate ─┤(FAIL信号)│
 └──────┬──────┘                   │                ▼      │
        │                          │  #2 自动补采触发(依赖#5+#1)│
        │                          └─────────┬───────────┘
        │                                    │ #1 落地后
        │  支柱2 依赖 Wave A：                ▼
        │  湖可信+写入守卫就位后        ┌─────────────────────┐
        └───────────────────────────▶ │  Wave C              │
                                      │  #3 D5/D6/D7/D9      │
                                      └─────────────────────┘
        │
        ▼
 ┌─────────────────────┐
 │  支柱 2（T16）        │
 │  2a 拆 compute_unit   │
 │  2b 结果回湖          │  ← 2b 的结果写入复用 Wave A 写入守卫
 │  2c SSoT 原则         │
 │  discovery 时段隔离   │
 └─────────────────────┘
```

**依赖铁律**：
- **Wave B 内部**：#5（限频守卫）是 #2（自动触发）的**硬前置**——无守卫的自动 repair 会触发 Tushare 限频雪崩（CLAUDE.md 风控红线）；#1（scan gate）提供 #2 的 FAIL 信号。
- **Wave A → B**：无代码依赖，仅时序——A 不先做，B 的 scan 在「能被静默抹除的湖」上无意义（T12 教训）。
- **Wave B → C**：#3 叠加在 #1 的 scan 上，#1 未落地则 #3 无处可挂。
- **Wave A → 支柱 2**：湖可信 + 写入守卫就位后，discovery 才能在可信湖上跑、结果回湖才受守卫保护。
- **T15 → B / 支柱 2**：仅**生产验收**前置（设计/单测不受阻）。

**排期**：`Wave A → Wave B → Wave C`，支柱 2（T16）在 Wave A 落地后进入。

---

## 5. 跨柱共享原则（蓝图级）

| # | 原则 | 状态 |
|---|---|---|
| 1 | **降级分层分级**（见 §2.3） | ✅ 已定 |
| 2 | **行数检查 SSoT**：行数校验逻辑收敛 `data/integrity.py` 单一函数；freshness（写入/实时侧）与 scan（周期侧）各自调用。Wave A 的「freshness 行数骤降」与 Wave C 的「D9 行数阈值」共用此函数，避免两套实现 | 待 spec 落实 |
| 3 | **写入守卫普适**：Wave A 的写入前行数守卫保护**所有湖写入口**——sync、repair、**支柱 2b 结果回湖**的结果写入。一个守卫、全写入口复用 | 设计判断 |
| 4 | **可观测性统一**：所有 gate/repair 事件走统一告警通道（CRITICAL/WARN），消除 `filter_universe_by_continuity` 静默 | 设计判断 |
| 5 | **T15 降级**：自动 repair 在代理失败时熔断不静默吞，与 T15 解耦（设计不阻、生产验证前置） | 设计判断 |
| 6 | **discovery 时段隔离**：盘后/周末 Win 本地跑，零实盘 contention | ✅ 已定 |

---

## 6. 各波/柱 spec 待定项（蓝图不深入，留对应子工单 spec）

| 波/柱 | 待 spec 定 |
|---|---|
| **Wave A** (T13-A) | 90% 阈值校准 · 双轨收口 registry 改动范围 · freshness 行数骤降环比窗口 · 行数 SSoT 函数签名 |
| **Wave B** (T13-B) | scan 精确插入点（`trading/orchestrate/pipeline.py` checkpoint vs `ops/data_pipeline.py`）· 配额/熔断阈值 · 每周全扫调度机制 · repair 性能（10min 无输出）根治 |
| **Wave C** (T13-C) | D5/D6/D7/D9 探测算法 · 与行数 SSoT 接口 |
| **支柱 2** (T16) | 生产拓扑实证（Win `discovery/` 是否全量承载、Mac 是否仍主力）· 结果回湖 schema · 时段隔离调度触发 · 24 文件改线清单 · compute_unit 删除顺序（先改线后删模块） |

---

## 7. Out of scope（归其他工单，非本蓝图）

- **[T15](T15.md)** tushare 代理 env 根治（独立 P0，蓝图只做降级耦合）
- **account_daily.start 漏采**（live P0 运维，pre_open 窗口内必须起）
- **[T2](T2.md)** broker/qmt.py 业务层 + trading↔broker 耦合
- 文档债 `data_pool.md` / `caisen-methodology-summary.md`（T0 丙删）
- 颈线法策略算法本身（缺口在 neckline-algorithm-gaps memory 独立跟踪，非架构债）

---

## 8. 治理归宿与毕业

T13 留作**伞工单**（持有本蓝图），四波/柱各毕业为独立 wayfinder 子工单，各自独立 spec→plan→实现→验收，进度回填 T13：

| 子工单 | 承载 | 依赖 |
|---|---|---|
| **T13-A** | 支柱 1 Wave A（L1 写入守卫 + 双轨收口 + freshness 行数骤降） | 无（⊥ T15） |
| **T13-B** | 支柱 1 Wave B（scan 生产 gate + 限频守卫 + 自动补采） | T13-A；生产验证依赖 T15 |
| **T13-C** | 支柱 1 Wave C（D5/D6/D7/D9 维度扩展） | T13-B (#1) |
| **T16** | 支柱 2（拆 compute_unit + 结果回湖 + SSoT 原则 + 时段隔离） | T13-A |

> T13-A/B/C/T16 的工单 Question 草稿在各子工单建立时填写（参考本蓝图 §6 待定项）。

---

## 9. 已确认决策清单（brainstorming 共识）

1. **范围**：本次产出 T13 总体治理蓝图（不立即深入单波 spec）。
2. **降级语义**：分层分级（L1 硬阻断 / L2 告警不阻断 / L3 熔断降级 / 交易侧 fail-open 去静默）。
3. **compute_unit 真实意图**：拆 Win→Mac 跨机回测架构 + discovery 回迁 Win 本地 + 回测产出回湖（经实证：compute_unit 是 Mac 远程副本，不拥有湖外市场数据）。
4. **discovery 资源隔离**：时段隔离（盘后/周末 Win 本地跑）。
5. **双支柱结构**：支柱 1 完整性 gate / 支柱 2 数据架构收口。
6. **支柱 2 毕业为 T16**；T13 伞工单持有蓝图。
7. **排期**：Wave A → B → C；支柱 2 依赖 Wave A。

---

## 变更日志

- 2026-08-10 初版（brainstorming 产出，待用户复核）。
