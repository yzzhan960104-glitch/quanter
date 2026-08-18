---
title: compute_unit（Mac 远程计算单元）退役设计
date: 2026-08-18
status: accepted（同日执行，工单链 T1-T4）
author: session（peer reviewer / risk officer 姿态）
scope: compute_unit 包整体退役——指纹子系统单源化迁入 discovery，其余全删
related:
  - docs/superpowers/specs/2026-07-26-compute-unit-design.md（出生设计，退役后留档为史料）
  - docs/superpowers/plans/2026-07-26-compute-unit.md（出生实施计划，史料）
  - docs/architecture/17-compute-unit-retirement.md（ADR-17 退役裁决）
  - docs/architecture/02-module-dependencies.md（包拓扑同步）
---

# compute_unit 退役设计

> 核心命题：compute_unit 不是纯死代码——指纹子系统是 discovery 搜索可比性的活体地基。
> 必须**先搬家再拆房**：指纹单源化（零行为变化）→ 拆包 → 注释清创 → 活文档同步。

## 0. 裁决依据（为什么退）

- Mac 远程跑批主使命已被本地链路取代：`python -m discovery run`（Plan 2）+ daemon 夜跑
  （Plan 4）在 Win 本机闭环，跨机三件哈希防漂移的守护对象（Win↔Mac 双机跑批）不复存在。
- 仓库仅剩历史工件 `tasks/sobol-3000.json`，无 result.json 回传痕迹（设计上"结果不回库"），
  ops/ 调度任务零引用——链路事实上停摆。
- roadmap Phase 3 对其角色标注"待定"；三维扩展（多策略×多资产）尚 Not-yet-specified，
  届时若需远程算力，按当时引擎重建优于拖着 v2 协议复活（ADR-17 记录复活路径）。

## 1. master（2881d0bc）全量依赖普查

### 1.1 运行时 import（拆包前必须改线的 6 处）

| 引用点 | 引用内容 | 处置 |
|---|---|---|
| `discovery/runner.py:47` | `ENGINE_FILES`（惰性 import） | T1 改指 `discovery/fingerprint` |
| `diag/a3_fill_realism_probe.py:103` | `_engine_hash` | T1 改线 |
| `diag/a4_kelly_convergence.py:118` | `_engine_hash` | T1 改线 |
| `diag/r1_portfolio_calibration.py:63` | `_engine_hash` | T1 改线 |
| `diag/r1_quality_grid.py:71` | `_engine_hash` | T1 改线 |
| `scripts/export_sobol_task.py:29` | `task_export.export_task` | T2 随包同删（Mac 流唯一入口） |

另：`discovery/cli.py` 存在第三个 `_engine_hash`（委托 runner 的薄壳），其 docstring
引用旧守卫测试名，T1 一并订正。

### 1.2 不删函数的核验（范围不扩大）

- `discovery/objective.evaluate_replay`：活体——`discovery/runner.py:267`（冠军 replay
  复评）、`research/proposals.py`、`research/autopromote.py`、`presentation/server`、
  4 个 diag 脚本均在用。仅 docstring 出处提及 compute_unit，T3 改注释。
- `discovery/objective.report_metrics`：活体——`objective.py:196` 内部调用。同上。

### 1.3 注释锚（~10 处，无运行时依赖，T3 改措辞防死链）

`backtest/replay.py:162`、`strategies/neckline/strategy.py:69`（策略实例契约出处）、
`broadcast/brief_strategy.py:72`（回撤口径出处）、`discovery/objective.py:5,143`、
`discovery/split.py:60`（caller 计数含 compute_unit）、`infra/pyio.py:19,36`（GBK 崩溃
教训出处）、`tests/ops/test_gbk_smoke.py:35,37`（docstring 引用；测试体只用 `python -c`
跑 infra.pyio，不受影响）。

### 1.4 测试与文档

- `tests/compute_unit/`（10 文件）：8 个随包同删（CLI/env_check/protocol/runner/summary/
  task_export/e2e 的 C6 等价红线——守护对象消失，保证自然失效）；
  `test_hashes.py` 两守卫**迁移**：内核覆盖守卫原样保留；双实现一致性断言转型为
  单源委托守护。
- 活文档：`README.md` §9.4 整节 + §10 模块表行（§9.5 顺位重编号；:210/:254 历史
  changelog 保留）；`docs/architecture/02-module-dependencies.md`（节点/2 出边/
  discovery 1 入边/infra 入边计数 8→7/LOC 表/PKGS 清单）；`docs/architecture/README.md:53`；
  `06-tech-debt.md:138`；`roadmap.md:27`。
- 历史文档**明确不改**：`2026-07-26-compute-unit-design.md`、`2026-07-26-compute-unit.md`、
  `2026-08-03-backtest-strategy-review-and-agent-loop.md`——出生史料，ADR-17 反向链接。

## 2. 工单链设计（4 步，每步独立提交可回滚）

### T1 指纹搬家（零行为变化，唯一技术步）

新建 `discovery/fingerprint.py`：`ENGINE_FILES` 元组 + `engine_hash()` **逐字节照搬**
自 `compute_unit/hashes.py`（文件名 `encode("utf-8")` 入 hash + 文件内容 + `[:12]`
截断；`PROJECT_ROOT = parents[1]` 在新位置依然正确；元组次序是 hash 输入的一部分，
不得重排）。

- `discovery/runner.py:_engine_hash` 改为薄委托（消灭"同款算法双份重声明"技术债；
  `tests/discovery/test_runner.py` 8 处 `monkeypatch.setattr(runner, "_engine_hash")`
  打在模块属性上，委托化完全兼容）。
- `discovery/cli.py` docstring 守卫测试名订正。
- 4 个 diag 脚本 import 改指新模块。
- `tests/discovery/test_engine_fingerprint.py`：覆盖守卫（迁移）+ 单源委托守护 +
  过渡期搬迁等价断言（`compute_unit.hashes._engine_hash() == fingerprint.engine_hash()`，
  T2 删包时随包移除）。

**红线**：engine_hash 输出逐字节不变。搬迁前留证值 `ce16cc4ee4de`（2881d0bc 工作树），
T1 验收 = 新旧实现输出相等 + 实测值等于留证值。trials 库既有 engine_hash 全程有效，
零 stale 误标。指纹模块自身**不入** ENGINE_FILES（与 hashes.py 时期对称）——改指纹
实现不误伤老 trial 可比性。

### T2 拆包

删 `compute_unit/`（8 文件 771 行）、`tests/compute_unit/`（10 文件）、
`scripts/export_sobol_task.py`、遗留工件 `tasks/sobol-3000.json`。`hashes.py` 的
`_git_head_sha/_file_sha256` 仅被同包 task_export/env_check 使用，不迁移、随包消失。

### T3 注释清创

§1.3 台账逐点改写：契约/口径定义转就地声明，出处改历史引述（"源自已退役的
compute_unit，见 ADR-17"）；`split.py:60` caller 计数订正。

### T4 活文档 + ADR-17

§1.4 四处文档同步（02 依赖图用文内扫描脚本重数边权/LOC，不手改增量）；
新增 `docs/architecture/17-compute-unit-retirement.md`（沿用 14/15/16 编号惯例）。

## 3. 验证门

| 门 | 判据 |
|---|---|
| G1 hash 等价 | T1 后 `discovery.fingerprint.engine_hash()` == `ce16cc4ee4de` |
| G2 守卫绿 | `pytest tests/discovery/test_engine_fingerprint.py tests/discovery/test_runner.py` 绿 |
| G3 收集零错 | T2 后 `pytest --collect-only tests/` 零 import 错误 |
| G4 零活引用 | `git grep -n "compute_unit" -- "*.py"` 仅剩 ADR 式历史注释（T3 后清零死路径） |
| G5 回滚 | 四步独立 commit，纯 `git revert`；零 schema/DB 变更 |

## 4. 风险登记

1. **三维扩展复活需求**（低）——ADR-17 明确"重建优于复活"；v2 协议设计史料在档，
   git 历史整包可找回。
2. **C6 等价红线消失**（无残余）——守护对象（远程跑批口径漂移）随场景消失。
3. **依赖图手工统计易错**（低）——T4 用 02 文档内嵌扫描脚本重数，不手改增量。
