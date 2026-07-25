# Plan 4 设计文档：L4 守护 daemon + L5 闭环 publish（spec §5.2 / §5.3 / §12 #13 #14）

> 主 spec：`2026-07-23-param-discovery-engine-design.md`（五层架构 L0-L5）。
> Plan 1-3 已完成 L0-L3 + L4 收敛部分（84 non-slow 测试绿）。本 Plan 补齐 **L4 守护调度**与 **L5 闭环 publish**，是 discovery 引擎最后一块拼图。

## 0. 定位

Plan 4 = **L4 守护 daemon（跨夜收敛① + schtasks 夜跑 + 断点续跑 + 新冠军钉钉 + 冠军 outer 去偏）+ L5 闭环 publish 收回版（publish→experiment DRAFT + outer 报告 + ≥5天硬闸真强制）**。

**收回决策**：spec §5.3 字面的「candidate 影子 dry_run ≥5 天对比 prod vs candidate 虚拟 PnL」在当前架构下物理不可行（见 §3 核实发现），Plan 4 做到「publish + 硬闸 + 可观测」，candidate-only 虚拟 PnL 列 documented follow-up（Plan 5，需重写 trading 信号分流）。

---

## 1. 背景：Plan 1-3 现状 + Plan 4 前置门槛

### 1.1 Plan 1-3 已交付（分支 `feat/discovery-l0-l1`）

| Plan | 范围 | 关键交付 | 状态 |
|---|---|---|---|
| Plan 1 | L0+L1 可信度闭环 | `snapshot/split/objective/store/judging/cli/neighborhood` + 验收锚（冠军 2026 outer ann 109.8%）+ 邻域高原放行 | ✅ 7 task |
| Plan 2 | L2+L3 搜索基础 | SQLite 三表（WAL）+ ProcessPool + 断点续跑去重 + 约束裁剪 + 自写 Sobol | ✅ 5 task |
| Plan 3 | L3 TPE + L4 收敛 | optuna TPE（Sobol warm start）+ Pareto + 覆盖度 + DSR + 单 run 收敛②④ + 分层裁判 + cli champions/report | ✅ 8 task |

Plan 3 final review：**ready 进 Plan 4，无 Critical/Important**。

### 1.2 Plan 4 前置门槛（Plan 3 留的 pre-flight verification）

Plan 4 动工前必须先闭合 Plan 3 地基（T0 task）：

1. **`.venv310` 重跑 T8 slow 集成**固化绿灯——确认 Plan 3 端到端 TPE 收敛在真实环境稳定（CI 环境与 `.venv310` 可能分叉）。
2. **补 DSR vs `scipy.stats.norm.ppf` 数值对照断言**——锁 DSR 闭式的 Acklam 22 系数未抄错（`discovery/dsr.py` 独立实现，须与 scipy 权威实现逐数字一致）。

---

## 2. 范围与非目标

### 2.1 范围（做）

- **L4 daemon**：`python -m discovery daemon --budget 4h`，schtasks @02:00 每夜触发短命子命令，跨夜收敛判据①（连续 K=3 夜 Pareto 前沿不扩张 → 自停）+ 断点续跑 + 新冠军钉钉告警 + 冠军 outer 去偏评估。
- **L5 publish**：`python -m discovery publish <trial_id>`，冠军 trial → `experiment.create_version(DRAFT, source=discovery:run_id)` + outer 去偏报告 + 人审下一步提示（**不自动 promote**）。
- **≥5 天硬闸真强制**：`trading/__main__.py` 现状的 WARNING（名不副实）升级为 fail-closed——mode=live 时查 `resolve_active` 的 `activated_at`，任一 < `TRADE_SHADOW_MIN_DAYS` → `sys.exit(2)` + 钉钉 CRITICAL 告警。
- **discovery 包自包含调度**：`discovery/schtasks.py` + `discovery/run_daemon.bat`（不依赖 `scripts/`，scripts 废弃迁移是独立工程）。

### 2.2 非目标（不做 / follow-up）

- **candidate-only 影子虚拟 PnL**（spec §5.3 字面）：需重写 `_eod`/`pre_open` 信号分流（按 experiment 标影子态不挂单）+ 新建纸上 mark-to-market 记账。深侵入 trading 引擎，与 discovery 包职责边界冲突。列 **Plan 5 follow-up**（待 prod 在线跑顺 + experiment 系统经实战后再上）。
- **自动 promote / 自动 archive**（spec §2.2 既有非目标）：MVP 仅产 DRAFT 候选 + 报告，promote 经人工。
- **多 outer 段轮流**（留一折 outer）：spec §13 follow-up，outer holdout 仍用单段 2026。
- **Plan 3 final review 5 个 Minor**（ρ 诚实标注 / converged_k_rounds dead-export / ei=inf 格式 / n 默认 30 防御 / cmd_champions 与 runner 读 trial 重复）：Plan 4 顺手清或留 polish，非阻塞。

---

## 3. 集成点核实发现（spec 写作时未实证的假设——Plan 4 必须正视）

> 本节是 Plan 4 范围收回的物理依据。核实源码逐条记录，防止后续遗忘。

### 3.1 已就绪的基建（Plan 4 直接复用，零改动）

| 集成点 | 现状（源码核实） | Plan 4 接入 |
|---|---|---|
| `experiment/store.py create_version` | `ExperimentVersion` 已有 `source`/`status`/`weight`/`note` 字段；`create_version` 写 DRAFT（weight=0）+ audit_log | L5 publish 零改 experiment：`source=f"discovery:{snapshot_hash[:8]}"` 溯源直接可用 |
| `experiment/resolver.py resolve_active` | 返回 `[ActiveExperiment(experiment_id, strategy_name, params, weight)]`，读 status=ACTIVE | ≥5天硬闸查 `activated_at`（需 resolver 暴露该字段，见 §6.3） |
| `experiment cli promote/report` | DRAFT→ACTIVE+设权重（资金守恒校验）+ report 扫 trading_plans 按 experiment_id 聚合归因 | 人审 promote 链路既有 |
| `infra/notifier.py` | `NotificationManager` 异步单例 + `notify_risk_event(msg, level)` + `fire_and_forget(coro)` 跨线程 + `DingTalkChannel` errcode 校验（防 HTTP200+业务失败静默丢失） | 新冠军告警 + 硬闸告警直接调 |
| `trading/engine.py _eod`（line 590-688） | 已完整实现「resolve_active 读在线实验 → 按 params 装策略 → `scan_live` 产信号 → 注入 experiment_id/weight 归因 → eod_plan 落盘」 | candidate promote 后自动走此链路（影子验证引擎侧就绪） |
| `discovery/runner.py run_search` | 返回 `RunSummary`（status/rho/ei/frontier_size/dsr_top/convergence_reason），内部含断点续跑去重 + 单 run 收敛②EI ④覆盖度 | daemon 复用为单夜跑批内核 |

### 3.2 spec §5.3 的三个不可行假设（Plan 4 收回依据）

**Gap 1：`AUTO_TRADE_MODE=dry_run` 是「语义糖」，物理分流靠「不连网关」**
- `server/services/trading_service.py:54-80` 的 `get_gateway()` **根本不读 `AUTO_TRADE_MODE`**，只看 QMT 凭证：有凭证→连网关，无凭证→`None`。
- `AUTO_TRADE_MODE` 仅在 `trading/__main__.py:126` 被读，且只决定打不打 WARNING。真正的"不下单"= 没配 QMT 凭证。

**Gap 2：candidate 与 prod 共进程共网关，无法并行「prod 真下单 + candidate 影子」**
- `_eod()` 的 `resolve_active()` 把所有 ACTIVE 实验（含 promote 后的 candidate）一视同仁地 `scan_live` 产信号 → 同一条 `pre_open` 挂单链路。
- candidate 一旦 `promote ACTIVE weight=0.1`，就和 prod 走完全相同的下单路径。**没有任何 candidate-only 影子分流机制**。

**Gap 3：spec §5.3 引用的「`trading/__main__.py:103-111` 已有 ≥5 天硬闸」名不副实**
- 核实源码（line 122-137）：只是一个 `logger.warning` 提醒，切 LIVE 只需改 env 变量，warning 打完照样裸跑真单，**无任何强制校验"影子是否真跑满 5 天"**。

**推论**：要做到 spec §5.3 字面的 candidate 影子对比，须重写信号分流 + 新建纸上 PnL，深侵入 trading 引擎。Plan 4 收回到「publish + 硬闸 + 可观测」，诚实把 candidate-only 虚拟 PnL 列 Plan 5 follow-up。

---

## 4. 设计决策（三个分叉 + 包落点，均已对齐）

| # | 分叉 | 决策 | 理由 |
|---|---|---|---|
| D1 | daemon 编排形态 | **schtasks 每夜触发短命子命令**（非常驻进程） | 崩溃不影响次夜、复用既有 schtasks 模式 + Plan 2 断点续跑去重、进程短命可观测。spec §10 字面即此 |
| D2 | 跨 run 判据①持久化 | **扩展 `search_run` 表**（非独立表/JSON） | 状态与 search_run 同生命周期（snapshot 变=新一轮，k 自然重置），SQLite WAL 既有，零新基建 |
| D3 | ≥5 天硬闸实现 | **启动期 fail-closed `sys.exit(2)` + 钉钉告警** | 风控红线最保守，与既有"宁可漏挂单不盲发真单"语义一致；强制降 dry_run 会掩盖运维意图 |
| D4 | schtasks/bat 落点 | **discovery 包自包含**（`discovery/schtasks.py` + `discovery/run_daemon.bat`） | scripts/ 后续废弃；discovery 自包含不卷入其迁移，符合"各包管自己调度"惯例（与 broadcast schtasks 解耦） |
| D5 | 两层收敛分工 | **单夜内②④（既有 run_search）+ 跨夜①（daemon 层新增）** | 化解 spec §5.2 K 轮歧义：单夜=一次调用内 EI/覆盖度收敛；跨夜=连续 K 夜前沿不扩张。daemon 是 run_search 的薄编排层，不重复采样逻辑 |
| D6 | budget 时间→组数 | **`estimate_budget(4h, n_proc)` 粗估换算**（非 daemon 内 while 循环） | daemon 单次调 run_search 更薄；run_search budget 是组数上限（跑完即停不无限），偏高→次夜断点续跑接续。诚实标注算力账待标定 |
| D7 | publish 自动化程度 | **不自动 promote**（人审 `experiment promote`） | spec §2.2 非目标——防过拟合参数直冲实盘 |
| D8 | 硬闸空 `resolve_active` | **放行**（合法"清场后无在线实验"） | 空列表是确定状态非查询失败；查询异常才 fail-closed 拒绝（D3） |
| D9 | activated_at 缺失 | **保守归入 fresh 拒绝** | 老实验/迁移致 None，宁可误杀不放过裸跑真单 |

---

## 5. 架构与文件落点

### 5.1 文件清单

```
discovery 包（Plan 4 自包含，零跨引擎侵入）
├─ daemon.py      【新】跨夜编排纯函数：读状态→调 run_search→比对前沿→判据①→钉钉/outer
├─ publish.py     【新】冠军 → experiment DRAFT 桥（调 experiment.store，零改 experiment）
├─ schtasks.py    【新】discovery 夜跑任务注册（register/unregister/list 纯函数+幂等先删后建）
├─ run_daemon.bat 【新】包内 bat：call venv + cd root + python -m discovery daemon --budget 4h
├─ store.py       【改】search_run 表扩跨夜状态字段 + read_latest_search_run/write_daemon_state
├─ runner.py      【改】run_search 收尾把 frontier_size 写回 search_run（供 daemon 跨夜比对）
├─ cli.py         【改】加 daemon / publish 子命令 + evaluate_champion outer 去偏复用
└─ __init__.py    【改】导出 daemon/publish API

trading/__main__.py  【改】WARNING → fail-closed ≥5天硬闸（查 resolve_active activated_at）
```

**复用（零改动）**：`experiment.store.create_version` / `experiment.resolver.resolve_active`（可能需暴露 activated_at，见 §6.3）/ `infra.notifier` / `discovery.{objective.evaluate, snapshot.freeze, split.holdout_split, runner.run_search, pareto, dsr}`。

---

## 6. 数据流

### 6.1 L4 daemon 跨夜编排

```
schtasks @02:00 → discovery/run_daemon.bat → python -m discovery daemon --budget 4h
  ├─1 freeze(lake_start) → snapshot_meta（锁数据版本；同 cmd_run）
  ├─2 读跨夜状态：latest = read_latest_search_run(snapshot_hash)
  │     若 latest 且 latest.status=="converged" → 早退（跨夜已收敛，不重复夜跑）+ 日志
  │     （latest=None = 首次 daemon，无历史 run，正常往下跑，k 从 0 起算）
  ├─3 n_budget = estimate_budget(4h, n_proc)  # 算力账 ~180s/组×并发，粗估诚实标注
  ├─4 summary = run_search(meta, split, budget=n_budget, tpe_trials=, rho_threshold=)
  │     └─ 内部：断点续跑去重 + 单夜收敛②EI ④覆盖度（Plan 3 既有，零改）
  ├─5 跨夜判据①（daemon 层新增）：
  │     if latest is None:                              k = 0   # 首次 daemon，从 0 起算
  │     elif summary.frontier_size > latest.frontier_size:  k = 0   # 前沿扩张 → 重置
  │     else:               k = latest.k_rounds_no_expansion + 1   # 未扩张 → 累加
  │     converged_cross = (k >= K=3)
  ├─6 write_daemon_state(snapshot_hash, frontier_size, k_rounds_no_expansion=k,
  │     daemon_run_count=latest+1, status="converged" if converged_cross else summary.status)
  ├─7 冠军 outer 去偏（信息隔离·只读不回写搜索）：
  │     outer = evaluate(load_trial_params(summary.top_trial_id), split.outer)  # 2026 真实 OOS
  ├─8 fire_and_forget(notify_risk_event(新冠军/收敛告警, "INFO"))  # 仅当有新冠军进前沿或收敛
  └─9 打印 RunSummary + outer 去偏报告（人审 / 下一步 publish）
```

**关键不变量**：
- daemon 是 `run_search` 的**薄编排层**——不重复采样/单 run 收敛逻辑，只做跨夜状态比对 + 告警 + outer 去偏。
- **信息隔离**：步骤 7 的 outer 结果严禁回写 run_search 排序（代码级：`outer` 变量不传回 `run_search`）。outer 是去偏参考，回写即 test 段泄露复现。

### 6.2 跨夜状态模型（`search_run` 表扩字段，D2）

```sql
ALTER TABLE search_run ADD COLUMN frontier_size_prev INTEGER DEFAULT 0;
ALTER TABLE search_run ADD COLUMN k_rounds_no_expansion INTEGER DEFAULT 0;
ALTER TABLE search_run ADD COLUMN daemon_run_count INTEGER DEFAULT 0;
-- status 既有 running/converged/budget_exhausted/stopped；converged 复用为「跨夜收敛」
```

- **跨夜状态键 = `snapshot_hash`**：数据快照一变 = 新一轮 daemon（新 search_run 行），`k_rounds_no_expansion` 自然重置（D5 + D6 物理保证）。
- **init_db 幂等 migration**：`PRAGMA table_info(search_run)` 查列存否再 `ALTER ADD COLUMN`，避免重复执行报错。开发期 db 也可删重建（discovery 尚未上线）。
- `read_latest_search_run(snapshot_hash)` / `write_daemon_state(...)` 纯函数，WAL + `_write_lock`（复用 Plan 1 跨线程写锁模式）。

### 6.3 L5 publish（`python -m discovery publish <trial_id>`）

```
├─1 读 trial（params/snapshot_hash）from store
├─2 outer = evaluate(params, split.outer)  # 复用 daemon 同款 evaluate_champion，单源不重复实现
├─3 experiment.create_version(ExperimentVersion(
│     experiment_id=f"neckline_disc_{today}_{trial_id[:6]}", strategy_name="neckline",
│     params=trial.params, weight=0.0, status=DRAFT,
│     source=f"discovery:{snapshot_hash[:8]}", note=f"outer ann/calmar/熊市"))
├─4 打印 outer 去偏报告 + 人审下一步提示：
│     "下一步：人审 outer 报告 → experiment promote <id> --weight 0.1 → 走既有 _eod 链路"
└─5 不自动 promote（D7）
```

**resolver 暴露 `activated_at`**：当前 `ActiveExperiment` dataclass（`experiment/models.py:46`）只有 `experiment_id/strategy_name/params/weight`，**缺 `activated_at`**。硬闸要查影子天数，须让 `resolve_active` 返回值带上 `activated_at`（T6 task 扩 `ActiveExperiment` 字段 + resolver SELECT 补列，向后兼容 additive）。

### 6.4 ≥5 天硬闸（`trading/__main__.py` fail-closed，D3/D8/D9）

```
mode = os.getenv("AUTO_TRADE_MODE", "dry_run")
if mode != "dry_run":  # LIVE
    min_days = int(os.getenv("TRADE_SHADOW_MIN_DAYS", "5"))
    try:
        experiments = resolve_active()   # 含 activated_at（§6.3 扩字段后）
    except Exception:
        fire_and_forget(notify_risk_event("拒切 LIVE：experiment 状态查询失败", "CRITICAL"))
        sys.exit(2)                       # 查询失败 fail-closed（非空列表）
    fresh = [e for e in experiments
             if not e.activated_at or days_since(e.activated_at) < min_days]
                                          # ↑ activated_at 缺失(None) → 保守归入 fresh(D9)
    if fresh:                             # 空列表(experiments==[])→ fresh=[] → 放行(D8)
        fire_and_forget(notify_risk_event(
            f"拒切 LIVE：{len(fresh)} 实验影子期不足 {min_days} 天（最早 {min(...)}）", "CRITICAL"))
        sys.exit(2)
```

---

## 7. 错误处理与风控边界（CLAUDE.md 拷问三连 → Plan 4 组件）

| 边界 | 物理意图 | 处置 |
|---|---|---|
| **首次 daemon（latest=None）** | 无历史 search_run，`latest.frontier_size` 会 AttributeError | 步骤 2/5 显式 `latest is None` 兜底：不早退、k 从 0 起算（首次不算扩张也不累加） |
| **daemon 夜跑崩溃**（拷问②接口/状态机） | kill/OOM 后跨夜状态可能写一半 | `write_daemon_state` 在 `run_search` 返回后单条原子 UPDATE；崩溃夜的 trial 已落库但跨夜状态未更新 → 次夜基于上夜 latest 比对（保守少算一轮扩张，**安全侧**） |
| **schtasks 漏触发/机器关机**（拷问②） | 漏一夜不影响收敛语义 | daemon 幂等（断点续跑去重）；`k_rounds_no_expansion` 只在真跑时更新，漏夜=这轮没发生，不重置 k（k 只在"前沿扩张"时重置） |
| **钉钉告警软降级**（拷问②） | daemon 不应被 IM 网络阻塞 | `fire_and_forget` + `_broadcast` gather return_exceptions（既有机制），投递失败仅 logger |
| **outer 去偏数据缺失**（拷问①流动性/数据） | 2026 holdout 行情缺失 → evaluate 抛/返空 | outer 评估软降级：告警 + 报告标注"outer 评估失败"，**不阻断** daemon/publish 主流程（outer 是去偏参考非阻塞） |
| **budget 估算偏差** | estimate_budget 粗估跑超时/跑不满 | run_search 内部 budget 是组数上限（跑完即停）；偏高→次夜 trial_id 去重断点续跑接续。算力账待 L1 replay 标定 |
| **硬闸 resolve_active 失败**（拷问③策略敞口） | experiment db 锁/损坏 → 误放行 LIVE | **fail-closed**：异常 → `sys.exit(2)` + 告警；空列表（合法清场）→ 放行（D8） |
| **硬闸 activated_at 缺失**（拷问③） | 老实验/迁移致 None | 保守归入 `fresh` 拒绝（D9，宁可误杀） |
| **信息隔离红线**（方法学风控） | outer 回写搜索 = test 段泄露复现 | `evaluate_champion` 结果只进报告/告警，**严禁**回写 run_search 排序 |

---

## 8. 测试策略（spec §9 + Plan 4 新增）

| 模块 | 关键测试 | 数量 |
|---|---|---|
| **store 跨夜状态** | `test_search_run_migration_idempotent`（init_db 重复不报错）/ `test_write_read_daemon_state_roundtrip` | 2 |
| **daemon 编排** | `test_daemon_reads_cross_run_state`（k=2→3 收敛）/ `test_daemon_resets_k_on_frontier_expansion` / `test_daemon_early_exit_when_converged` / `test_daemon_alerts_on_new_champion`（mock fire_and_forget）/ `test_daemon_outer_eval_no_feedback`（信息隔离） | 5 |
| **publish** | `test_publish_creates_draft`（DRAFT + source=discovery:xxx）/ `test_publish_no_auto_promote`（weight=0 未 promote）/ `test_publish_outer_report` | 3 |
| **≥5天硬闸** | `test_live_blocked_when_shadow_insufficient`（exit 2）/ `test_live_allowed_when_shadow_sufficient` / `test_live_blocked_when_activated_at_missing` / `test_resolve_active_failure_blocks_live` / `test_dry_run_skips_gate` | 5 |
| **resolver 扩字段** | `test_resolve_active_includes_activated_at`（向后兼容 additive） | 1 |
| **slow E2E** | 多夜 daemon（3夜不扩张→跨夜收敛自停）+ publish→experiment DRAFT 闭环 | 1 文件 |
| **零回归** | Plan 1-3 的 84 non-slow 全绿 + trading/experiment 既有测试不新增失败 | — |

**TDD 纪律**：每 task 先 RED（测试先写 → ImportError/断言失败）→ GREEN（实现）→ 回归，与 Plan 1-3 一致。schtasks subprocess 调用走纯函数 `build_register_commands` 模式（mock `subprocess.run`，不污染 Windows 任务计划程序，复用 `manage_ops_schtasks` 既有单测纪律）。

---

## 9. task 拆分（8 task，TDD，含 pre-flight）

| # | task | 层 | 关键产出 |
|---|---|---|---|
| **T0** | **pre-flight verification**（Plan 3 地基） | — | ①`.venv310` 重跑 T8 slow 固化绿灯 ②DSR vs `scipy.norm.ppf` 数值对照断言 |
| T1 | search_run 表扩跨夜状态 + 读写 | L4 | `store.py`：`frontier_size_prev`/`k_rounds_no_expansion`/`daemon_run_count` + 幂等 migration + `read_latest_search_run`/`write_daemon_state` 纯函数 |
| T2 | daemon 跨夜编排纯函数 | L4 | `daemon.py`：`run_daemon_cycle()` 读状态→调 run_search→比对前沿→更新 k→判据①自停。**纯函数可单测，不触达 schtasks/钉钉** |
| T3 | daemon 钉钉告警 + outer 去偏调度 | L4 | 新冠军进前沿 → `fire_and_forget(notify_risk_event)`；`evaluate(top, outer)` 跑 2026 真实 OOS |
| T4 | cli daemon 子命令 + schtasks 注册 | L4 | `cli cmd_daemon`（budget 时间→组数换算）+ `discovery/schtasks.py`（register/unregister/list）+ `discovery/run_daemon.bat` |
| T5 | resolver 扩 activated_at + publish 命令 | L5 | `experiment/resolver.py`+`models.py` 扩 `activated_at`（additive）+ `discovery/publish.py` + `cli cmd_publish` |
| T6 | ≥5 天硬闸 fail-closed | L5 | `trading/__main__.py`：mode=live 时查 `resolve_active` 的 `activated_at`，任一 < min_days → `sys.exit(2)` + 钉钉 CRITICAL |
| T7 | slow 端到端集成 | 全 | 模拟多夜 daemon（跨夜判据①收敛）+ publish→experiment DRAFT 闭环 + 硬闸拒绝切 LIVE |

**预估规模**：~8 task，与 Plan 3 持平。discovery 包新增 4 文件（daemon/publish/schtasks/run_daemon.bat）+ 改 4 文件（store/runner/cli/__init__），trading/__main__ 轻改 1 处，experiment/resolver+models additive 扩 1 字段。

---

## 10. 关键决策记录（ADR · Plan 4 增补）

- **ADR14（L4 编排）**：**schtasks 每夜触发短命子命令 + 跨夜状态落 search_run 表**。非常驻进程（崩溃不影响次夜、断点续跑去重天然支持）；跨夜判据①状态与 search_run 同生命周期（snapshot 变=新一轮 k 重置）。spec §5.2 K 轮歧义化解为"单夜②④ + 跨夜①"两层。
- **ADR15（L5 收回）**：**publish 到 DRAFT + 硬闸 + 可观测，candidate-only 虚拟 PnL 列 follow-up**。spec §5.3 字面的 candidate 影子对比在当前架构下物理不可行（candidate 与 prod 共 `_eod` 链路无影子分流，§3.2 Gap 2），强行实现要么"candidate 裸奔实盘"要么"prod 也被影子掉"。诚实收回，留 Plan 5 重写信号分流。
- **ADR16（硬闸 fail-closed）**：**≥5 天硬闸 `sys.exit(2)` + 钉钉告警，activated_at 缺失保守拒绝**。现状 WARNING（spec §5.3 误称"已有硬闸"）名不副实（§3.2 Gap 3）；fail-closed 是风控红线最保守形态。空 `resolve_active` 放行（合法清场），查询异常拒绝（fail-closed）。
- **ADR17（包自包含）**：**discovery 自管 schtasks 调度，不依赖 scripts/**。scripts/ 后续废弃（独立迁移工程）；discovery 包自包含其夜跑注册与 bat，与 broadcast schtasks 解耦。

---

## 11. 验收标准映射（主 spec §12）

| §12 验收项 | Plan 4 交付 |
|---|---|
| **#13 L4 自治**：连续 K=3 轮无新候选前沿 → run 自停；schtasks 夜跑守护 + 断点续跑稳定 | T1-T4：跨夜判据① + daemon + schtasks + 断点续跑（既有） |
| **#14 L5 闭环**：冠军 → experiment DRAFT（source=discovery:run_xxx）；影子 dry_run ≥5 天链路通；不自动 promote | T5-T6：publish DRAFT + ≥5天硬闸真强制；candidate 走既有 `_eod` 链路（影子引擎侧就绪）；不自动 promote（D7） |

**⚠️ #14 部分兑现说明**：「影子 dry_run ≥5 天链路通」的引擎侧（`_eod` resolve 实验→scan_live）已就绪（Plan 1-3 + auto-trading-rehearsal），但 candidate-only 虚拟 PnL 对比未做（ADR15 follow-up）。Plan 4 兑现"publish DRAFT + 硬闸守护 + 可观测归因"，不兑现"虚拟 PnL 对比决定扩量/下线"——后者列 Plan 5。

**全程零回归**：`test_param_iter_kernel_same_source.py` 绿；回测内核零改动；Plan 1-3 的 84 non-slow 不新增失败；trading/experiment 既有测试不新增失败。

---

## 12. 风险与 follow-up

- **candidate-only 影子虚拟 PnL**（Plan 5）：重写 `_eod`/`pre_open` 按 experiment 标影子态分流 + 纸上 mark-to-market 记账。待 prod 在线跑顺 + experiment 系统经实战后再上。
- **算力账标定**（§3.6）：`estimate_budget` 当前粗估（~180s/组 × 并发），待 L1 replay 真实标定单组成本后校准。
- **scripts/ 废弃迁移**（独立工程）：`manage_ops_schtasks.py` + 各 `run_*.bat` 的归宿（ops/ 包或各包自管）不在 Plan 4 范围，Plan 4 的 discovery 自包含调度不受其影响。
- **outer holdout 单段**（spec §13）：2026 至今仅半年，outer 去偏点估计置信区间宽；须与邻域稳定性（Plan 1 已做）交叉印证。follow-up 扩多 outer 段轮流。
- **activated_at 时区/口径**：`experiment.create_version` 落 `created_at`/`activated_at` 用 `_now()`（ISO 本地时区）；硬闸 `days_since` 按自然日算，跨时区忽略（单机部署）。

链接 [[param-discovery-engine]] [[discovery-plan3-l3-l4]] [[quanter-experiment-system]] [[auto-trading-rehearsal]] [[neckline-method]]。
