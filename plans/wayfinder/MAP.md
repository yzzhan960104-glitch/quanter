---
id: QUANTER-ARCH
title: quanter 量化系统架构演进蓝图 + 优化路线图
labels: [wayfinder:map]
status: open
tracker: local-markdown
---

# quanter 量化系统架构演进蓝图 + 优化路线图

> 本 map 由 wayfinder 建立。它是**索引**，不是知识库——每个决策只活在它的工单里，map 只 gist + 链接。
> tracker = local-markdown（本目录 `plans/wayfinder/`）。工单 = 同目录下 `T{id}.md`。
> 工单类型 `type` ∈ {research（AFK）/ prototype（HITL）/ grilling（HITL）/ task}。

## Destination

产出一套《quanter 量化系统架构演进蓝图 + 分阶段路线图》（**spec，不改代码**）：以"现状架构全景梳理"为 Phase 0 地基，**渐进式解构** `trading/engine.py`（3437 行 god module）为"**可编排内核 + 可插拔策略/资产/账户/经纪商适配层**"，支撑"**多策略 × 多资产（港/美/期）× 多账户**"三维扩展；同步收口 **miniQMT/xtquant 连接稳定性、data 完整性、state_store SSoT 治理**四大痛点。**演进优先于 live**，但设"**可上 live 判据**"里程碑防止无限演进。

## Notes

- **领域**：A 股量化交易系统（颈线法策略，Tushare 唯一数据源，miniQMT/xtquant 经纪商），即将从模拟盘进入实盘；已历经 SSoT(C1-C9)/进程模型/时间统一/风控挡板/错误分级/调度硬化/启动补跑/数据观测治理等系统级改造。
- **每次 session 必查 skill**：`mattpocock-skills:grilling`、`mattpocock-skills:domain-modeling`、`mattpocock-skills:prototype`、`mattpocock-skills:research`。
- **本项目硬约束（来自 CLAUDE.md，优先级最高）**：① 全中文沟通与文档；② Karpathy 极简——拒绝重型黑盒框架；③ 量化风控极度拷问；④ 反前视偏差/NaN/除零/时区错位/部分成交。
- **演进护栏**："可上 live 判据"里程碑 = 四大痛点收口 + 多策略/多账户隔离经模拟盘验证；禁止无限演进。
- **tracker 约定**：工单答案以 `## Resolution` 段追加到工单正文，关闭后回填 *Decisions so far*。`frontier` = open 且 `blocked_by` 为空。
- **✅ data 活债完整闭环（[T12](T12.md)+[T14](T14.md)+[T15](T15.md) closed）**：a_shares_daily 已恢复（**1021万行 / 2016-07-06~2026-08-07 / 2451天**）+ SOCKS 代理 env 已移除（schtasks 新进程不再失败）。**剩治本**（写入守卫 + 双轨收口 + scan/repair 自动化）归 [T13](T13.md)。⚠️ a_shares_daily 现为 modified 未提交，建议尽快 `git commit` 固化。
- **broker 业务层重构归 T2**（T4 实证；**已兑现**）：连接层不需重构；`broker/qmt.py` 1540 行的"堆补丁"在业务层——T2 keystone（08-12）+ W2-H1 broker 四文件分层 + BrokerProtocol（2026-08-15 `e3195df0`，54/54 函数逐字等价搬迁）已落地。

## Decisions so far

- [miniQMT 连接真根因调研](T4.md)（closed）— xtquant 交易层经共享内存队列通信，**同一 sid 单进程独占、一次性连接、无内置心跳/重连**；"连接不稳定"是违反三约束的派生症状（A残留/B双进程/C重连盲区/D客户端判定），A/D 已修 B/C 部分修；**不重构 broker**，毕业 [T9](T9.md)/[T10](T10.md)/[T11](T11.md)；串通挂撤/拒涨停/撤单延迟是业务机制**非连接 bug**。
- [data 完整性根治调研](T5.md)（closed）— 生产 gate 只校验实时性不校验历史连续性；`scan_integrity`/`repair_gaps` 孤立 CLI 无调度；历史缺口被动跳过**永不发现**；叠加活数据债三层 gate 静默失效。根治 5 项，毕业 [T13](T13.md)。
- [data 活债排查](T12.md)（closed）— `a_shares_daily` 8/7 18:12 被 `sync_tushare.py daily`（data_service 启动 sweep）残片覆盖 **1020万→3200行**；根因三层（SOCKS 缺 PySocks / daily 双轨 / freshness 不测行数骤降）。HEAD LFS 对象完整可恢复。毕业 [T14](T14.md)+[T15](T15.md)，治本归 [T13](T13.md)。
- [恢复 a_shares_daily](T14.md)（closed）— 全量恢复（LFS d96839e，1020万行）+ 增量补到 08-07（**1021万8475行/2451天**）。装 pysocks + 临时 unset ALL_PROXY 直连。
- [tushare 代理 env 根治](T15.md)（closed）— 决策 B：移除 Windows User env ALL_PROXY（来源仅 User env，.env/bat/profile 无）。SetEnvironmentVariable 删除 + 注册表确认（`HKCU\Environment\ALL_PROXY` 找不到）→ schtasks 新进程不再继承 → 下次 sync 直连 tushare。提示：代理软件若重启设系统代理会复发，需在代理软件层关。
- [现状架构全景梳理](T0.md)（closed 2026-08-08）— **8 视图全景**（topic-flat + Mermaid，`docs/architecture/`）+ **单一归宿映射表**；engine 深剖毕业 [T0.1](T0.1.md) 阻塞 T1；过时文档丙删（`data_pool.md` + `caisen-methodology-summary.md`）。T1/T2/T3 公共地基。
- [engine.py 当前态深剖](T0.1.md)（closed 2026-08-10）— 产出落盘 [deep-dives/engine-current-state.md](../../docs/architecture/deep-dives/engine-current-state.md)（10 责任集群 + 调用图 + 缝合点排序）；[T1](T1.md) 据此执行拆分（3437→1546 行，-55%），文档转「拆分前历史态」活文档。
- [可插拔适配层接口契约](T2.md)（done 2026-08-12）— W1-A/t2-keystone 27 commit 合并（final `96d55418`）：`_eng_mod` 反查切断（grep=0）+ `EnginePorts` 窄接口 + `_resolve_account_id` 单源。**三维扩展完整目标未全竟**（诚实注记见工单 Resolution）：余项 W2（2026-08-15 已落 H1/H2/M2）/W3。
- [data 完整性生产 gate + 自动补采重构](T13.md)（done 2026-08-11）— T13-A 写入侧安全网（`assert_safe_overwrite` 四写入口 + daily 双轨收口）+ T13-B scan→repair 闭环（`31304e9f`）合并；T13-C 维度扩展未竟转出；**停牌真值缺口**（suspend_d 不覆盖历史长停牌 → 16371 段误判 unjustified）另立新工单（2026-08-15 波次登记素材）。
- [连接韧性三件](T9.md)（done）/[T10](T10.md)（closed）/[T11](T11.md)（closed）— T9 主动探针 `299ab2de`（query_account_status 补 on_disconnected 僵死盲区）；T10 实证嵌套为**单树 launcher 形态非双起** → 递归祖先链去重覆盖后降级关闭（不引入新机制）；T11 connect -1 逐轮留痕 + L1→L2→L3 重试链观测闭合（重试语义保持 G8 形态，退避增项裁定过度设计）。
- [state_store SSoT 演进](T6.md)（closed 2026-08-15）— 四决策点定稿：① plan→SQLite 不重启（DG-5 划线）② 多策略/多账户隔离=W3 划线 ③ 表扩展路径=Phase A/B/C 已治 + fill.direction CHECK（2026-08-15 T3）④ 读写键对齐=is_vetoed 单点 + actual_sid DB 单源 + StopLossContext（T11/T14）。
- [验证策略](T7.md)（closed 2026-08-15）— 四层验证（L1/L2/L3/L4）+ 性能基线 10% 软拦，落 [ADR-09](../../docs/architecture/09-t7-validation-strategy.md)；2026-08-15 波次 Wave E 门实跑兑现（L3 26 passed / L4 零差异 / perf +1.9%）。
- [依赖态度](T8.md)（closed 2026-08-15）— 严守 Karpathy 极简 + 绝不引入清单 + 三问审批口径，落 [ADR-10](../../docs/architecture/10-t8-dependency-policy.md)；W1/W2 两波次零新增依赖实跑佐证。

## 路线图骨架（Phase 雏形，工单细化后回填）

- **Phase 0** — 现状架构全景梳理 → [T0](T0.md)✓ / [T0.1](T0.1.md)✓
- **Phase 1** — `engine.py` 模块化拆分 → [T1](T1.md)✓
- **Phase 2** — 可插拔适配层（含 broker 业务层重构）→ [T2](T2.md)✓（keystone 08-12；broker 分层 H1 于 2026-08-15 W2 落地）
- **Phase 3** — 三维扩展落地 → [T3](T3.md)（frontier 唯一余项）+ 待定
- **横向并行** —
  - 连接韧性（毕业自 T4）：[T9](T9.md)✓ / [T10](T10.md)✓ / [T11](T11.md)✓
  - data 完整性（毕业自 T5/T12）：[T13 生产 gate+自动补采+写入守卫重构](T13.md)✓（A/B 合并；C 维度扩展+停牌真值缺口转出）
  - [state_store SSoT 演进](T6.md)✓
- **横切决策** — [T7 验证策略](T7.md)✓ / [T8 依赖态度](T8.md)✓（ADR-09/10）
- **里程碑** — "可上 live 判据"
- **已关闭** — [T4](T4.md)✓ / [T5](T5.md)✓ / [T12](T12.md)✓ / [T14](T14.md)✓ / [T15](T15.md)✓ / [T0.1](T0.1.md)✓ / [T1](T1.md)✓ / [T2](T2.md)✓ / [T13](T13.md)✓ / [T9](T9.md)✓ / [T10](T10.md)✓ / [T11](T11.md)✓ / [T6](T6.md)✓ / [T7](T7.md)✓ / [T8](T8.md)✓

## Frontier（当前可领取的未阻塞工单）

> 2026-08-15 CR-9 对账后重写：T0.1/T9/T13 已完成移出；T6/T7/T8/T10/T11 已闭；**T3 是唯一余项**——原阻塞方 T2 已 done（W2 主体 H1/H2/M2 亦于 2026-08-15 落地），阻塞解除可领取。

- [多账户资金/持仓/订单隔离与路由](T3.md) — grilling · **可领取**（Phase 3 主脊柱末段；前置 T2 keystone + W2 broker 分层/Ports 化/actual_sid 单源已就位；验证护栏见 [ADR-09](../../docs/architecture/09-t7-validation-strategy.md)，依赖边界见 [ADR-10](../../docs/architecture/10-t8-dependency-policy.md)）

> 阻塞链：主脊柱 T0.1→T1→T2 已全通（T0 closed 08-08 / T0.1 closed 08-10 / T1 done 08-10 / T2 done 08-12），**T3 为最后一环**；横向 T9→T11 已闭合（08-12/08-15）；横切 T7/T8 已闭（ADR-09/10）。
> 已登记（T17 · 2026-08-15 落 06-tech-debt 新债务区【High】）：scan 停牌真值缺口（suspend_d 不覆盖历史长停牌 → 16371 段误判 unjustified、补采配额永占；需另寻真值源 + repair 侧 unfillable 标记）。

## Not yet specified

- **Phase 3 三维扩展落地顺序**：多策略/多资产/多账户谁先？依赖 [T2](T2.md) 与 [T3](T3.md)。
- **"可上 live 判据"的量化标准**：T4/T5/T12/T14/T15 已关闭，剩余依赖 [T6](T6.md) + [T9-T13](T9.md) 横向工单成形。
- **compute_unit 在三维扩展下的角色**：依赖多策略落地形态。
- **broadcast/config 等非痛点模块的适配层改造**：依赖 [T2](T2.md)。
- **颈线法之外的新策略算法**：本次"多策略"指接口抽象，新策略算法是独立 effort。

## Out of scope

- **实时 / tick 级行情与超低延迟执行**。
- **broadcast / config / discovery / experiment / ops 的非痛点改造**（仅当三维扩展要求时由适配层工单驱动）。
- **wayfinder 阶段直接改业务代码**（spec/路线图由工单 session 落地；P0 运维恢复 T14/T15 例外，属数据/环境债急救）。
- **期权 Greeks / FICC / 做空偏好等非颈线法策略的算法实现**。
