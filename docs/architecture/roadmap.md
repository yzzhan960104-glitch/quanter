> 最近复核：2026-08-14 · 维护者：glm-5.3-session ·
> 权威归宿：**演进目标态 / Phase / 波次编排与状态**（living）。现状见 #1-#8；债务见 [#6](06-tech-debt.md)；批判评估见 [deep-dives/2026-08-14-critical-review.md](deep-dives/2026-08-14-critical-review.md)。
> ⚠️ 2026-08-14 复核注记：本文件此前仅覆盖 wayfinder T 系工单且状态滞后两波（Phase 1 曾仍写「待细化」而 T1 实已完成两周）。本次重写并入 W/G/A/C/P 全部波次轨，状态以 git/reflog 实证为准，不以工单文件头为准（工单状态失同步本身是 [CR-9](deep-dives/2026-08-14-critical-review.md) 登记的债）。

# 演进路线图（living）

《quanter 架构演进蓝图》的分阶段路线图。**演进优先于 live**，但设「可上 live 判据」里程碑防无限演进。

## Phase 编排（主脊柱）

### Phase 0 — 现状架构全景梳理 ✅（[T0](../../plans/wayfinder/T0.md) · 2026-08-08）
- 8 全景视图（#1-#8）✅ · engine 当前态深剖 ✅（[T0.1](../../plans/wayfinder/T0.1.md)）· 过时文档丙删 ✅

### Phase 1 — `engine.py` 模块化拆分 ✅（[T1](../../plans/wayfinder/T1.md) · 2026-08-10 完成）
- 3437 → 1546 行（-55%），8 集群外迁（critical / order_state / data_ctx / eod_plan / ports + phases×4），`_ACTIVE_ENGINE` 单例桥清零
- 红线守住：trading 515 单测 + e2e 长周期 26 测全绿（行为等价）；历史态解剖 → [deep-dives/engine-current-state.md](deep-dives/engine-current-state.md)

### Phase 2 — 可插拔适配层（[T2](../../plans/wayfinder/T2.md) · **进行中**）
- **W1-A keystone ✅ 已合并（2026-08-12，`96d55418` fast-forward，origin 已含）**：`_eng_mod` 反查 19 符号全量退役、`trading_service` 下沉 `trading/gateway_service.py`（trading→presentation 边权 2→0）、~360 测试 patch 迁物理路径
- **W1-B 连接韧性 ◐ 半程**：T9 探针已落（`299ab2de`）；T10 嵌套父子 / T11 启动重试未完
- **W2 broker 分层 ✗ 未启动**：H1 broker/qmt 业务层重构（1540 行堆补丁 → 适配层契约）+ H2 回调体 Ports + M2 读写键治理——T2 的主体，依赖 W1 全收
- 契约核心切点：trading↔broker 回调写 DB（[#2](02-module-dependencies.md) 双向耦合表）

### Phase 3 — 三维扩展（[T3](../../plans/wayfinder/T3.md) · 阻塞于 T2）
- 多策略 × 多资产（港/美/期）× 多账户；落地顺序与 compute_unit 角色待定（MAP Not-yet-specified）
- 前置依赖：W3 多策略 schema（DG-5/DG-6）仅划线未启动

## 横向波次轨（2026-08-14 盘点，与主脊柱并行）

| 轨 | 波次 | 目标 | 状态 |
|---|---|---|---|
| 技术债总纲（08-11 spec） | **W0** | 护栏+清场：M4 测试卫生 / M1 data-cycle / T7 验证 ADR / T8 依赖 ADR / H3 Phase C 尾 / D1 T13-B 尾 | ✅ done（08-12，`b699d2ad` 闭合） |
| | **W1-A** | T2 keystone（反查切断 + 下沉 + account_id 收口） | ✅ done 已合并（见上） |
| | **W1-B** | 连接韧性（T9/T10/T11） | ◐ T9 落，T10/T11 待 |
| | **W2** | H1 broker 分层 / H2 回调 Ports / M2 读写键 | ✗ 未启动 |
| | **W3** | 多策略 schema（DG-5/DG-6） | ✗ 仅划线 |
| 审计治理（08-13 spec） | **G 波** | 七类保护链 fail-closed：G1 CI 复活 / G2 鉴权 / G3 熔断基线 / G4 超时 / G5 原子写 / G6 SQLite+幽灵单 / G7 告警观测 / G8 sid 轮换闸 | ✅ G1-G8 全进 master（08-13~14；**G8 `2e74cb9d` 尚未 push**） |
| | **A 波** | **策略可信**：A1 regime 闸 / A2 walk-forward 判定 / A3 成交真实性 / A4 Kelly 收敛 / A5 价位单源 | ✗ **未动工**（spec 显式「负责任地跳过」；A2 已被 P5 覆盖基建） |
| | **C1** | 配置六层默认值 SSoT（stop_atr_mult/trailing/tp1_portion） | ✗ 未做 |
| | **P2-ts** | 时间戳三口径统一（Asia/Shanghai aware） | ✗ 未做 |
| 策略优化（08-12 spec） | **P0-P6** | P1 向量化 35.5s / P2 TPE batch / P3 敏感性后台 / P4 min_rr 复活 / P5 walk-forward / P6 数据指纹 | ✅ 已合并（`cc4629b7`，08-13）；P4 语义候选未启动（ADR 诚实标注） |
| 连接韧性（毕业自 T4） | T9 高 / T10 中 / T11 低 | 同 W1-B | ◐ 见上 |
| data 完整性 | [T13](../../plans/wayfinder/T13.md) | 生产 gate + 自动补采 + 写入守卫 | ✅ 代码路径全治（T13-A/B + D1）；⚠️ **运行态尾巴：15701 段漏采、补采回路熔断停摆**（[CR-6](06-tech-debt.md)） |
| state_store SSoT | [T6](../../plans/wayfinder/T6.md) | SSoT 演进收口 | ◐ Phase A/B/C 大头已治，残余待领 |

> 三套工单体系并存（wayfinder T 系 / 总纲 W 系 / 审计 G-A-C 系 + opt P 系），交叉映射见各自 spec；**状态同步债见 [CR-9](deep-dives/2026-08-14-critical-review.md)**——领工单前以 git 实证为准。

## 横切决策

- [ADR-09 验证策略](09-t7-validation-strategy.md) — L1/L3/L4 四层验证 + 性能基线（10% 软拦）
- [ADR-10 依赖态度](10-t8-dependency-policy.md) — 纯标准库基线 + 绝不引入清单
- [框架评估决策](../2026-08-11-framework-evaluation-decision.md) — 六大外部框架全不引入，四类不可迁移资产论证

## 里程碑：可上 live 判据（**待量化——当前演进与 live 之间最大的空洞**）

四大痛点收口 + 多策略/多账户隔离经模拟盘验证——具体量化标准仍空缺。**2026-08-14 批判性复核的立场**（详见 [deep-dives §4.1 张力一 / §6 建议 1](deep-dives/2026-08-14-critical-review.md)）：

1. **A 波（策略可信）是当前最高优先**，不是因为它最容易，而是它是唯一决定「这一切值不值」的波。P5 walk-forward / DSR / P6 指纹工具已全部就位——该用它们出验证结论了（颈线法分年表现 / 折外衰减 / 滑点敏感性）。
2. **给「演进优先于 live」装停损点**：定义量化判据（例：分年 Calmar 全正 + walk-forward 折外衰减 <50% + 成交摩擦敏感性存活 + 影子期 N 天零 L1），判据不过则**冻结非安全类演进**（W2/T3 让位策略迭代），防止基础设施建设成为拖延策略验证的心理出口。
3. live 前硬前提（安全侧，G 波后已大幅收敛）：CR-1 修复 / CR-2 价位单源 / CR-4 curr_equity fail-closed / CR-6 补采回路复活 / audit_ssot 挂调度。
