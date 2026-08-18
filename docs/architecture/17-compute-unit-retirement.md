# ADR-17：compute_unit（Mac 远程计算单元）整体退役

- 日期：2026-08-18
- 状态：已裁决并执行（与退役 spec、工单链 T1-T4 同批落地，分支 `debt/compute-unit-retirement-0818`）
- 关联：退役 spec `docs/superpowers/specs/2026-08-18-compute-unit-retirement-design.md`；出生设计 `docs/superpowers/specs/2026-07-26-compute-unit-design.md` 与实施计划 `docs/superpowers/plans/2026-07-26-compute-unit.md`（均留档为史料，不再维护）

## Context（为什么）

compute_unit 出生于 2026-07-26：Mac（封闭工作机 · 只允许 git pull）分摊 Win 主机的参数
回测算力——Win 导出 task.json 进 git → Mac 离线跑批 → 人带回摘要（结果不回库），跨机
以三件哈希（git_commit / engine_hash / parquet_sha256）+ snapshot 双校验防漂移。

此后 discovery 本地化闭环（Plan 2 `python -m discovery run` 并发跑批 + Plan 4 daemon
夜跑 + autopromote 七门）使 Win 单机即可完成搜索，跨机跑批场景消失：仓库无 result.json
回传痕迹、ops 调度零引用，仅剩 `tasks/sobol-3000.json` 一个历史导出工件。roadmap
Phase 3 对其未来角色标注"待定"。但包内 `hashes.py` 的 ENGINE_FILES/engine_hash 是
discovery 搜索可比性的活体地基（trial stale 判定 + `discovery verify`），不能陪葬。

## Decision（决策）

1. **整体退役，不陪葬指纹**：删除 `compute_unit/`（8 文件 771 行）、`tests/compute_unit/`
   （10 文件）、`scripts/export_sobol_task.py`、`tasks/sobol-3000.json`。不留 env 开关、
   不留死配置（对齐 A-2 / trailing / regime 删除先例；回滚走 git revert，四步独立提交）。
2. **指纹单源化迁入 discovery**：`ENGINE_FILES` 清单 + hash 算法逐字节迁
   `discovery/fingerprint.py`；`discovery/runner` 与 `discovery/cli` 的 `_engine_hash`
   收口为薄委托——顺带消灭"同款算法双份重声明"债（2026-08-13 三份实现不一致事故的
   根因结构）。迁移前后输出恒等（T1 验证门留证 `ce16cc4ee4de`），trials 库既有指纹
   语义连续；内核覆盖守卫 + 单源委托守护迁 `tests/discovery/test_engine_fingerprint.py`。
3. **engine_hash 基线预期重估**：注释清创触及 3 个内核文件（replay / strategy /
   objective，均 docstring 级），指纹随之 `ce16cc4ee4de` → `26028e24860e`——内容哈希
   的设计语义（同 T18 先例），非行为变化；老 trial 与新跑不可比属预期，`discovery
   verify` 对旧 trial 报 stale 如实反映。
4. **复活路径**：三维扩展（多策略 × 多资产）若需远程算力，按当时引擎**重建优于复活**
   v2 协议——跨机防漂移（内容指纹 + 双校验拒跑）与 C6 等价红线的方法论在出生 spec
   （史料）与 git 历史中可查。

## 后果

- 包拓扑 13→12（≈44.6k 行 / 240 文件，2026-08-18 扫描）；infra 仍 8 包入边
  （compute_unit 2 出、experiment 1 入——后者为 08-15 后 master 增量，与退役无关）。
- `evaluate_replay` / `report_metrics`（discovery/objective）**不随退役**：runner 冠军
  复评 / research proposals / autopromote / diag 证据脚本均为活体调用方，仅出处注释
  改历史引述。
- GBK stderr 治理（infra/pyio P2-A）的出生教训保留，出处标注已退役。
- 历史文档（出生 spec/plan、2026-08-03 评审）一律不改，本 ADR 反向链接。
