# ADR-14：分数 Kelly 仓位（fixed→kelly 可切，单向安全）

- 日期：2026-08-16
- 状态：已落地（默认 fixed 影子观察期）
- 编号说明：architecture 序列 09/10 之后直接用 14——11/12/13 已被 discovery spec 内部决策记录（Sobol→TPE / 分层裁判 / DSR）占用，避让防检索混淆
- 关联：audit spec `docs/superpowers/specs/2026-08-13-full-audit-and-governance-spec.md` DG-G5 定稿；A4 实证 `docs/research/2026-08-16-a4-kelly-convergence.md`

## Context（为什么）

审计标定颈线法「4% Kelly 薄边缘」：固定 `pos_cap=0.05` 无视信号质量的时期漂移。DG-G5 定稿「分数 Kelly 0.25× 起步（≈1%/笔），上限 0.5× 需样本外验证」，但一直未落地。

A4 实证（2026-08-16，ACTIVE 冠军 25c602，engine_hash=53090190e55f）：

- 分年 kelly（n≥30 年）：2021 +0.186 / **2022 0.000 / 2023 0.000** / 2024 +0.296 / 2025 +0.212 / 2026 +0.048——正 kelly 年 4/6 **贴线**，均值 +0.124、CV 0.92（幅度极不稳）。
- wf 四折 OOS kelly：+0.000 / +0.310 / +0.099 / +0.0005——**无一翻负**（边缘从不反号）但两折归零。
- 结论：满仓 f* 的复利期望是幻觉；0.25×hat 仓位区间 [1.2%, 4.7%, 5%, 5%]（cap 后）vs 固定 5%——弱年自动降杠杆正是想要的防御性。

## Decision（决策）

1. **env 三键**（`trading/critical.py::_trade_cfg`，env-only SSoT，不引引擎读 DB 耦合）：
   - `TRADE_SIZING_MODE`：`fixed`（默认，零行为变化）| `kelly`；非法值 **fail-closed 拒起**。
   - `TRADE_KELLY_FRACTION`：默认 0.25；合法域 (0, 0.5]，越界 **fail-closed 拒起**（DG-G5 上限语义）。
   - `TRADE_KELLY_HAT`：默认 0.0；值域 [0, 0.5] 越界钳制 + CRITICAL 日志（估计量钳制语义，区别于 fraction 的风控拒起语义）。
2. **注入点**（`trading/eod_plan.py::compute`）：`pos_cap_eff = min(kelly_hat × kelly_fraction, pos_cap)`，传 `build_orders_from_signals(pos_cap=pos_cap_eff)`。复合语义 `budget = capital × pos_cap_eff × experiment_weight` 两层正交（kelly 收策略层基础仓，weight 按实验归因分流）。
3. **hat 来源**：autopromote/验证管线算出 → 写实验 note + 钉钉播报一行 `.env` 配置 → 人工贴入（运维动作可审计）。A4 实证 CV 0.92 → **hat 取保守分位（下三分位）**，弱年退化到最小仓位是特性不是缺陷。
4. **影子日志**：fixed 模式且 hat>0 时打 `[sizing-shadow]` 对照行（零行为变化的观察面）。

## 对抗性推演（为什么不是别的）

- **为什么不直接切 kelly**：`AUTO_TRADE_MODE=live` 实盘在跑——任何 sizing 改动必须默认零行为变化 + 影子观察 ≥5 交易日（对齐 `TRADE_SHADOW_MIN_DAYS=5` 基线节奏）后才可切。
- **为什么 hat 不从 DB 实时读**：critical.py 保持 env-only SSoT（引擎启动不耦合实验库读取）；hat 是慢变量（年级），无实时读的必要性，人工贴 env 的运维成本可接受。
- **为什么 min() 而不是独立 cap**：`min(hat×frac, pos_cap) ≤ pos_cap` 恒成立——单向安全（切 kelly 只会减仓不会加仓）不需要第二道独立风控，复合即安全。
- **为什么 fraction 越界拒起而 hat 越界钳制**：fraction 是风控决策值（人设定的杠杆意志），设错必须停；hat 是统计量（估计的输入），钳到边界 + 告警已足够暴露笔误。

## Consequences

- 正面：弱信号年（2022/2023 口径）自动降杠杆至最小仓位；kelly 口径与 DG-G5 审计定稿对齐；A4 报告有了 production 落点。
- 负面/代价：多三个 env 键的运维面；hat 需人工随冠军更替更新（忘更新 = 用旧 hat，偏保守方向）。
- 回滚：`.env` 改回 `TRADE_SIZING_MODE=fixed` + 重启引擎（QuanterServer schtasks 重启 SOP）。
- 上线节奏：影子日志观察 ≥5 交易日 → 贴 hat（autopromote 播报值）→ 切 `kelly`。
