# 调参循环协议（双层闸 · v1）

> 2026-08-16 · 长周期策略及参数优化模式的运行宪法。执行者：Claude Code 会话驱动（一期）→ 自动环（二期）。
> 配套：全景 `docs/2026-08-16-strategy-param-landscape.md`；日志 `docs/research/ROUND_LOG.md`；ADR-14/15。

## 一、循环形状

```
候选来源                 快筛层                    严审层                     放行层
─────────────────────────────────────────────────────────────────────────────────────
discovery DRAFT 池   →  inner 段 evaluate_replay   冠军候选：              autopromote 七门
邻域扰动提案         →  （replay 口径，            · wf 四折 evaluate_wf    （G1-G7，
LLM 提案（verify 后）    单组全宇宙分钟级）        · discovery verify 邻域   G7 未定稿
                                                   · outer 终审              = fail-closed）
                                                                        ↓
                                                   过闸：灰度两步（0.3→1.0）
                                                   不过：experiment discard
                                                   ↓
                                        ROUND_LOG 记录（假设→数字→决策→下一步）
```

## 二、双层闸分工

- **快筛层（每轮）**：只用 inner 段。判据——inner ann/calmar 代理优于当前工作参数且 n≥30。淘汰不记录明细（ROUND_LOG 只记晋级者）。
- **严审层（里程碑）**：仅对快筛幸存且明显优于 incumbent 的候选。`python -m experiment autopromote <id>`（默认 dry-run）逐门判定。

## 三、口径纪律（R0 实证后的最高军规）

**任何进入放行判据的收益指标必须用 replay 口径**（`evaluate_replay` / PositionModel 组合约束）。R0 实证：scan 口径（kelly/calmar）与 replay 口径**符号反转**（+18.4% vs -23.9%；+53.4% vs -44.5%）。scan 口径数字可作搜索启发与统计工具，**禁止直接作为放行依据**。

## 四、每轮记录义务（ROUND_LOG）

- 假设：这轮想验证什么
- 命令+指纹：snapshot_hash / engine_hash / 参数 experiment_id
- 关键数字：inner/outer 的 replay 口径 + 必要的 scan 口径（标注口径）
- 决策：晋级/discard/继续观察 + 理由
- 下一步

## 五、节奏

- 会话驱动期（当前）：每轮由 Claude Code 发起，产出分析报告（`docs/research/YYYY-MM-DD-*.md`）
- 自动环期（阶段 3 后）：02:00 daemon 搜索 → 18:30 digest+提案+auto-verify → 18:35 autopromote dry-run 日报 → 夜间 `AUTO_PROMOTE_ENABLED` 控制灰度

## 六、红线（不可协商）

1. regime 阈值不进搜索（audit A1）。
2. 行为等价：策略内核改动走 golden + ADR。
3. `AUTO_TRADE_MODE=live` 下 trading 侧改动：env 开关 + 默认零行为变化 + 单向安全论证（ADR-14 范式）。
4. promote 只经 autopromote（七门）或人审 CLI；`AUTO_PROMOTE_ENABLED` 默认 false。
5. 口径纪律（第三节）。
